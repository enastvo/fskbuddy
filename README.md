# FSK Buddy

A POCSAG + GSC pager transmitter/receiver for the USRP B200mini. Real-time
bit sync, batch/frame sync, and channel filtering run in a custom FPGA
image, decode/FEC and message assembly run in Python, and a Textual TUI
(or a plain CLI) sits on top -- listen for pages on air, or send your own.

**Only transmit on a frequency you're actually licensed to use.** This
tool has no way to know what you're authorized for -- see "Licensing"
near the bottom before you hit TX.

## Quick start

### Installation

**1. System dependencies** (Ubuntu/Debian; tested on Ubuntu 26.04):

```
sudo apt update
sudo apt install uhd-host python3-uhd libuhd-dev python3-numpy
```

This installs the UHD driver, its Python bindings, and numpy, all
system-wide -- no venv needed for these three. `uhd-host` also installs
the udev rules needed for non-root USB access to the B200mini; unplug and
replug the board (or `sudo udevadm control --reload-rules`) if it was
already connected when you installed.

**2. This repo, plus a sibling FPGA source tree:**

```
git clone git@github.com:enastvo/fskbuddy.git
```

FSK Buddy talks to stock UHD APIs (`enable_user_regs`,
`get_user_settings_iface()`) -- the host-side driver from step 1 doesn't
need to be custom. What *does* need to be custom is the FPGA bitstream
itself (see "Architecture" below for exactly which modules are added on
top of the stock b2xxmini image). This repo expects a sibling `uhd/`
checkout -- carrying this project's own `radio_200` RTL additions, built
into a `.bit` file -- living right next to `fskbuddy/` under one parent
directory (a separate `docker/` sibling holds the Xilinx ISE build
environment that actually produces that bitstream, see below):

```
some-parent-dir/
├── fskbuddy/   (this repo)
├── uhd/        (the FPGA source tree, with this project's RTL patches, built)
└── docker/     (the Xilinx ISE 14.7-in-Docker build environment for uhd/'s bitstream)
```

`modem.py` looks for the built bitstream at
`uhd/fpga/usrp3/top/b2xxmini/build-B200mini/b205.bit` relative to that
parent directory by default; set the `FSKBUDDY_FPGA_BIN` environment
variable to point elsewhere if your layout differs. Building that
bitstream needs Xilinx ISE 14.7 (a licensed, discontinued Xilinx tool that
only runs containerized -- see `docker/README.md` for the full one-time
setup: installing Docker, getting a free Xilinx WebPACK license, building
the ISE container image, then `docker/build_fpga.sh`) and isn't part of
*this* repo's own install -- `fskbuddy.py`/the TUI will raise a clear,
specific error naming the path it expected if the bitstream isn't there
yet, rather than fail confusingly deep inside UHD.

**3. Python venv, for the TUI only:**

```
cd fskbuddy
python3 -m venv --system-site-packages .venv
.venv/bin/pip install textual
```

`--system-site-packages` lets the venv see the system-installed
`uhd`/`numpy` from step 1 rather than needing its own copies (the system
Python itself is externally-managed / Debian-policy-locked, hence the
venv at all). `fskbuddy.py send`/`listen` and the test scripts don't need
this venv -- they only need `numpy` + `uhd`, already installed system-wide
in step 1; only the `tui` subcommand needs `textual`.

**Verify the install** (no hardware or FPGA bitstream needed for this part):

```
python3 -c "import uhd, numpy; print('uhd', uhd.__file__, '/ numpy', numpy.__version__)"
.venv/bin/python3 fskbuddy.py --help
python3 fskbuddy.py send --help
```

If those all print/run cleanly, the Python side of the install is good;
`fskbuddy.py send`/`listen`/`tui` will still need a real B200mini plugged
in (and its custom bitstream built, per step 2) to actually do anything.

### Running it

```
.venv/bin/python3 fskbuddy.py                 # TUI (default)
.venv/bin/python3 fskbuddy.py tui --no-rx      # TUI, don't auto-start RX
.venv/bin/python3 fskbuddy.py tui --no-spectrum --no-waterfall  # hide both panels *and*
                                                                 # stop streaming raw IQ
                                                                 # over USB (see below)

python3 fskbuddy.py send --address 1234567 --alpha "hello world"
python3 fskbuddy.py send --address 1234568 --numeric "18005551234"
python3 fskbuddy.py send --address 1234567 --alpha "hi there" --protocol gsc
python3 fskbuddy.py listen --duration 30 --address 1234567
```

