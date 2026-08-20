#!/usr/bin/env python3
"""Validates the FPGA GSC PHY (gsc_bitsync + gsc_framer.v) over the internal
digital TX->RX loopback register, before trying real RF. Mirrors
pocsag_test_loopback.py exactly (same rationale: isolates RTL logic bugs
from RF link-budget/alignment issues), just against GSC's own registers
(REG_GSC_CTRL/RB_GSC_STATUS) and gsc.py's build_bitstream()/LiveParser
instead of POCSAG's.

Continuously transmits a known GSC message (looped, so there's always fresh
preamble+blocks flowing) while draining RX and polling the new PHY
registers -- both from this same (main) thread; see pocsag_modem's module
docstring for why RX draining and register polling can't be split across
their own separate threads on this device.

Note: this script modulates the whole loop with ZERO gap between
iterations -- a worse case than any real transmission, which is why it can
show occasional (safely-rejected, non-corrupting) decode errors right at
each loop boundary even on a known-good build. That's a real, understood,
now-documented characteristic of gsc_framer.v's comma-based resync (a
trailing control word immediately followed by another transmission's own
preamble, both alternating patterns, can transiently confuse it) -- see
gsc_framer.v's header and transceiver.py's PocsagTransmitter.send(), which
inserts a real gap between repeats specifically to avoid this in normal
use. This script deliberately doesn't use that gap, since it's exercising
the framer directly, not through send().
"""
import threading
import time
import numpy as np
import uhd

import gsc as g
from pocsag_modem import open_usrp, modulate_cpfsk, REG_GSC_CTRL, RB_GSC_STATUS

FREQ = 929.6625e6
RATE = 1e6
BITRATE = 600  # GSC's fixed baud rate -- see gsc.py/gsc_framer.v, not user-configurable
ADDRESS = 765432  # < 2^21, see gsc.py's encode_address bounds check
FUNCTION = 3
MESSAGE = "HELLO GSC WORLD 123"
DURATION_S = 8.0

LOOPBACK_REG = 2  # user_loopback, my_addr=2 (see radio_legacy.v) -- shared with POCSAG's
                  # own loopback test, splices TX IQ straight to RX in fabric


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
    regs.poke32(REG_GSC_CTRL * 4, (1 << 16) | sps)

    bits = g.build_bitstream(ADDRESS, FUNCTION, MESSAGE)
    iq = modulate_cpfsk(bits, sps, RATE, deviation_hz=g.DEFAULT_DEVIATION_HZ)
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

    def on_error(w1, w2, errs):
        print(f"  [decode error: w1=0x{w1:06x} w2=0x{w2:06x} errs={errs}]")

    parser = g.LiveParser(on_error=on_error)
    n_blocks = 0
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
            status = regs.peek64(RB_GSC_STATUS * 8)
            locked = (status >> 54) & 0x1
            count = (status >> 46) & 0xFF
            word1 = (status >> 23) & 0x7FFFFF
            word2 = status & 0x7FFFFF

            if locked and not was_locked:
                print("  [block sync acquired]")
            was_locked = bool(locked)

            if last_count is None:
                last_count = count
            elif count != last_count:
                n_new = (count - last_count) & 0xFF
                if n_new > 1:
                    print(f"  [warning: missed {n_new - 1} block(s)]")
                last_count = count
                n_blocks += 1

                page = parser.feed(word1, word2)
                if page is not None:
                    n_pages += 1
                    match = "OK" if (page.address == ADDRESS and page.message == MESSAGE) else "MISMATCH"
                    print(f"  PAGE addr={page.address} func={page.function} "
                          f"message={page.message!r}  [{match}]")
    finally:
        tx_thread.stop_event.set()
        tx_thread.join(timeout=2.0)
        stream_cmd = uhd.types.StreamCMD(uhd.types.StreamMode.stop_cont)
        rx_streamer.issue_stream_cmd(stream_cmd)
        regs.poke32(REG_GSC_CTRL * 4, 0)
        regs.poke32(LOOPBACK_REG * 4, 0)

    print(f"\n{n_blocks} blocks captured, {n_pages} page(s) decoded.")
    if n_pages == 0:
        print("FAIL: no pages decoded.")
    else:
        print("Done.")


if __name__ == "__main__":
    main()
