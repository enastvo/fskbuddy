# FSK Buddy FPGA image

This directory ships everything needed to run FSK Buddy against a real
B200mini: the pre-built bitstream, and the complete corresponding source
for it (required for GPLv3 compliance whenever the binary bitstream is
distributed -- see the repo's top-level `LICENSE`), so nothing here
depends on a separate checkout.

- **`b205.bit`** -- the pre-built bitstream. This is what `modem.py`
  loads by default (see "Using it" below) -- most people need nothing
  else in this directory at all.
- **`radio_200.patch`** -- this project's entire FPGA-side contribution,
  as a single patch against upstream UHD, applies cleanly on top of tag
  **`v4.9.0.1`** (commit `9ec1f5823`) of
  [EttusResearch/uhd](https://github.com/EttusResearch/uhd). That's the
  exact, complete corresponding source for `b205.bit`: 14 files, all
  listed in the patch header, adding the custom `radio_200` PHY modules
  (`fsk_demod.v`, `pocsag_bitsync.v`/`pocsag_framer.v`,
  `gsc_framer.v`, `pocsag_channel_filter.v`, `clip_detect.v`,
  `channel_width_detect.v`) plus the `radio_legacy.v`/build-config wiring
  to bring them in. See the main README's "Architecture" section for
  what each module actually does.
- **`src/`** -- the same changes as `radio_200.patch`, but as plain files
  (mirroring their real path under a `uhd/` checkout) for browsing
  without needing to apply anything.
- **`docker/`** -- the Xilinx ISE 14.7-in-Docker build environment used
  to actually turn that source into `b205.bit` (`docker/README.md` here
  has the full one-time setup: installing Docker, getting your own free
  Xilinx WebPACK license -- gated behind their own account, not
  something this project can redistribute -- building the ISE container
  image, then `build_fpga.sh`). Not needed at all just to *use*
  `b205.bit`; only relevant if you want to modify the RTL and rebuild.

## Using it (the common case -- no flashing required)

`modem.py` looks for `fpga/b205.bit` (right here, relative to the repo
root) by default and hands it to UHD's own `fpga=` device argument every
time it opens the USRP. This is a **transient load**: UHD pushes the
bitstream over USB into the FPGA's volatile configuration memory fresh
at the start of every session. Nothing on the device's own persistent
storage is touched, and there's nothing to "flash" for normal use --
just run FSK Buddy normally (see the main README) with the board plugged
in.

Building from a different location? Set `FSKBUDDY_FPGA_BIN` to point at
your own `.bit` file instead of the shipped one.

## Optional: permanently flashing it to the device

Only relevant if you want the device to come up running this custom
image *without* FSK Buddy specifying `fpga=` -- e.g. so other UHD tools
default to it too. This is a real, persistent write to the B200mini's
own onboard flash, unlike the transient load above.

```
uhd_find_devices                 # confirm the device is healthy first --
                                  # don't flash mid-dropout
uhd_image_loader --args="type=b200" --fpga-path=fpga/b205.bit
```

(`--args`/`--fpga-path` syntax confirmed against this project's own
`uhd_image_loader --help` output -- not something exercised end-to-end
against real hardware in this project's own testing, since everything
so far has used the transient `fpga=` load above; verify independently
before relying on it.)

**To go back to a stock image later**, use `uhd_images_downloader` to
fetch Ettus's official B200mini image, then `uhd_image_loader` again
pointing at that instead. Don't interrupt a flash in progress (power
loss or USB disconnection mid-write is the real risk here, not the
custom image itself).

## Rebuilding from source

1. `git clone https://github.com/EttusResearch/uhd.git && cd uhd && git checkout v4.9.0.1`
2. `git apply /path/to/fskbuddy/fpga/radio_200.patch`
3. Follow `docker/README.md` (in this directory) for the ISE-in-Docker
   build environment, then `docker/build_fpga.sh`.
4. Copy the resulting `.bit` back into `fskbuddy/fpga/b205.bit` (or point
   `FSKBUDDY_FPGA_BIN` at it directly).
