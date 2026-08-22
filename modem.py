#!/usr/bin/env python3
# Copyright (C) 2026 Estefan Nastvogel
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared UHD/RF plumbing for the POCSAG TX and RX programs.

Threading note: this device hangs (not just slows down -- a hard deadlock,
confirmed with diagnostic prints showing not one of three threads completes
even a single call) when three threads concurrently hit it: a TX send()
loop, an RX recv() loop, and peek64/poke32 register I/O, each in their own
thread. Two threads is fine (proven: TX send() loop in one thread, RX
recv() interleaved with peek64/poke32 in the main thread/one other
thread). So RX draining and register polling must always happen from the
*same* thread here -- see poll_loop() below -- with TX (if any) in its own
separate thread."""
import os
from pathlib import Path

import numpy as np
import uhd

# The custom FPGA bitstream -- see fpga/README.md for what's in that
# directory and how to rebuild it from source. Ships pre-built right in
# this repo (fpga/b205.bit, relative to this file's own location, not a
# hardcoded absolute path) so nothing external is required for the
# common case. Override with FSKBUDDY_FPGA_BIN to point at a different
# build (e.g. one you rebuilt yourself elsewhere).
_DEFAULT_FPGA_BIN = Path(__file__).resolve().parent / "fpga/b205.bit"
FPGA_BIN = os.environ.get("FSKBUDDY_FPGA_BIN", str(_DEFAULT_FPGA_BIN))

# Standard POCSAG deviation.
DEVIATION_HZ = 4500.0

# my_addr values in radio_legacy.v's USER_SETTINGS block.
REG_POCSAG_CTRL = 3     # poke32(3*4, {bit16:enable, bits15:0:sps})
RB_POCSAG_STATUS = 2    # peek64(2*8) -> {23'b0, locked, count[7:0], codeword[31:0]}
REG_GSC_CTRL = 4        # poke32(4*4, {bit16:enable, bits15:0:sps}) -- same layout as
                         # REG_POCSAG_CTRL, a second independent bitsync/framer chain
                         # (gsc_bitsync/gsc_framer) running concurrently in fabric off the
                         # same fsk_demod bit stream; see gsc_framer.v.
RB_GSC_STATUS = 3       # peek64(3*8) -> {9'b0, locked, pair_count[7:0], word1[22:0],
                         # word2[22:0]}. word1/word2 are gsc_framer.v's most recently
                         # captured raw Golay(23,12,7) codewords (golay.golay_decode()
                         # does the FEC in host software, same PHY/framing-only split
                         # pocsag_framer/RB_POCSAG_STATUS uses). pair_count is
                         # free-running, diffed the same way as POCSAG's codeword count.
RB_PHY_STATUS = 4       # peek64(4*8) -> protocol-agnostic PHY status:
                         # {magic[15:0], version[15:0], width_narrow[1], width_locked[1],
                         # clip_count[7:0], 22'b0}. magic/version are a hardware-level
                         # identity marker (radio_legacy.v's FSKB_MAGIC/FSKB_VERSION) --
                         # confirms this specific custom image is actually configured on
                         # the FPGA right now, rather than inferring it from which file
                         # was asked for at load time (UHD's own "Loading FPGA image" log
                         # line can go quiet even when the wrong image is running --
                         # confirmed on real hardware, not theoretical -- since it's based
                         # on a host-tracked hash, not a fresh read of the chip). See
                         # FSKB_MAGIC/FSKB_VERSION below. clip_count is clip_detect.v's
                         # free-running counter, diffed the
                         # same way as codeword count above. width_narrow/width_locked are
                         # channel_width_detect.v's classification (1=12.5kHz-style
                         # narrowband, 0=25kHz-style wideband; see there for the
                         # empirical-deviation-measurement approach and calibration data).

# Must match radio_legacy.v's own FSKB_MAGIC/FSKB_VERSION localparams
# exactly -- these are the expected values, not computed from anything;
# a real device that reads back something else is running a different
# (older, newer, or entirely unrelated) FPGA image.
FSKB_MAGIC = 0xF5CB
FSKB_VERSION = 0x0001


def list_devices():
    """Enumerate connected B200-family USRPs. This is UHD's own discovery
    (uhd.find(), the same mechanism the `uhd_find_devices` CLI tool wraps)
    returned as data instead of printed -- no external library needed, no
    shelling out to the CLI tool."""
    results = uhd.find("type=b200")
    return [{"serial": r.get("serial"), "name": r.get("name"), "product": r.get("product")}
            for r in results]


def select_device_interactive(serial=None):
    """Resolve which physical B200mini to use. If `serial` is already
    given, returns it unchanged (no discovery needed -- e.g. --serial was
    passed explicitly). Otherwise: silently picks the one device found if
    there's only one, prompts interactively (numbered list, like
    `uhd_find_devices` but with a selection) if there's more than one, and
    raises a clear error if none are found."""
    if serial:
        return serial

    devices = list_devices()
    if not devices:
        raise RuntimeError("No B200mini devices found. Check connections/power -- "
                            "uhd_find_devices should list at least one.")
    if len(devices) == 1:
        d = devices[0]
        print(f"Using the only B200mini found: serial={d['serial']}")
        return d["serial"]

    print(f"{len(devices)} B200mini devices found:")
    for i, d in enumerate(devices, 1):
        print(f"  [{i}] serial={d['serial']}  product={d.get('product') or '?'}")
    while True:
        try:
            choice = input(f"Select device [1-{len(devices)}]: ").strip()
        except EOFError:
            raise RuntimeError("No device selected (stdin closed) -- pass --serial explicitly.")
        if choice.isdigit() and 1 <= int(choice) <= len(devices):
            return devices[int(choice) - 1]["serial"]
        print(f"Enter a number from 1 to {len(devices)}.")


def open_usrp(freq, rate, gain, antenna, tx=False, serial=None):
    if not os.path.isfile(FPGA_BIN):
        raise FileNotFoundError(
            f"Custom FPGA bitstream not found at {FPGA_BIN!r}. It should have shipped "
            f"at fpga/b205.bit in this repo -- see fpga/README.md. If you moved/rebuilt "
            f"it elsewhere, set FSKBUDDY_FPGA_BIN to point at wherever it actually is.")
    args = f"type=b200,fpga={FPGA_BIN},enable_user_regs"
    if serial:
        args += f",serial={serial}"
    usrp = uhd.usrp.MultiUSRP(args)
    usrp.set_rx_rate(rate)
    usrp.set_rx_freq(uhd.types.TuneRequest(freq))
    usrp.set_rx_gain(gain)
    usrp.set_rx_antenna(antenna)
    if tx:
        usrp.set_tx_rate(rate)
        usrp.set_tx_freq(uhd.types.TuneRequest(freq))
    return usrp


def modulate_cpfsk(bits, sps, rate, deviation_hz=DEVIATION_HZ):
    """bits: list/array of 0/1. Continuous-phase 2-FSK, sps samples/bit."""
    dev = np.where(np.asarray(bits) == 1, deviation_hz, -deviation_hz)
    freq_samples = np.repeat(dev, sps).astype(np.float64)
    phase = 2 * np.pi * np.cumsum(freq_samples) / rate
    return np.exp(1j * phase).astype(np.complex64)


def poll_loop(rx_streamer, regs, duration_s, on_status, recv_timeout=0.3):
    """Drains RX (required to keep strobe_rx pulsing) and polls the POCSAG
    status register, both from this one thread/call -- see the module
    docstring for why that matters. on_status(status_u64) is called after
    every recv(); loops until duration_s elapses."""
    import time
    buf = np.zeros(rx_streamer.get_max_num_samps(), dtype=np.complex64)
    md = uhd.types.RXMetadata()
    deadline = time.monotonic() + duration_s
    while time.monotonic() < deadline:
        rx_streamer.recv(buf, md, timeout=recv_timeout)
        status = regs.peek64(RB_POCSAG_STATUS * 8)
        on_status(status)
