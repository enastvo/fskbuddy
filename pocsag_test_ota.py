#!/usr/bin/env python3
"""Real over-the-air POCSAG test: TX out the TRX port, RX in on RX2, same
board. This is pocsag_test_loopback.py's twin with the digital loopback
register removed -- signal goes through the actual DUC/DAC/mixer/antenna/
mixer/ADC/DDC path this time, same as the FLEX over-the-air test earlier.

One process, not pocsag_tx.py + pocsag_rx.py separately -- only one process
can hold the device open at a time, and this needs TX and RX live
concurrently anyway for the framer to have something to lock onto.

Adjust TX_GAIN / RX_GAIN below (or edit to add argparse) -- the script
prints the hardware's actual valid gain range on startup.
"""
import threading
import time
import numpy as np
import uhd

import pocsag as p
from pocsag_modem import open_usrp, modulate_cpfsk, REG_POCSAG_CTRL, RB_POCSAG_STATUS

# fsk_demod.v's discriminator has no DC/CFO correction -- it's a bare
# sign-of-instantaneous-frequency slicer around 0Hz. This board's RX front
# end has a real, previously-measured ~-3.4kHz LO-leakage/DC-offset artifact
# baked into the analog path (present over real RF, absent in the digital
# loopback register we validated against earlier -- that's why loopback was
# flawless and real RF isn't automatically). Standard POCSAG deviation is
# only +/-4500Hz, comparable in size to that offset, likely biasing enough
# decisions to prevent the framer from ever locking. Using a much larger
# deviation here swamps that artifact, same fix that made the FLEX
# over-the-air test work earlier -- this validates the PHY chain over real
# RF; it trades away compatibility with a real commercial pager's exact
# deviation, which was never the goal of this test anyway.

FREQ = 929.6625e6
RATE = 1e6
BITRATE = 1200
ADDRESS = 1234567
FUNCTION = 3
MESSAGE = "the quick brown fox jumps over the lazy dog 0123456789"
DURATION_S = 20.0

TX_GAIN = 15.0
RX_GAIN = 35.0
TEST_DEVIATION_HZ = 25000.0  # standard 4500Hz still fully fails on this board -- see
                              # module docstring re: the ~3.4kHz LO-leakage DC-offset
                              # artifact this RX front end has (comparable in size to
                              # spec deviation, swamps the zero-threshold discriminator)


class TxThread(threading.Thread):
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
    usrp = open_usrp(FREQ, RATE, gain=RX_GAIN, antenna="RX2", tx=True)
    usrp.set_tx_gain(TX_GAIN)
    print(f"RX gain range: {usrp.get_rx_gain_range()}  (using {RX_GAIN} dB)")
    print(f"TX gain range: {usrp.get_tx_gain_range()}  (using {TX_GAIN} dB)")
    print(f"RX {usrp.get_rx_freq()/1e6:.4f} MHz on {usrp.get_rx_antenna()}, "
          f"TX {usrp.get_tx_freq()/1e6:.4f} MHz on TRX")

    regs = usrp.get_user_settings_iface(0)
    sps = round(usrp.get_rx_rate() / BITRATE)
    actual_bitrate = usrp.get_rx_rate() / sps
    print(f"sps={sps}, actual bitrate={actual_bitrate:.1f}bps")
    regs.poke32(REG_POCSAG_CTRL * 4, (1 << 16) | sps)

    msg_cws = p.encode_alpha(MESSAGE)
    bits = p.build_bitstream(ADDRESS, FUNCTION, msg_cws)
    iq = modulate_cpfsk(bits, sps, RATE, deviation_hz=TEST_DEVIATION_HZ)
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

    parser = p.LiveParser(alpha_function=3)
    n_codewords = 0
    n_pages = 0
    n_correct = 0
    n_prefix_correct = []
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
                    addr_ok = (page.address == ADDRESS)
                    exact_ok = addr_ok and (page.message == MESSAGE)
                    n_correct += exact_ok
                    prefix_len = 0
                    for a, b in zip(page.message, MESSAGE):
                        if a != b:
                            break
                        prefix_len += 1
                    n_prefix_correct.append(prefix_len)
                    if exact_ok:
                        tag = "OK"
                    elif prefix_len == len(MESSAGE):
                        tag = "OK (message correct, trailing junk after)"
                    else:
                        tag = f"correct prefix: {prefix_len}/{len(MESSAGE)} chars"
                    print(f"  PAGE addr={page.address} func={page.function} "
                          f"message={page.message!r}  [{tag}]")
    finally:
        tx_thread.stop_event.set()
        tx_thread.join(timeout=2.0)
        stream_cmd = uhd.types.StreamCMD(uhd.types.StreamMode.stop_cont)
        rx_streamer.issue_stream_cmd(stream_cmd)
        regs.poke32(REG_POCSAG_CTRL * 4, 0)

    print(f"\n{n_codewords} codewords captured, {n_pages} page(s) decoded, "
          f"{n_correct} exact matches.")
    if n_prefix_correct:
        full_msg_ok = sum(1 for n in n_prefix_correct if n >= len(MESSAGE))
        print(f"{full_msg_ok}/{n_pages} had the full correct message content "
              f"(address + all {len(MESSAGE)} chars right, some with trailing "
              f"junk from a late batch-boundary detection).")
        print(f"correct-prefix lengths: {n_prefix_correct}")


if __name__ == "__main__":
    main()
