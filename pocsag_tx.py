#!/usr/bin/env python3
"""POCSAG pager transmitter. Encodes an address+message into a standard
POCSAG batch (BCH-encoded, preamble + sync + codewords) and transmits it
as 2-FSK out the TRX port. The modulation itself runs on the host (see
pocsag_modem.modulate_cpfsk) -- the DUC/DAC/mixer that actually puts it on
the air is FPGA/RFIC hardware either way.

Usage:
  python3 pocsag_tx.py --address 1234567 --alpha "hello world"
  python3 pocsag_tx.py --address 1234567 --numeric "18005551234"
"""
import argparse
import time
import numpy as np
import uhd

import pocsag as p
from pocsag_modem import open_usrp, modulate_cpfsk


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--address", type=int, required=True, help="21-bit pager capcode")
    msg = ap.add_mutually_exclusive_group(required=True)
    msg.add_argument("--alpha", help="alphanumeric message text")
    msg.add_argument("--numeric", help="numeric message (digits + " + p.NUMERIC_CHARS.strip() + ")")
    ap.add_argument("--function", type=int, default=None, help="override function bits (0-3)")
    ap.add_argument("--bitrate", type=int, default=1200, choices=[512, 1200, 2400])
    ap.add_argument("--freq", type=float, default=929.6625e6)
    ap.add_argument("--gain", type=float, default=15.0)
    ap.add_argument("--rate", type=float, default=1.024e6, help="host sample rate (Hz)")
    args = ap.parse_args()

    if args.alpha is not None:
        function = args.function if args.function is not None else 3
        msg_cws = p.encode_alpha(args.alpha)
        preview = args.alpha
    else:
        function = args.function if args.function is not None else 0
        msg_cws = p.encode_numeric(args.numeric)
        preview = args.numeric

    bits = p.build_bitstream(args.address, function, msg_cws)
    sps = round(args.rate / args.bitrate)
    actual_bitrate = args.rate / sps
    print(f"Address {args.address}, function {function}, {len(bits)} bits "
          f"({len(bits)/args.bitrate*1000:.1f} ms @ {args.bitrate}bps, sps={sps})")
    print(f"Message: {preview!r}")

    iq = modulate_cpfsk(bits, sps, args.rate)

    usrp = open_usrp(args.freq, args.rate, gain=0, antenna="RX2", tx=True)
    usrp.set_tx_gain(args.gain)
    print(f"TX {usrp.get_tx_freq()/1e6:.4f} MHz @ {usrp.get_tx_gain()} dB, "
          f"rate {usrp.get_tx_rate()/1e3:.1f} kHz")

    tx_streamer = usrp.get_tx_stream(uhd.usrp.StreamArgs("fc32", "sc16"))
    md = uhd.types.TXMetadata()
    md.start_of_burst = True
    md.end_of_burst = False
    md.has_time_spec = False
    tx_streamer.send(iq, md)
    md2 = uhd.types.TXMetadata()
    md2.end_of_burst = True
    tx_streamer.send(np.zeros(1, dtype=np.complex64), md2)
    time.sleep(0.3)
    print("Sent.")


if __name__ == "__main__":
    main()
