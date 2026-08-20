#!/usr/bin/env python3
"""POCSAG pager transceiver: combined TUI + CLI controller, built on the
PocsagTransceiver/PocsagReceiver/PocsagTransmitter classes in
transceiver.py (the same classes the TUI uses, so `send`/`listen` here are
not a separate implementation -- just a headless front end onto the same
machinery).

`send`/`listen` never stream raw IQ over USB at all -- decode runs
entirely off FPGA register polling (see README's "USB streaming"
section). `tui` gets the same behavior automatically whenever BOTH
--no-spectrum and --no-waterfall are given (there's no raw-sample use left
once both panels are hidden); with either one shown, streaming stays on
as usual, since the spectrum/waterfall display needs real sample content.

--freq is fully configurable and the hardware will transmit on whatever
you set it to -- this tool has no way to know what you're actually
licensed to transmit on. That's the operator's responsibility every time
TX is used; see the README's "Licensing" section before changing it.

Usage:
  python3 pocsag_ctl.py                                    # TUI (default)
  python3 pocsag_ctl.py tui
  python3 pocsag_ctl.py tui --no-spectrum --no-waterfall    # headless-equivalent TUI,
                                                             # no USB IQ streaming
  python3 pocsag_ctl.py send --address 1234567 --alpha "hi there"
  python3 pocsag_ctl.py listen --duration 30 --address 1234567
"""
import argparse
import sys
import time

from transceiver import (
    PocsagTransceiver, DEFAULT_FREQ, DEFAULT_RATE, DEFAULT_TX_GAIN,
    DEFAULT_RX_GAIN, DEFAULT_BITRATE, DEFAULT_DEVIATION_HZ,
)


def cmd_tui(args):
    from pocsag_tui import PocsagTUI
    # tx_gain/rx_gain default to None here (see build_parser) so, unless the
    # user explicitly passes --tx-gain/--rx-gain, PocsagTUI's own defaults
    # apply (see TUI_DEFAULT_TX_GAIN/RX_GAIN in pocsag_tui.py) instead of
    # duplicating that number here.
    kwargs = dict(freq=args.freq, rate=args.rate, bitrate=args.bitrate,
                  deviation_hz=args.deviation, autostart_rx=not args.no_rx,
                  serial=args.serial, show_spectrum=not args.no_spectrum,
                  show_waterfall=not args.no_waterfall)
    if args.tx_gain is not None:
        kwargs["tx_gain"] = args.tx_gain
    if args.rx_gain is not None:
        kwargs["rx_gain"] = args.rx_gain
    app = PocsagTUI(**kwargs)
    app.run()


def cmd_send(args):
    tc = PocsagTransceiver(freq=args.freq, rate=args.rate, tx_gain=args.gain,
                            rx_gain=args.rx_gain, bitrate=args.bitrate,
                            deviation_hz=args.deviation, protocol=args.protocol,
                            on_log=print, serial=args.serial)
    function = 3 if args.alpha is not None else 0
    message = args.alpha if args.alpha is not None else args.numeric
    print(f"Sending ({args.protocol}) to {args.address}, function={function}: {message!r}")
    tc.send(args.address, function, message, bitrate=args.bitrate, blocking=True)
    tc.close()
    print("Done.")


def cmd_listen(args):
    tc = PocsagTransceiver(freq=args.freq, rate=args.rate, tx_gain=args.tx_gain,
                            rx_gain=args.gain, bitrate=args.bitrate,
                            deviation_hz=args.deviation, protocol=args.protocol,
                            on_log=print, serial=args.serial)

    def on_page(page):
        if args.address is None or page.address == args.address:
            print(f"PAGE addr={page.address} func={page.function} "
                  f"type={page.msg_type} message={page.message!r}")

    tc.start_rx(on_page=on_page)
    print(f"Listening for {args.duration:.0f}s (Ctrl-C to stop early)...")
    try:
        time.sleep(args.duration)
    except KeyboardInterrupt:
        pass
    finally:
        tc.close()
    print("Done.")


def add_common_args(sp):
    sp.add_argument("--freq", type=float, default=DEFAULT_FREQ,
                     help="center frequency (Hz) -- only transmit where you're licensed to; "
                          "see the README's Licensing section")
    sp.add_argument("--rate", type=float, default=DEFAULT_RATE, help="host sample rate (Hz)")
    sp.add_argument("--bitrate", type=int, default=DEFAULT_BITRATE, choices=[512, 1200, 2400])
    sp.add_argument("--deviation", type=float, default=DEFAULT_DEVIATION_HZ,
                     help="FSK deviation (Hz) -- see transceiver.py's DEFAULT_DEVIATION_HZ "
                          "docstring for why this may need to be larger than POCSAG's "
                          "standard 4500Hz on some boards")
    sp.add_argument("--serial", default=None,
                     help="target a specific B200mini by serial (required if more than "
                          "one is plugged in -- run uhd_find_devices to list them)")


