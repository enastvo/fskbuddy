#!/usr/bin/env python3
# Copyright (C) 2026 Estefan Nastvogel
# SPDX-License-Identifier: GPL-3.0-or-later
"""Validates PocsagReceiver's register-poll-only mode (see
radio_legacy.v's run_rx_fabric / PocsagReceiver.start()'s
`self._streaming = self.on_spectrum is not None` gate) end-to-end, over
real RF between two genuinely separate B200minis -- one board's
PocsagTransceiver transmits, the other's PocsagReceiver (started with NO
on_spectrum, matching every headless use case: fskbuddy.py listen/send,
a TUI with both panels hidden) receives.

Unlike gsc_test_ota_2radio.py / pocsag_test_ota.py (which manage
stream_cmd/recv() directly), this goes through the actual production
PocsagReceiver class -- the thing that changed. Tests BOTH protocols.

Also confirms recv() is never called in this mode by construction (not by
sniffing the USB bus -- see the module's own start()/_run() gating) and
prints the receiver's own internal _streaming flag as direct evidence.
"""
import sys
import time

from modem import open_usrp, list_devices
from transceiver import (PocsagReceiver, PocsagTransmitter, GSC_BITRATE, DEFAULT_FREQ,
                          TWO_RADIO_TX_GAIN, TWO_RADIO_RX_GAIN)

FREQ = DEFAULT_FREQ
RATE = 1e6
TX_GAIN = TWO_RADIO_TX_GAIN
RX_GAIN = TWO_RADIO_RX_GAIN
ADDRESS = 765432
FUNCTION = 3
MESSAGE = "HELLO GSC WORLD 123"
POCSAG_MESSAGE = "the quick brown fox jumps over the lazy dog 0123456789"
DURATION_S = 15.0


def run_one_protocol(protocol, tx_usrp, tx_streamer, rx_usrp, rx_regs, rx_streamer):
    print(f"\n=== protocol={protocol} ===")
    pages = []

    def on_page(p):
        pages.append(p)
        print(f"  PAGE addr={p.address} func={p.function} message={p.message!r}")

    def on_log(msg):
        print(f"  [rx] {msg}")

    receiver = PocsagReceiver(rx_usrp, rx_regs, rx_streamer, protocol=protocol,
                               on_page=on_page, on_log=on_log)
    # Deliberately NOT passing on_spectrum -- this is the exact condition
    # every headless caller (fskbuddy.py listen/send, a TUI with both
    # panels hidden) already uses. start() should skip stream_cmd/recv()
    # entirely in this mode.
    receiver.start()
    print(f"  receiver._streaming = {receiver._streaming}  (expect False)")

    transmitter = PocsagTransmitter(tx_usrp, tx_streamer, tx_gain=TX_GAIN, on_log=print)
    if protocol == "pocsag":
        transmitter.send(ADDRESS, FUNCTION, POCSAG_MESSAGE, bitrate=1200, repeat=2)
        expected = POCSAG_MESSAGE
    else:
        transmitter.send(ADDRESS, FUNCTION, MESSAGE, protocol="gsc", repeat=2)
        expected = MESSAGE

    time.sleep(3.0)  # let RX catch up via register polling
    receiver.stop()

    ok = any(p.address == ADDRESS and p.message == expected for p in pages)
    print(f"  {len(pages)} page(s) decoded, exact match: {ok}")
    return ok


def main():
    devices = list_devices()
    if len(devices) < 2:
        print(f"Need 2 B200minis, found {len(devices)}: {devices}")
        sys.exit(1)
    tx_serial, rx_serial = devices[0]["serial"], devices[1]["serial"]
    print(f"TX board: {tx_serial}   RX board: {rx_serial}")

    tx_usrp = open_usrp(FREQ, RATE, gain=RX_GAIN, antenna="RX2", tx=True, serial=tx_serial)
    tx_usrp.set_tx_gain(TX_GAIN)
    rx_usrp = open_usrp(FREQ, RATE, gain=RX_GAIN, antenna="RX2", tx=False, serial=rx_serial)
    rx_regs = rx_usrp.get_user_settings_iface(0)

    import uhd
    tx_streamer = tx_usrp.get_tx_stream(uhd.usrp.StreamArgs("fc32", "sc16"))
    rx_streamer = rx_usrp.get_rx_stream(uhd.usrp.StreamArgs("fc32", "sc16"))

    results = {}
    for protocol in ("gsc", "pocsag"):
        results[protocol] = run_one_protocol(protocol, tx_usrp, tx_streamer,
                                              rx_usrp, rx_regs, rx_streamer)
        time.sleep(1.0)

    print("\n=== summary ===")
    for protocol, ok in results.items():
        print(f"  {protocol}: {'PASS' if ok else 'FAIL'}")
    if not all(results.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
