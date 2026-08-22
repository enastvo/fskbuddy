# FSK Buddy FPGA image

- **`b205.bit`** -- the custom FPGA bitstream for the USRP B200mini, with
  the standard Xilinx `.bit` header. This is what `modem.py` loads by
  default -- nothing else in this directory is needed to run FSK Buddy.
- **`b205.bin`** -- the same bitstream without the `.bit` header (107
  bytes smaller, otherwise identical configuration data). Included only
  for `uhd_image_loader` users who specifically want it -- see below.

## Using it (the common case)

`modem.py` looks for `fpga/b205.bit` (right here, relative to the repo
root) by default and hands it to UHD's own `fpga=` device argument every
time it opens the USRP. Just run FSK Buddy normally (see the main
README) with the board plugged in -- nothing else to do.

Using a bitstream from somewhere else instead? Set `FSKBUDDY_FPGA_BIN` to
point at it rather than the shipped one.

## How this actually gets onto the FPGA (important correction)

**The B200mini has no persistent flash for the FPGA image.** Checked
directly in UHD's own source (`b200_iface.cpp`'s `load_fpga()`): it
opens the given file and streams its raw bytes over USB straight into
the FPGA's *volatile* configuration memory -- no EEPROM or flash
involved. That's the exact same function whether it's called from a
normal FSK Buddy session (via the `fpga=` device argument above) or from
the standalone `uhd_image_loader` CLI tool -- so `uhd_image_loader`
doesn't make anything persistent either; it's just a one-off way to
trigger the same transient load outside of running FSK Buddy itself
(e.g. to sanity-check the image, or pre-load it before another tool).
**The device reverts to needing an image pushed again after every power
cycle, regardless of which tool loaded it last** -- there's nothing to
"flash" here in the sense of a lasting hardware change, and an earlier
draft of this file incorrectly implied otherwise.

If you want a UHD session that *doesn't* pass `fpga=` to still pick up
this custom image (e.g. for other UHD tools), the only way is to replace
the file UHD's own default image lookup finds in its standard images
directory (typically wherever `uhd_images_downloader` installs to) with
this one -- a host-side file swap, not a device operation. Not something
this project sets up automatically, since it'd affect every UHD session
on the machine, not just FSK Buddy's.

To trigger the transient load standalone, outside of FSK Buddy, first
confirm the device is healthy:

```
uhd_find_devices
```

Then load whichever file you prefer -- both go through the identical
`load_fpga()` code path in UHD, so there's no functional difference
between them, just the 107-byte `.bit` header:

```
# Using the .bit file (has the Xilinx header):
uhd_image_loader --args="type=b200" --fpga-path=fpga/b205.bit

# Using the .bin file (raw config data, no header) -- equivalent:
uhd_image_loader --args="type=b200" --fpga-path=fpga/b205.bin
```

Both commands were run back-to-back against real hardware as part of
this project's own testing (immediately re-probed with a register-level
loopback + decode check, not just watching the log output) and behaved
identically: the load succeeds either way, and -- consistent with
"How this actually gets onto the FPGA" above -- neither survives a
power cycle. Use whichever format you have on hand; there's no reason
to prefer one over the other for this command specifically.

## Telling which image is actually running

Because none of the above is persistent, and because UHD's own
"Loading FPGA image" log line turned out to be an unreliable way to
tell (it's driven by a host-tracked hash, not a fresh read of the chip
-- confirmed on real hardware to stay silent even when a *different*
image was actually configured), this build carries its own hardware
identity marker: `RB_PHY_STATUS`'s top 32 bits (`radio_legacy.v`) hold a
fixed magic value + version, checked by `PocsagReceiver.start()` in
`transceiver.py` every time RX starts. The TUI's STATUS panel shows the
result directly (`FSK Buddy v0x0001` in green, or a red
`UNRECOGNIZED -- wrong image!`); the CLI logs the same check at startup.
See the main README's "The TUI panels, in detail" section for more.

## Source availability (GPLv3)

This bitstream is built from modified USRP `radio_200` FPGA RTL (custom
paging-protocol PHY modules -- see the main README's "Architecture"
section for what they do) on top of upstream UHD. The source itself
isn't included in this distribution. Per GPLv3 section 6(b), this is a
written offer: the corresponding source for `b205.bit`/`b205.bin` is
available on request -- open an issue on this repo, or contact the
copyright holder listed in the main README's "Software license" section
-- for at least three years from this release.
