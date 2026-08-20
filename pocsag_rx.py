#!/usr/bin/env python3
"""POCSAG pager receiver. Bit synchronization and batch/frame sync are done
entirely in FPGA fabric (pocsag_bitsync.v + pocsag_framer.v, fed by the
existing fsk_demod discriminator) -- this program just configures the PHY
registers, polls for freshly-captured raw codewords, and does BCH decode +
address/message assembly in software.

RX draining and register polling both happen in this single thread -- see
pocsag_modem's module docstring: splitting them across separate threads
(even just those two, no TX involved) deadlocks this device.

Usage:
  python3 pocsag_rx.py --bitrate 1200 --duration 10
  python3 pocsag_rx.py --bitrate 1200 --address 1234567   # only print pages for this capcode
"""
import argparse
import time
import numpy as np
import uhd

import pocsag as p
from pocsag_modem import open_usrp, REG_POCSAG_CTRL, RB_POCSAG_STATUS


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bitrate", type=int, default=1200, choices=[512, 1200, 2400])
    ap.add_argument("--freq", type=float, default=929.6625e6)
    ap.add_argument("--gain", type=float, default=35.0)
    ap.add_argument("--rate", type=float, default=1.024e6, help="host sample rate (Hz)")
    ap.add_argument("--duration", type=float, default=30.0)
    ap.add_argument("--address", type=int, default=None, help="only show pages for this capcode")
    ap.add_argument("--alpha-function", type=int, default=3)
    args = ap.parse_args()

    usrp = open_usrp(args.freq, args.rate, args.gain, antenna="RX2", tx=False)
    print(f"RX {usrp.get_rx_freq()/1e6:.4f} MHz @ {usrp.get_rx_gain()} dB, "
          f"rate {usrp.get_rx_rate()/1e3:.1f} kHz")

    sps = round(usrp.get_rx_rate() / args.bitrate)
    actual_bitrate = usrp.get_rx_rate() / sps
    print(f"POCSAG {args.bitrate}bps -> sps={sps} (actual {actual_bitrate:.1f}bps)")

    regs = usrp.get_user_settings_iface(0)
    ctrl_value = (1 << 16) | sps
    regs.poke32(REG_POCSAG_CTRL * 4, ctrl_value)

    rx_streamer = usrp.get_rx_stream(uhd.usrp.StreamArgs("fc32", "sc16"))
    stream_cmd = uhd.types.StreamCMD(uhd.types.StreamMode.start_cont)
    stream_cmd.stream_now = True
    rx_streamer.issue_stream_cmd(stream_cmd)

    parser = p.LiveParser(alpha_function=args.alpha_function)
    n_codewords = 0
    n_pages = 0
    last_count = None
    was_locked = False

    print(f"Listening for {args.duration:.0f}s...")
    buf = np.zeros(rx_streamer.get_max_num_samps(), dtype=np.complex64)
    md = uhd.types.RXMetadata()
    deadline = time.monotonic() + args.duration
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
                # 8-bit wraparound-safe "how many new codewords" count
                n_new = (count - last_count) & 0xFF
                if n_new > 1:
                    print(f"  [warning: missed {n_new - 1} codeword(s) -- polling too slow "
                          f"or codewords arriving faster than expected]")
                last_count = count
                n_codewords += 1

                page = parser.feed(codeword)
                if page is not None:
                    n_pages += 1
                    if args.address is None or page.address == args.address:
                        print(f"  PAGE addr={page.address} func={page.function} "
                              f"type={page.msg_type} message={page.message!r}")
    finally:
        stream_cmd = uhd.types.StreamCMD(uhd.types.StreamMode.stop_cont)
        rx_streamer.issue_stream_cmd(stream_cmd)
        regs.poke32(REG_POCSAG_CTRL * 4, 0)

    print(f"Done. {n_codewords} codewords captured, {n_pages} page(s) decoded.")


if __name__ == "__main__":
    main()