`--freq`, `--rate`, `--bitrate` (512/1200/2400), `--deviation`, `--protocol`
(`pocsag`/`gsc`) are available on all three subcommands; TX/RX gain flags
differ slightly by subcommand (see `--help`).

`send`/`listen` never stream raw IQ over USB in the first place -- decode
runs entirely off FPGA register polling. `tui` does the same whenever
*both* `--no-spectrum` and `--no-waterfall` are given. See "USB
streaming" below for the full writeup.

### Multiple boards

If more than one B200mini is plugged in, all three subcommands prompt for
which one to use (a numbered list):

```
2 B200mini devices found:
  [1] serial=3103D0D  product=B200mini
  [2] serial=3103D16  product=B200mini
Select device [1-2]:
```

Pass `--serial <serial>` to skip the prompt (scriptable/non-interactive
use); with only one board connected it's picked automatically, no prompt
either way.

### TUI keys

| Key | Action |
|---|---|
| `t` | Transmit (compose: address or saved nickname, type, message) |
| `r` | Toggle RX on/off |
| `s` | Settings (frequency, TX/RX gain, bitrate, address filter, our address) |
| `c` | Save the last-seen address to the address book |
| `o` | Hide/show messages we sent ourselves, in MESSAGES |
| `h` / `?` | Help |
| `q` | Quit (closes the device cleanly) |

Layout: FREQUENCY/MODE, RADIO SETTINGS, STATUS, SPECTRUM SCOPE,
WATERFALL, MEMORY (address book), MESSAGES, ACTIVITY LOG. See "The TUI
panels, in detail" below for what each one actually shows and why.

---

Everything past this point is implementation detail most people won't
need -- how it's built, what's been verified and how, and the caveats
that came out of actually testing this on real hardware.

## Architecture

**FPGA (real-time PHY, in fabric):**
- `fsk_demod.v` -- delay-and-multiply FSK discriminator (bit slicer).
- `pocsag_channel_filter.v` -- 31-tap FIR lowpass (~20kHz cutoff) ahead of
  the discriminator. Without this, standard POCSAG deviation (+/-4500Hz)
  doesn't decode at all on real RF -- the discriminator was seeing the
  DDC's full output bandwidth as noise, hundreds of kHz wide, instead of
  POCSAG's actual ~15-20kHz channel, which collapses a delay-and-multiply
  discriminator's SNR (a textbook "FM threshold effect": output SNR scales
  with deviation^2 for fixed input noise bandwidth). Confirmed empirically
  with a deviation sweep before this filter existed (0% decode at
  4.5-15kHz, 100% at 25kHz+); fixed by narrowing the noise bandwidth
  instead of inflating the deviation, the same approach real POCSAG
  decoders use (SDRangel's pager demod plugin defaults to a 20kHz RF
  bandwidth filter, independently matching this design).