def build_parser():
    ap = argparse.ArgumentParser(prog="pocsag_ctl.py", description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    tui_p = sub.add_parser("tui", help="launch the interactive TUI (default)")
    add_common_args(tui_p)
    tui_p.add_argument("--tx-gain", type=float, default=None,
                        help="TX gain (dB); defaults to the TUI's own starting point "
                             "(see TUI_DEFAULT_TX_GAIN in pocsag_tui.py) if not given")
    tui_p.add_argument("--rx-gain", type=float, default=None,
                        help="RX gain (dB); defaults to the TUI's own starting point "
                             "(see TUI_DEFAULT_RX_GAIN in pocsag_tui.py) if not given")
    tui_p.add_argument("--no-rx", action="store_true", help="don't auto-start RX on launch")
    tui_p.add_argument("--no-spectrum", action="store_true",
                        help="hide the spectrum scope panel. Combined with --no-waterfall, "
                             "also stops streaming raw IQ over USB entirely (decode keeps "
                             "working via FPGA register polling alone) -- see README's "
                             "'USB streaming' section")
    tui_p.add_argument("--no-waterfall", action="store_true",
                        help="hide the waterfall panel. Combined with --no-spectrum, "
                             "also stops streaming raw IQ over USB entirely -- see above")
    # Note: the underlying FFT (PocsagReceiver._run()) only runs at all if
    # at least one of the two panels is enabled -- passing both flags skips
    # it entirely rather than just hiding empty panels. As of the
    # run_rx_fabric fix (radio_legacy.v) this also means PocsagReceiver
    # skips stream_cmd/recv() entirely in that case (self._streaming =
    # False) -- decode still works, purely off USER_SETTINGS register
    # polling, just without the USB bandwidth cost of a continuous raw IQ
    # stream. send/listen already get this for free -- they never pass
    # on_spectrum at all.
    tui_p.set_defaults(func=cmd_tui)

    send_p = sub.add_parser("send", help="transmit one page and exit")
    add_common_args(send_p)
    send_p.add_argument("--protocol", choices=["pocsag", "gsc"], default="pocsag",
                         help="paging protocol to transmit (default: pocsag). GSC's bitrate "
                              "is fixed at 600bps regardless of --bitrate -- see gsc_framer.v.")
    send_p.add_argument("--address", type=int, required=True, help="21-bit pager capcode")
    msg = send_p.add_mutually_exclusive_group(required=True)
    msg.add_argument("--alpha", help="alphanumeric message text")
    msg.add_argument("--numeric", help="numeric message (digits + " + p_numeric_chars() + ")")
    send_p.add_argument("--gain", type=float, default=DEFAULT_TX_GAIN, help="TX gain (dB)")
    send_p.add_argument("--rx-gain", type=float, default=DEFAULT_RX_GAIN,
                         help="RX gain (dB) -- unused for send, kept so the same device "
                              "args work for tui/send/listen uniformly")
    send_p.set_defaults(func=cmd_send)

    listen_p = sub.add_parser("listen", help="listen and print decoded pages until duration elapses")
    add_common_args(listen_p)
    listen_p.add_argument("--protocol", choices=["pocsag", "gsc"], default="pocsag",
                           help="paging protocol to receive (default: pocsag). GSC's bitrate "
                                "is fixed at 600bps regardless of --bitrate -- see gsc_framer.v.")
    listen_p.add_argument("--gain", type=float, default=DEFAULT_RX_GAIN, help="RX gain (dB)")
    listen_p.add_argument("--tx-gain", type=float, default=DEFAULT_TX_GAIN,
                           help="TX gain (dB) -- unused for listen, see --rx-gain note above")
    listen_p.add_argument("--duration", type=float, default=30.0, help="seconds to listen")
    listen_p.add_argument("--address", type=int, default=None,
                           help="only print pages for this capcode")
    listen_p.set_defaults(func=cmd_listen)

    return ap, tui_p


def p_numeric_chars():
    import pocsag as p
    return p.NUMERIC_CHARS.strip()


def main():
    ap, tui_p = build_parser()
    argv = sys.argv[1:]
    if not argv or argv[0] not in ("tui", "send", "listen", "-h", "--help"):
        argv = ["tui"] + argv
    args = ap.parse_args(argv)

    # Resolve which physical board to use, once, before dispatching to
    # tui/send/listen -- auto-picks if there's only one, prompts (numbered
    # list) if there's more than one connected, unless --serial was already
    # given explicitly.
    from pocsag_modem import select_device_interactive
    args.serial = select_device_interactive(args.serial)

    args.func(args)


if __name__ == "__main__":
    main()
