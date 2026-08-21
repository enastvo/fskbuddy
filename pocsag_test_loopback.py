#!/usr/bin/env python3
# Copyright (C) 2026 Estefan Nastvogel
# SPDX-License-Identifier: GPL-3.0-or-later
"""Validates the FPGA POCSAG PHY (pocsag_bitsync.v + pocsag_framer.v) over
the internal digital TX->RX loopback register, before trying real RF. This
isolates RTL logic bugs from RF link-budget/alignment issues -- if this
doesn't decode cleanly, the problem is in the FPGA, not the antenna setup.

Continuously transmits a known POCSAG message (looped, so there's always
fresh preamble+batches flowing) while draining RX and polling the new PHY
registers -- both from this same (main) thread; see modem's module
docstring for why RX draining and register polling can't be split across
their own separate threads on this device.
"""
import threading
import time
import numpy as np
import uhd

import pocsag as p
from modem import open_usrp, modulate_cpfsk, REG_POCSAG_CTRL, RB_POCSAG_STATUS
from transceiver import DEFAULT_FREQ

FREQ = DEFAULT_FREQ  # not redeclared as a literal -- see transceiver.py, the one place
                      # this project's default operating frequency is actually defined
RATE = 1e6
BITRATE = 1200
ADDRESS = 1234567  # frame_number=7 -> message spans multiple batches
FUNCTION = 3
MESSAGE = "the quick brown fox jumps over the lazy dog 0123456789"
DURATION_S = 8.0

LOOPBACK_REG = 2  # user_loopback, my_addr=2 (see radio_legacy.v)


class TxThread(threading.Thread):
    """TX runs in its own thread; RX+register polling happens together in
    the main thread (see module docstring -- three-way concurrent USB
    access from separate threads deadlocks this device, two-way is fine)."""
    CHUNK = 4000

    def __init__(self, tx_streamer, iq):
        super().__init__(daemon=True)
        self.tx_streamer = tx_streamer
        self.iq = iq
        self.stop_event = threading.Event()

    def run(self):
        md = uhd.types.TXMetadata()
        md.start_of_burst = True
        md.has_time_spec = False
        while not self.stop_event.is_set():
            for start in range(0, len(self.iq), self.CHUNK):
                if self.stop_event.is_set():
                    break
                self.tx_streamer.send(self.iq[start:start + self.CHUNK], md)
                md.start_of_burst = False
        md.end_of_burst = True
        self.tx_streamer.send(np.zeros(1, dtype=np.complex64), md)


def main():
    usrp = open_usrp(FREQ, RATE, gain=0, antenna="RX2", tx=True)
    usrp.set_tx_gain(0)
    regs = usrp.get_user_settings_iface(0)

    regs.poke32(LOOPBACK_REG * 4, 1)
    print("loopback readback (should be 1):", regs.peek64(1 * 8))

    sps = round(usrp.get_rx_rate() / BITRATE)
    actual_bitrate = usrp.get_rx_rate() / sps
    print(f"sps={sps}, actual bitrate={actual_bitrate:.1f}bps")
    regs.poke32(REG_POCSAG_CTRL * 4, (1 << 16) | sps)

    msg_cws = p.encode_alpha(MESSAGE) if FUNCTION == 3 else p.encode_numeric(MESSAGE)
    bits = p.build_bitstream(ADDRESS, FUNCTION, msg_cws)
    iq = modulate_cpfsk(bits, sps, RATE)
    print(f"TX waveform: {len(bits)} bits, {len(iq)} samples "
          f"({len(iq)/RATE*1000:.1f} ms/loop)")

    tx_streamer = usrp.get_tx_stream(uhd.usrp.StreamArgs("fc32", "sc16"))
    rx_streamer = usrp.get_rx_stream(uhd.usrp.StreamArgs("fc32", "sc16"))

    stream_cmd = uhd.types.StreamCMD(uhd.types.StreamMode.start_cont)
    stream_cmd.stream_now = True
    rx_streamer.issue_stream_cmd(stream_cmd)

    tx_thread = TxThread(tx_streamer, iq)
    tx_thread.start()
    time.sleep(0.2)

    parser = p.LiveParser(alpha_function=3)  # fixed convention: function 3 = alpha, not "this test's FUNCTION"
    n_codewords = 0
    n_pages = 0
    last_count = None
    was_locked = False

    print(f"Polling for {DURATION_S:.0f}s...")
    buf = np.zeros(rx_streamer.get_max_num_samps(), dtype=np.complex64)
    md = uhd.types.RXMetadata()
    deadline = time.monotonic() + DURATION_S
    try:
        while time.monotonic() < deadline:
            rx_streamer.recv(buf, md, timeout=0.3)
            status = regs.peek64(RB_POCSAG_STATUS * 8)
            locked = (status >> 40) & 0x1
            count = (status >> 32) & 0xFF
            codeword = status & 0xFFFFFFFF

            if locked and not was_locked:
                print("  [batch sync acquired]")
            was_locked = bool(locked)

            if last_count is None:
                last_count = count
            elif count != last_count:
                n_new = (count - last_count) & 0xFF
                if n_new > 1:
                    print(f"  [warning: missed {n_new - 1} codeword(s)]")
                last_count = count
                n_codewords += 1

                page = parser.feed(codeword)
                if page is not None:
                    n_pages += 1
                    expected = MESSAGE if FUNCTION == 3 else MESSAGE.ljust((len(MESSAGE) + 4) // 5 * 5)
                    match = "OK" if (page.address == ADDRESS and page.message == expected) else "MISMATCH"
                    print(f"  PAGE addr={page.address} func={page.function} "
                          f"message={page.message!r}  [{match}]")
    finally:
        tx_thread.stop_event.set()
        tx_thread.join(timeout=2.0)
        stream_cmd = uhd.types.StreamCMD(uhd.types.StreamMode.stop_cont)
        rx_streamer.issue_stream_cmd(stream_cmd)
        regs.poke32(REG_POCSAG_CTRL * 4, 0)
        regs.poke32(LOOPBACK_REG * 4, 0)

    print(f"\n{n_codewords} codewords captured, {n_pages} page(s) decoded.")
    if n_pages == 0:
        print("FAIL: no pages decoded.")
    else:
        print("Done.")


if __name__ == "__main__":
    main()