- `pocsag_bitsync.v` -- recovers per-bit timing from the discriminator's
  oversampled output. Free-running divider with edge-triggered resync
  *only while unlocked* (gated by `pocsag_framer`'s `locked` signal) --
  TX and RX share one on-board clock here, so there's no drift to track
  once locked, and continuously resyncing on every real-RF noise blip
  turned out to actively hurt reliability (this was a real, measured
  regression -- see git history/commit notes on the two-part fix).
- `pocsag_framer.v` -- finds the frame sync word (0x7CD215D8, Hamming-
  distance-tolerant, resolves 2-FSK polarity ambiguity), shifts out raw
  32-bit codewords, re-verifies sync every batch.
- Reachable via `USER_SETTINGS`: `poke32(3*4, {enable,sps})` to configure,
  `peek64(2*8)` to poll for freshly-captured codewords (see
  `modem.py`'s `REG_POCSAG_CTRL`/`RB_POCSAG_STATUS`).
- `gsc_framer.v` -- GSC (Golay Sequential Code)'s equivalent of
  `pocsag_framer.v`, a second bit-timing-recovery (a second
  `pocsag_bitsync.v` instance, reused as-is at GSC's own 600-baud sps) +
  block-sync chain running concurrently in fabric off the same `fsk_demod`
  bit stream. Finds the control-word Word1 pattern (Hamming-tolerant, same
  technique as `pocsag_framer.v`'s sync word), then tracks subsequent
  comma-delimited blocks, re-verifying the comma before each one rather
  than POCSAG's fixed-batch-length resync (see its header for why: this
  project's own GSC TX convention puts a full comma before every block).
  Reachable via `poke32(4*4, {enable,sps})` / `peek64(3*8)` (see
  `modem.py`'s `REG_GSC_CTRL`/`RB_GSC_STATUS`). See "GSC support"
  below for the full caveats on what this protocol implementation is (and
  isn't) validated against.
- All of the above tap `ddc_chain`'s `sample_rx`/`strobe_rx`, gated by a
  dedicated `run_rx_fabric = pocsag_en | gsc_en` (`radio_legacy.v`) --
  deliberately separate from `run_rx` (which `new_rx_control`/
  `new_rx_framer` still use, unchanged, to gate the actual host-facing USB
  packet path). This means the whole custom PHY chain keeps running and
  decoding purely off USER_SETTINGS register enables, independent of
  whether the host ever streams/drains raw IQ over USB at all -- see
  `transceiver.py`'s `PocsagReceiver.start()` for how the host side uses
  this (skips `stream_cmd`/`recv()` entirely unless the spectrum/waterfall
  display actually needs sample content).

**Host (this directory):**
- `pocsag.py` -- protocol library: BCH(31,21) encode/decode with
  single-bit correction, numeric/alphanumeric message packing, batch/
  preamble assembly, `LiveParser` for incremental decode of a live
  codeword stream.
- `golay.py` / `gsc.py` -- GSC's equivalent of `pocsag.py`: textbook
  Golay(23,12,7) encode/decode (`golay.py`, spec-independent, cross-checked
  bit-for-bit against a real reference implementation -- see its
  docstring) and GSC's own framing/message assembly on top of it
  (`gsc.py`, with a `LiveParser` mirroring `pocsag.py`'s). See "GSC
  support" below for what's confirmed-real vs. this project's own
  convention.
- `modem.py` -- shared UHD plumbing (`open_usrp`, `modulate_cpfsk`,
  the USER_SETTINGS register addresses). Read its module docstring before
  touching threading here -- **three threads concurrently doing USB I/O
  against this device deadlocks it** (confirmed with a minimal repro: not
  one of three threads completed even a single call). Two is fine. RX
  draining and register polling must always be the same thread; TX gets
  its own.
- `transceiver.py` -- `PocsagReceiver`, `PocsagTransmitter`,
  `PocsagTransceiver` classes wrapping the above into a reusable API. This
  is what both the TUI and the CLI (`send`/`listen`) are built on -- not a
  separate implementation for each front end. Also where the shared
  operating defaults actually live (`DEFAULT_FREQ`, `DEFAULT_TX_GAIN`/
  `DEFAULT_RX_GAIN` for a same-board link, `TWO_RADIO_TX_GAIN`/
  `TWO_RADIO_RX_GAIN` for a real two-board link) -- other scripts import
  these rather than redeclaring their own copies of the same numbers.
- `tui.py` / `tui.css` -- the interactive TUI (Textual).
- `fskbuddy.py` -- combined entry point: `tui` (default), `send`,
  `listen` subcommands.
- `pocsag_test_loopback.py` / `pocsag_test_ota.py` -- validation scripts
  (internal digital loopback and real same-board over-the-air, respectively).
- `gsc_test_loopback.py` / `gsc_test_ota_2radio.py` -- GSC's equivalents:
  digital loopback, and true two-radio over-the-air (one board TX, a
  genuinely separate one RX -- possible here since two boards are
  available; POCSAG's own OTA test predates having a second board).
- `two_radio_streaming_test.py` -- exercises `PocsagReceiver`/
  `PocsagTransmitter` themselves (not a raw `stream_cmd`/`recv()` script
  like the others above) over real two-radio RF, both protocols, with no
  `on_spectrum` given -- i.e. the register-poll-only USB streaming mode
  described in "USB streaming" below, the thing that actually changes
  when that mode is used.
- `addresses.json` -- capcode -> nickname address book, used by the TUI.
- `station.json` -- this station's own capcode (Settings -> "Our address"),
  purely identifying/informational; see the promiscuous-receive note below.
- `logs/` -- one plain-text file per TUI session (`fskbuddy_YYYYMMDD_HHMMSS.log`,
  created on launch, path also announced in ACTIVITY LOG), mirroring both
  ACTIVITY LOG and MESSAGES -- neither panel is otherwise persisted
  anywhere, so this is what there is to go back and review after a run
  ends. Not rotated/pruned automatically; delete old ones by hand.

## The TUI panels, in detail

MESSAGES is a decluttered, structured RX/TX log -- kept deliberately
separate from ACTIVITY LOG, which still gets its own fuller technical
line for the same event (gain/bitrate changes, batch sync acquired/lost,
codeword-decode failures, etc.) alongside everything else it already
logs. Each entry shows explicit `To:`/`From:` fields (address, plus a
saved nickname or `(you)` if it's our own capcode) followed by the
message text, color-coded by priority (`RX_MSG_COLOR`/`OWN_MSG_COLOR`/
`TO_US_MSG_COLOR` in `tui.py`): gray for anything we transmitted
ourselves (`From: US` -- POCSAG/GSC pages carry no real sender field, so
"from us" is only ever knowable for what this station itself sent; a
received page's actual origin is unknowable from the protocol, shown as
`From: RF`), yellow for a received page addressed to our own capcode
(Settings' "Our address"), blue otherwise. `o` hides/shows our own
messages -- retroactively, not just for new ones, since the whole panel
is rebuilt from `PocsagTUI.messages` (a kept, unfiltered history) rather
than appended to directly; the session log file always gets every
message regardless of this filter, since filtering is a display-only
concern. A codeword BCH can't correct (`pocsag.py`'s `bch_decode` returns
`None`) now surfaces there too as `decode FAILED: codeword 0x... (...)`
instead of being silently dropped -- wired through `LiveParser`'s
`on_error` hook in `transceiver.py`'s `_run()`. (Deliberately not raised
for an address with no message words before the next one -- that's normal
for a spec-compliant tone-only/ring-only page, not a decode failure.)

That same "codeword BCH can't correct" case used to cause a worse, silent
failure than just a missed page: `LiveParser` (and `parse_codewords()`)
dropped the bad codeword but left whatever page was already in progress
pending. If the bad codeword was actually meant to be the *next* page's
address word (indistinguishable from a bad message word once BCH fails --
the flag bit that would tell them apart lives inside the payload that
didn't decode), that next page's real message codewords would silently
get appended onto the *previous*, unrelated page's buffer instead, and the
following flush would emit one page carrying the old address but a
garbled splice of two different pages' text -- reproduced directly
(`'AAAAA'` became `'AAAAA@PPPP\x10'` once a second page's address word was
corrupted). This is the likely cause of MESSAGES occasionally showing a
wrong-looking message. Fixed by discarding whatever's pending on any
decode failure instead of leaving it around to be contaminated -- losing
an in-flight page outright is strictly better than emitting one with
mismatched address/content.

The receiver is promiscuous by default -- neither the FPGA (no
address-match register exists in `pocsag_framer.v`/the `USER_SETTINGS`
block) nor `pocsag.py`'s decode path filter by capcode; every decodable
page on the tuned frequency is received and logged regardless of address.
The "Address filter" field in Settings (blank by default, shown as
`ALL (promiscuous)` in the STATUS panel) narrows the *display* to one
capcode -- it doesn't change what's actually received, and `n_pages`/
`Codewords` in STATUS keep counting everything either way (see
`PocsagReceiver._run()` in `transceiver.py`). Settings' separate "Our
address" field (persisted to `station.json`) is this station's own
capcode for reference -- purely identifying, not a filter; it doesn't
change what's received or displayed.

SPECTRUM SCOPE and
WATERFALL can each be hidden with `--no-spectrum`/`--no-waterfall`; when
both are given the underlying FFT is skipped entirely, not just the
panels hidden -- see `PocsagReceiver.on_spectrum` in `transceiver.py`,
which is left `None` (rather than a no-op callback) in that case so
`_run()`'s `if ... is not None` check bypasses the FFT call altogether.
Going further than the FFT: with both hidden, `PocsagReceiver` doesn't
stream raw IQ over USB at all in that mode either -- see "USB streaming"
below.

The spectrum scope and waterfall are computed live from real RX samples
(a throttled FFT inside the existing RX thread -- no extra device I/O, no
new thread) -- not simulated. The FFT is cropped to the center
`SPECTRUM_DISPLAY_SPAN_HZ` (`transceiver.py`, default 150kHz -- POCSAG's
own channel is only ~15-20kHz wide, no need to show the full Nyquist
span) and binned to `SPECTRUM_NBINS` columns (150, i.e. 1kHz/bin by
default). A frequency-axis line (left/center/right, in MHz) runs under
the bars -- note this means the panel needs a terminal at least
`SPECTRUM_NBINS` columns wide (150) to show the whole thing un-clipped.

The spectrum scope (green, a live multi-row bar chart using eighth-block
sub-character resolution) sits directly above the waterfall, both using
the exact same one-character-per-bin grid so a signal in one lines up in
the same column in the other. The waterfall itself is colored by an RGB
thermal heatmap (blue -> cyan -> green -> yellow -> red -- deliberately
not the rest of the UI's green theme), scaled against a slowly-adapting
floor/ceiling so a real signal shows as a bright band against a stable
background instead of every row always spanning the full color range. It
updates on its own, much slower cadence than the live bars
(`WATERFALL_INTERVAL_S` in `tui.py`, derived from
`WATERFALL_MAX_LINES=200` for a 10-minute horizon -- ~3s between rows) so
that horizon doesn't scroll out of view in under a minute.

It's a *falling* waterfall -- each new row enters at the top and existing
rows fall toward the bottom, oldest falling off the bottom once
`WATERFALL_MAX_LINES` is full, the direction real spectrogram displays
use (not the "rises from the bottom" direction a naive scrolling log
would give you for free -- Textual's `RichLog` only supports appending at
the bottom, so this is done by rebuilding the visible buffer from a
newest-first `deque` on every waterfall tick; see
`PocsagTUI._handle_spectrum`/`render_waterfall_rows`). A time-scale label
(`-0:00`, `-1:00`, ... how long ago that row was captured) is appended
after every 20th row (~once a minute) as a trailing suffix, not a prefix,
so it doesn't disturb the left-edge column alignment with the spectrum
bars above.

**CLIPPING** in the Status panel flags RX front-end saturation -- dial RX
gain down in Settings (`s`) if you see it. Detected in FPGA fabric
(`clip_detect.v`, tapped pre-channel-filter off the same `sample_rx`/
`strobe_rx` the channel filter itself uses), not by scanning every sample
in Python: a free-running `clip_count` (same diff-against-last-seen-value
idiom `pocsag_framer.v` already uses for codeword count) exposed via
`RB_PHY_STATUS` (`peek64(4*8)`), replacing an O(n) `np.max(np.abs(buf))`
scan every RX loop iteration with one cheap register read. The magnitude
threshold was empirically calibrated against real hardware (a temporary
peak-hold diagnostic register, since removed, compared fabric's raw
I^2+Q^2 against the host's own peak during deliberate extreme overdrive) --
confirmed to within 1 LSB of the intended 95%-of-full-scale trip point, not
just assumed correct from datasheet math. One caveat found while building
this: at *extreme* overdrive (e.g. both TX and RX pinned near max on a
short/strong link) the receiver can stop returning usable samples almost
entirely, and in that specific regime the CLIPPING flag may not light up
either -- but `LOCK: no` / `Codewords: 0` persisting indefinitely is
already an unambiguous sign something's wrong even then. This is exactly
what an empirical two-board gain sweep found on real hardware: the TUI's
gain defaults (`TWO_RADIO_TX_GAIN`/`TWO_RADIO_RX_GAIN` in
`transceiver.py`) used to be pinned to the B200mini's hardware ceilings
(TX 89.75dB, RX 76dB) on the theory that there's no single sane default
across setups -- but at typical close range between two separate radios,
that combo reliably saturated the receiver (CLIPPING, no LOCK, nothing
decodes). The defaults are now 50dB TX/65dB RX, the combo that sweep
found actually locks and decodes cleanly; still just a starting point to
dial in for your own antenna distance/link budget, not a guarantee for
every setup.

**Channel** (12.5kHz "narrow" vs 25kHz "wide") in the FREQUENCY/MODE panel
auto-detects the RX channel width in FPGA fabric (`channel_width_detect.v`),
shown as "detecting..." until it settles. Rather than running a second
bandpass filter pair to measure occupied bandwidth directly (the literal
approach, which would need ~32 more DSP48A1 multiplies -- this design
didn't have them spare, 112 of 132 already used), it estimates FSK
deviation from the statistical swing of `fsk_demod.v`'s own discriminator
output (exposed via new `disc_out`/`disc_valid` ports) relative to the
signal's own power -- narrower-channel conventions use smaller deviation,
wider use larger, and normalizing by power keeps the classification
gain-independent (confirmed: re-measured at two very different gain
settings and got the same ratio). Empirically calibrated against real
hardware transmitting known 2000Hz and 4500Hz deviations through this
project's own TX chain at a genuinely locked, decoding link -- measured
ratios were within 0.4% of the small-angle theoretical prediction
(2*pi*f_dev/sample_rate), and the ratio between them (~2.26x) matched the
deviation ratio (4500/2000 = 2.25x) almost exactly. Debounced over 8
consecutive ~4ms windows (~33ms) before latching a classification change,
same "commit only after sustained agreement" pattern `pocsag_bitsync.v`
already established. Purely informational for now -- it doesn't yet
change filter/deviation handling (the channel filter is still one fixed
~20kHz-cutoff design).

## GSC support

GSC (Golay Sequential Code) is a second paging protocol, decoded/encoded
concurrently with POCSAG by its own fabric chain (`gsc_bitsync`/
`gsc_framer.v`) rather than a separate build -- select which one
`PocsagReceiver`/`PocsagTransceiver` actually listens to and decodes via
`protocol="pocsag"|"gsc"` (the other chain's register just gets disabled,
see `transceiver.py`); TX protocol defaults to match but can be overridden
per-`send()` call.

Framing (comma/gap structure, LSB-first doubled-bit transmission, Golay
FEC) and several real constants (control/activation codewords, preamble
values, address-word table, alpha/numeric character tables) are adopted
directly from **multimon-ng**'s real, independent, field-used GSC decoder
(`demod_gsc.c`/`bch.c`, public domain, github.com/EliasOenal/multimon-ng)
and cross-checked against a primary source, US Patent 4,427,980 (which
describes GSC as background art, not its own invention) -- see `gsc.py`'s
module docstring for the full, source-by-source breakdown of what's
confirmed-real versus this project's own convention.

**What's still this project's own convention, not real GSC**, and why:
real GSC's Word2 -> address-digit arithmetic is a genuine, citable
mixed-radix algorithm (multimon-ng's `reverse_word2()`) intricate enough
that fully inverting it into an encoder wasn't done here -- `gsc.py` uses a
direct bit-packed address mapping instead. Real GSC also uses a separate,
smaller BCH(15,7) code for data blocks (distinct from address/control's
Golay(23,12)) -- not adopted; this implementation uses Golay(23,12)
uniformly rather than add a second FEC implementation for a
self-consistent system. Neither omission affects whether this project's
own TX and RX talk to each other correctly, only interop with a real GSC
network -- which is moot regardless: GSC infrastructure is defunct, and
unlike POCSAG (validated against SDRangel/multimon-ng's own POCSAG
support), there's no independent GSC implementation to interop-test
against, so validation here is limited to this project's own
self-consistency (build a bitstream, feed it through the FPGA framer or a
software-simulated receiver, confirm it comes back out correctly), the
same real limitation the POCSAG section above documents plainly rather
than overclaiming.

`gsc_framer.v` mirrors `pocsag_framer.v`'s division of labor exactly
(framing/PHY in fabric, Golay decode and message assembly in host
software via `golay.py`/`gsc.py`'s `LiveParser`) but differs in lock
strategy: POCSAG re-verifies its fixed 32-bit sync word once per 16-word
batch, while GSC has no such fixed batch length to fall back on, so
`gsc_framer.v` instead re-verifies a 28-symbol alternating comma before
*every* block (matching this project's own TX convention of prefixing
every block with one, not just the first) -- a bit slip self-heals at the
next block instead of the next batch.

Verified on real hardware: digital loopback (`gsc_test_loopback.py` /
`PocsagTransceiver(protocol="gsc")`) and true two-radio over-the-air
(`gsc_test_ota_2radio.py` -- one B200mini transmitting, a genuinely
separate one receiving over real antennas, not the same-board TRX->RX2
trick `pocsag_test_ota.py` uses). Both: a single page transmits and decodes
with the exact expected address and message. The OTA test needed the same
real-two-board gain defaults already established for POCSAG (50dB TX/65dB
RX, not the same-board test's much lower 15dB/35dB -- a genuine
antenna-to-antenna link budget, not near-zero same-board leakage) and the
same larger-than-spec deviation trick (25kHz, swamps this board's
~3.4kHz DC-offset artifact) `pocsag_test_ota.py` already validated on the
identical shared PHY (channel filter, discriminator) GSC's own framer sits
downstream of. One real, characterized edge case turned up along the way
and is worth knowing about: if a transmission's trailing control word is
immediately followed by another transmission's own fresh preamble with
*zero* gap (concatenating repeat bursts bit-for-bit, the way POCSAG's
`repeat=` does safely), the comma-based resync can transiently mistake
preamble content for a comma+word pair -- both are alternating patterns.
This is always safe (Golay's error threshold on the host side rejects the
resulting garbage, never producing a corrupted page) and self-healing (a
fresh correlation search re-finds the next real control word within tens
of symbols), but can briefly cost throughput right at that boundary --
confirmed empirically: zero-gap back-to-back repeats occasionally dropped
a repeat's page outright (safely, not corrupted -- just missing), a real
gap between them didn't. Fixed on the TX side rather than by making the
framer's resync more elaborate: `PocsagTransmitter.send()` inserts a real
~100ms RF-silence gap between GSC repeats (`GSC_INTER_REPEAT_GAP_S`,
`transceiver.py`) instead of concatenating them; confirmed clean (2/2
repeats decoded, zero spurious errors) with the default `repeat=2`
afterward. POCSAG doesn't need this -- its framer resyncs via a direct
32-bit sync-word correlation, immune to "this looks alternating"
confusion, so its own repeats stay concatenated with no gap.

## USB streaming

`PocsagReceiver` skips streaming raw IQ over USB entirely when nothing
needs the sample content -- i.e. whenever `on_spectrum` isn't given
(`fskbuddy.py listen`/`send`, or the TUI with both the spectrum and
waterfall panels hidden, since `tui.py` already funnels both into
one `on_spectrum` callback: `want_spectrum = self.show_spectrum or
self.show_waterfall`). Before this, `start()`/`_run()` issued
`stream_cmd(start_cont)` and called `rx_streamer.recv()` unconditionally,
every loop iteration, regardless of whether anything used the result --
at 1MSPS with the `sc16` wire format (4 bytes/sample), that's a continuous
~32Mbps of USB traffic just to keep decode working, even for a purely
headless listener that only ever reads `peek64` registers.

Root cause (confirmed by reading the RTL, not assumed): the whole custom
PHY chain (`pocsag_channel_filter`, `fsk_demod`, both bitsync/framer
pairs, `clip_detect`, `channel_width_detect`) taps `ddc_chain`'s own
`sample_rx`/`strobe_rx`, and `ddc_chain`'s `run` input used to be the same
`run_rx` signal `new_rx_control`/`new_rx_framer` use to gate the
host-facing USB packet path (`uhd/fpga/usrp3/lib/vita_200/new_rx_control.v`:
`assign run = (ibs_state == IBS_RUNNING)`, deasserted the instant the
downstream framing FIFO reports full). So the decimator -- and everything
downstream of it in fabric -- silently stalled whenever the host stopped
draining, confirmed empirically before the fix: `stream_cmd(start_cont)`
issued once with zero subsequent `recv()` calls showed zero register
progress for a full 6 seconds. Fixed with a dedicated
`run_rx_fabric = pocsag_en | gsc_en` (`radio_legacy.v`) driving only
`ddc_chain`'s `run` input -- `new_rx_control`/`new_rx_framer`/both
`gpio_atr` ATR instances keep using the original `run_rx`, completely
unchanged, so the actual USB packet path still gates exactly as before.
`ddc_chain`'s `run` port was already a plain level-sensitive enable
throughout (CORDIC, CIC strober, both halfband decimators -- see
`uhd/fpga/usrp3/lib/dsp/ddc_chain.v`), so decoupling it was safe: the only
other effect of holding it low was resetting the NCO phase accumulator,
harmless to avoid by holding it at 1 whenever either protocol's decode is
enabled.

Verified on real hardware, both before and after: with the fix, the same
zero-`recv()`-calls test that previously stalled for the full 6-second
poll window now shows register-poll-only progress within the first
100ms. End-to-end confirmed over true two-radio RF too
(`two_radio_streaming_test.py` -- one board TX, a genuinely separate one
RX via `PocsagReceiver` with no `on_spectrum`, `receiver._streaming ==
False` the whole time): both POCSAG and GSC pages decode correctly with
zero `rx_streamer.recv()` calls on the RX board for the entire session.
Streaming mode (spectrum/waterfall on) was re-verified unchanged
alongside it. One real bug found and fixed along the way: the initial
register-poll-only loop paced itself with the same 0.3s sleep `recv()`
used to block for, which is far slower than data actually arrives (a
32-bit POCSAG codeword every ~13.3ms at 2400bps) -- since the status
registers hold only the latest codeword/word pair (no queue), a slow poll
doesn't just add latency, it silently loses codewords/blocks. Fixed by
polling every 2ms instead (cheap -- `peek64` is a register read, not a USB
bulk transfer).

**On the actual USB traffic number**: "zero streaming" is verified directly
(`rx_streamer.recv()` is provably never called in this mode -- see above),
but "how many bytes/sec that leaves" is an estimate, not a bus-level
measurement -- this environment doesn't have `usbmon` access (needs `sudo`,
unavailable non-interactively here) to capture and confirm one. At up to
~500-1000 tiny `peek64` register-read transactions/sec (2ms poll x 2 reads/
iteration) over UHD's small control-packet channel -- not the bulk
sample-streaming endpoint an RX packet uses -- even a generous per-
transaction estimate bounds this well under 2Mbps, versus the ~32Mbps
continuous stream this mode replaces. That's a >95% cut, confidently, but
don't take "under 2Mbps" as a precise measured figure -- it's a bound, not
a capture.

## POCSAG spec compliance

Core framing matches spec and is validated end-to-end on real hardware:
preamble, 0x7CD215D8 sync word, 8-frame/16-codeword batches, BCH(31,21)
with single-bit correction (double-bit errors are detected, not corrected
-- standard practice, matches most real-world decoders), 3-bit
frame-number address encoding, both numeric and alphanumeric message
types, standard bit rates (512/1200/2400), and -- after the channel filter
fix -- standard +/-4500Hz deviation, with 100% exact-match decode measured
across a real TX(TRX)->air->RX(RX2) link at every deviation from 4.5kHz to
25kHz.

Two things worth knowing:
- **Numeric character table**: verified against two independent
  transcriptions of the spec's Table 1 (0x0-0x9 digits, 0xA spare/
  reserved, 0xB 'U', 0xC space, 0xD hyphen, 0xE ']', 0xF '['). An earlier
  version of this table was wrong (guessed from memory, not checked) --
  digits 0-9 were always right, but 4 of the 6 special-character
  positions weren't. Fixed and cross-checked against the spec's own
  stated padding value (space = code 1100 = 0xC), which only lines up
  with the corrected table.
- **Function bits**: the spec leaves their meaning carrier-defined. This
  implementation uses the common convention (function 3 = alphanumeric,
  else numeric), which is reasonable but not *the* standard -- there
  isn't one.

Not implemented (not required for spec-compliant framing, real pagers use
these for other reasons): address-based frame-skip for battery saving
(a receiver optimization, not a framing requirement), and POCSAG doesn't
have a 4-level FSK mode at all (that's FLEX) so there's nothing missing
there. `PocsagTransmitter.send()` does repeat each page's full
preamble+batch(es) twice in one burst by default (`repeat=` parameter) --
real systems commonly repeat pages for reliability; a single one-shot
burst measurably has less decode margin than a repeated one.

## Known hardware quirk

Every script here reliably segfaults (or, occasionally, aborts with a
glibc "double free" message) a moment after finishing -- always *after*
all real work is done and logged, confirmed repeatedly with unbuffered
output and headless test harnesses that assert success before the crash
happens. This is a host-side UHD/pybind teardown-ordering issue in this
environment (likely toolchain-version related -- this UHD build runs
against a newer GCC/Boost/CPython than it's typically tested with), not a
device or logic problem: `uhd_find_devices` confirms the board is healthy
immediately after every occurrence. Treat a nonzero exit code alongside
expected output/log lines as a pass, not a failure.

## Licensing -- transmit responsibly

**Frequency (`--freq`, or the Settings screen's "Frequency" field, `s`) is
fully configurable, and the hardware will transmit on whatever you set it
to.** This software has no way to know what you're actually authorized to
transmit on -- it doesn't check band plans, doesn't check your license
class, doesn't stop you from keying up somewhere you shouldn't. That's on
you, the operator, every time. Receiving is generally unrestricted;
*transmitting* is what requires real authorization (an amateur radio
license for ham bands, a commercial/private-land-mobile license
elsewhere, etc.) -- know what band you're tuned to and what it actually
authorizes before you hit TX. `DEFAULT_FREQ` (929.6625MHz, in
`transceiver.py`) was this project's original private-paging-band test
frequency; it is not a recommendation for your own use.
