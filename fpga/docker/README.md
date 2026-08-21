# B200mini FPGA build environment (Xilinx ISE 14.7 in Docker)

The B200mini's FPGA is a Spartan-6 (XC6SLX75). Vivado never supported
Spartan-6, so the only toolchain that can build for it is Xilinx ISE 14.7 --
the "ancient IDE." This wraps it in Docker so it doesn't have to touch the
host directly.

## 1. Install Docker (host, one-time)

```
sudo apt install docker.io
sudo usermod -aG docker $USER   # then fully log out/in -- group membership
                                 # is fixed at login and won't refresh in an
                                 # already-running shell
```

## 2. Get the ISE 14.7 installer + a free WebPACK license (you, not me)

Both are gated behind an AMD/Xilinx account -- I can't fetch either.

1. Create/log into an account at xilinx.com (now AMD).
2. Download "ISE Design Suite 14.7 Full Installer" for Linux
   (`Xilinx_ISE_DS_Lin_14.7_1015_1.tar`, ~6.5 GB).
3. Extract it and put the resulting directory in `docker/installer/`
   (`docker/installer/Xilinx_ISE_DS_Lin_14.7_1015_1/`). A bare `.tar` there
   also works -- `install.sh` extracts it itself.
4. Generate a free **ISE WebPACK** license from Xilinx's licensing portal.
   WebPACK is free but still requires a license file for the `map` (place)
   step -- `xst`/synthesis runs without one, which is easy to mistake for
   "no license needed" (I made exactly that mistake once here). Node-lock
   it to this host's real Ethernet MAC, not a container's ephemeral one --
   check with `ip link show` (look for the `UP` interface, not `lo` or
   `docker0`). Save the license as `docker/xilinx.lic`.

## 3. Build the image

```
./docker/build_image.sh
```

Installs ISE unattended (Debian 9/stretch base -- see "Base image" below)
and runs `make B200mini PROJECT_ONLY=1` implicitly via normal `make`
afterwards. Expect ~10 minutes and note the disk usage below.

### Base image

Ubuntu 16.04/18.04 (the usual ISE 14.7 recommendation) have both been fully
pruned from `old-releases.ubuntu.com` as of 2026 -- neither `xenial/` nor
`bionic/` exist there anymore. This uses **Debian 9 (stretch)** via
`archive.debian.org` instead, which is still fully served and is a commonly
reported working base for ISE 14.7's old libstdc++/ncurses ABI.

### Disk usage

The installed image is ~26GB. The installer payload (~13GB) is brought in
via a build-time bind mount (`RUN --mount=type=bind`), not `COPY` -- a
`COPY` bakes that payload into a permanent layer even if you `rm -rf` it in
a later `RUN` (union filesystem layers don't reclaim space across
instructions, only within the same one). Budget **40GB+ free disk** before
building, and run `docker builder prune -af` if a build fails with "no
space left on device" -- stale build-cache layers add up fast at this
scale.

### Known gotchas (already fixed here, kept for context)

- **Real unattended-install entry point is `bin/lin64/batchxsetup`, not
  `xsetup`.** The Xilinx docs describe a `xsetup -b ConfigGen`/`-b Install`
  flow; this installer build's `xsetup` only exposes
  `--help`/`--uninstall`/`--copy_registry` and silently falls back to a
  GUI wizard (which then hangs forever in a headless container) for
  anything else. `batchxsetup --samplebatchscript <file>` and
  `--batch <file>` are the real flags (found via `objdump -p` / `strings`
  on the binary, not from docs).
- **`yes Y | batchxsetup ...` under `pipefail`**: `yes` gets SIGPIPE (exit
  141) once the installer stops reading stdin, which `pipefail` turns into
  a false failure even though the install itself succeeded. `install.sh`
  checks `batchxsetup`'s own exit status via `PIPESTATUS` instead.
- **`entrypoint.sh` sourcing `settings64.sh` silently did nothing.**
  `settings64.sh` checks `$#` to decide whether it was given an explicit
  install path; sourced from `entrypoint.sh` it inherits entrypoint's own
  leftover `"$@"` (e.g. `bash -c ...`) and misreads `"bash"` as the install
  path, so nothing gets sourced and `PATH` stays untouched with no error.
  Fixed by passing the path explicitly:
  `source .../settings64.sh /opt/Xilinx/14.7/ISE_DS`.
- Motif (`libXm.so.4`) isn't needed for the actual `make B200mini` flow in
  practice -- if you hit it, it's likely `impact`/`cs`, which this build
  doesn't use.

## 4. Build the B200mini bitstream

```
./docker/build_fpga.sh
```

Runs `make B200mini` against `../uhd/fpga/usrp3/top/b2xxmini` inside the
container (`--network host`, so the container's Ethernet MAC matches what
the license is bound to). Output lands in
`uhd/fpga/usrp3/top/b2xxmini/build/usrp_b200mini_fpga.bin` (+ `.bit`, plus
`.syr`/`.twr`/`.rpt` reports).

### Repo checkout matters -- don't build from `uhd` HEAD

The `uhd` git HEAD's `fpga/usrp3/lib/fifo/axi_fifo_2clk.v` uses an XPM
(Xilinx Parameterized Macro) primitive that didn't exist until
Vivado-era libraries -- ISE 14.7 (2013) can't synthesize it and XST fails
with `Instantiating <impl_xpm_i> from unknown module <fifo_xpm_2clk>`.
Check out tag **`v4.9.0.1`** (matches the UHD host driver version already
installed) instead, which predates that commit:

```
cd uhd && git checkout v4.9.0.1
```

### Known Makefile bug (patched in this checkout)

`fpga/usrp3/top/Makefile.common`'s `bin` target depends on both
`$(BIN_FILE)` and `$(BIT_FILE)`, but only ever defines a recipe for
`$(BIN_FILE)` -- a clean `make B200mini` errors with `No rule to make
target '.../b205.bit'`. In reality the `BIN_FILE` recipe's "Generate
Programming File" ISE step produces both `.bin` and `.bit` as real outputs
of the same bitgen run. `fpga/usrp3/top/b2xxmini/Makefile.b205.inc` has a
one-line fix appended right after `include ../Makefile.common`:
`$(BIT_FILE): $(BIN_FILE)` (dependency only, no separate recipe -- adding
a real recipe there risks running the expensive bitgen step twice).

Don't pre-`touch` `.bit` as a workaround instead -- it fools `make`'s
staleness check into thinking a subsequent *real* build is already done
and skips it silently. (Learned that one the hard way; if a rebuild
finishes suspiciously fast and produces a 0-byte `.bin`, that's why --
`rm -rf build-B200mini/` and rebuild.)

A synthesis+PAR+bitgen run takes on the order of tens of minutes;
run it detached (`docker run -d ...`) rather than attached if driving it
from a script, since a single foreground command may time out long before
the build does.

## 5. Flash it

Once you have a `.bin`, flashing to the device is done from the *host*
UHD tools (not the container -- it just needs USB access to the B200mini),
e.g. `uhd_image_loader --args="type=b200,fpga-path=<path>.bin"`. Do this
only after confirming `uhd_find_devices` sees the device again -- flashing
mid-dropout risks bricking the FPGA image.
