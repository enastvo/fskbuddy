# FSK Buddy FPGA image

- **`b205.bit`** -- the custom FPGA bitstream for the USRP B200mini. This
  is what `modem.py` loads by default -- nothing else in this directory
  is needed to run FSK Buddy.

## Using it (the common case -- no flashing required)

`modem.py` looks for `fpga/b205.bit` (right here, relative to the repo
root) by default and hands it to UHD's own `fpga=` device argument every
time it opens the USRP. This is a **transient load**: UHD pushes the
bitstream over USB into the FPGA's volatile configuration memory fresh
at the start of every session. Nothing on the device's own persistent
storage is touched, and there's nothing to flash for normal use -- just
run FSK Buddy normally (see the main README) with the board plugged in.

Using a bitstream from somewhere else instead? Set `FSKBUDDY_FPGA_BIN` to
point at it rather than the shipped one.

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

## Source availability (GPLv3)

This bitstream is built from modified USRP `radio_200` FPGA RTL (custom
paging-protocol PHY modules -- see the main README's "Architecture"
section for what they do) on top of upstream UHD. The source itself
isn't included in this distribution. Per GPLv3 section 6(b), this is a
written offer: the corresponding source for `b205.bit` is available on
request -- open an issue on this repo, or contact the copyright holder
listed in the main README's "Software license" section -- for at least
three years from this release.
