#!/usr/bin/env python3
"""Real two-radio over-the-air GSC test: one B200mini transmits, a SEPARATE
one receives -- true OTA, not pocsag_test_ota.py's same-board TRX->RX2
loopback-through-air trick (this one doesn't need that trick since there
are two boards; genuinely independent TX and RX antennas/front ends).

Two independent MultiUSRP sessions, one per board (identified by serial --
see SERIALS below, or override via argv). TX runs in its own thread against
its own device; RX polling happens in the main thread against the other
device -- each device only ever sees the one thread it actually needs, so
the documented 2-thread-max-per-device contract (pocsag_modem.py's module
docstring) is satisfied independently on each board, not shared across them
(they're different USB devices entirely).

Uses a larger-than-spec deviation, same reasoning as pocsag_test_ota.py:
this board's RX front end has a real, previously-measured ~3.4kHz
LO-leakage/DC-offset artifact that a bare zero-threshold discriminator is
sensitive to. GSC's own real-world DEFAULT_DEVIATION_HZ (2000Hz, see
gsc.py) is comparable in size to that offset -- likely to bias slicing
decisions the same way standard 4500Hz POCSAG deviation originally did,
before that test found 25kHz swamps it. GSC's PHY (channel filter,
fsk_demod discriminator) is the exact same shared fabric POCSAG's own OTA
test already validated at 25kHz -- this is a well-precedented choice, not a
new gamble.
"""
import sys
import threading
import time
import numpy as np
import uhd

import gsc as g
from pocsag_modem import open_usrp, modulate_cpfsk, REG_GSC_CTRL, RB_GSC_STATUS, list_devices

FREQ = 929.6625e6
RATE = 1e6
GSC_BITRATE = 600  # GSC's fixed baud rate, see gsc.py's module docstring / transceiver.py
ADDRESS = 765432
FUNCTION = 3
MESSAGE = "HELLO GSC WORLD 123"
DURATION_S = 20.0

TX_GAIN = 50.0  # matches pocsag_tui.py's TUI_DEFAULT_TX_GAIN -- calibrated for a real
                 # two-separate-boards link budget (found the hard way: pocsag_test_ota.py's
                 # own 15dB/35dB is same-board TRX->RX2 leakage, near-zero path loss, way too
                 # weak for real antenna-to-antenna distance)
RX_GAIN = 65.0  # matches pocsag_tui.py's TUI_DEFAULT_RX_GAIN, see above
TEST_DEVIATION_HZ = 25000.0  # see module docstring -- swamps this board's DC-offset artifact,
                              # same fix pocsag_test_ota.py already validated on the same PHY


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
    devices = list_devices()
    if len(devices) < 2:
        print(f"Need 2 B200minis, found {len(devices)}: {devices}")
        sys.exit(1)
    tx_serial = sys.argv[1] if len(sys.argv) > 1 else devices[0]["serial"]
    rx_serial = sys.argv[2] if len(sys.argv) > 2 else devices[1]["serial"]
    if tx_serial == rx_serial:
        print("TX and RX serials must differ")
        sys.exit(1)
    print(f"TX board: {tx_serial}   RX board: {rx_serial}")

    tx_usrp = open_usrp(FREQ, RATE, gain=RX_GAIN, antenna="RX2", tx=True, serial=tx_serial)
    tx_usrp.set_tx_gain(TX_GAIN)
    rx_usrp = open_usrp(FREQ, RATE, gain=RX_GAIN, antenna="RX2", tx=False, serial=rx_serial)
    rx_regs = rx_usrp.get_user_settings_iface(0)

    print(f"TX {tx_usrp.get_tx_freq()/1e6:.4f} MHz gain={TX_GAIN}dB (TRX)")
    print(f"RX {rx_usrp.get_rx_freq()/1e6:.4f} MHz gain={RX_GAIN}dB (RX2)")

    sps = round(rx_usrp.get_rx_rate() / GSC_BITRATE)
    actual_bitrate = rx_usrp.get_rx_rate() / sps
    print(f"sps={sps}, actual bitrate={actual_bitrate:.1f}bps")
    rx_regs.poke32(REG_GSC_CTRL * 4, (1 << 16) | sps)

    bits = g.build_bitstream(ADDRESS, FUNCTION, MESSAGE)
    iq = modulate_cpfsk(bits, sps, tx_usrp.get_tx_rate(), deviation_hz=TEST_DEVIATION_HZ)
    print(f"TX waveform: {len(bits)} bits, {len(iq)} samples "
          f"({len(iq)/tx_usrp.get_tx_rate()*1000:.1f} ms/loop)")

    tx_streamer = tx_usrp.get_tx_stream(uhd.usrp.StreamArgs("fc32", "sc16"))
    rx_streamer = rx_usrp.get_rx_stream(uhd.usrp.StreamArgs("fc32", "sc16"))

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
    n_correct = 0
    last_count = None
    was_locked = False

    print(f"Polling for {DURATION_S:.0f}s...")
    buf = np.zeros(rx_streamer.get_max_num_samps(), dtype=np.complex64)
    md = uhd.types.RXMetadata()
    deadline = time.monotonic() + DURATION_S
    try:
        while time.monotonic() < deadline:
            rx_streamer.recv(buf, md, timeout=0.3)
            status = rx_regs.peek64(RB_GSC_STATUS * 8)
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
                    ok = (page.address == ADDRESS and page.message == MESSAGE)
                    n_correct += ok
                    print(f"  PAGE addr={page.address} func={page.function} "
                          f"message={page.message!r}  [{'OK' if ok else 'MISMATCH'}]")
    finally:
        tx_thread.stop_event.set()
        tx_thread.join(timeout=2.0)
        stream_cmd = uhd.types.StreamCMD(uhd.types.StreamMode.stop_cont)
        rx_streamer.issue_stream_cmd(stream_cmd)
        rx_regs.poke32(REG_GSC_CTRL * 4, 0)

    print(f"\n{n_blocks} blocks captured, {n_pages} page(s) decoded, {n_correct} exact matches.")
    if n_pages == 0:
        print("FAIL: no pages decoded.")
    else:
        print("Done.")


if __name__ == "__main__":
    main()
