#!/usr/bin/env python3
"""Reusable POCSAG/GSC transmitter/receiver classes, sitting on top of the
FPGA PHY (pocsag_bitsync.v/gsc_framer.v + the existing fsk_demod/duc/ddc
chains) the same way pocsag_test_ota.py does.

One physical device, one MultiUSRP session (PocsagTransceiver owns it) --
these classes are meant to be driven together by one controller (see
fskbuddy.py).

Threading contract (load-bearing, see modem.py's docstring for the
underlying discovery): this device deadlocks with three threads
concurrently doing USB I/O against it. So:
  - PocsagReceiver owns exactly one thread, which does recv() AND
    peek64()/poke32() together, in that one thread, forever.
  - PocsagTransmitter owns exactly one thread, spun up per send() and
    joined before returning (or reused across calls), doing send() and its
    own poke32()/set_tx_gain() calls.
  - That's the two-thread ceiling. Nothing else (the TUI, the CLI) is
    allowed to touch usrp/regs/streamers directly while either is running
    -- gain/bitrate changes go through the request_* methods below, which
    hand the change to the owning thread instead of applying it inline.
"""
import threading
import time
import queue
import numpy as np
import uhd

import pocsag as p
import gsc
from modem import (open_usrp, modulate_cpfsk, REG_POCSAG_CTRL, RB_POCSAG_STATUS,
                   REG_GSC_CTRL, RB_GSC_STATUS, RB_PHY_STATUS)

DEFAULT_FREQ = 929.6625e6
DEFAULT_RATE = 1e6
DEFAULT_TX_GAIN = 15.0  # tuned for a same-board loopback link (a much shorter/stronger
DEFAULT_RX_GAIN = 35.0  # path than two separate radios over the air) -- see TWO_RADIO_*
                         # below for the real two-board defaults; the CLI's send/listen
                         # subcommands use these since fskbuddy.py's send/listen are
                         # commonly run against a single board's own loopback/OTA path.
DEFAULT_BITRATE = 1200

# Real two-board defaults -- used to be pinned to the B200mini's hardware
# ceilings (TX 89.75dB, RX 76dB) on the theory that there's no single sane
# default across setups so it should start hot and let the user dial down.
# That theory didn't survive contact with a real two-radio link: an
# empirical gain sweep between two boards on a bench found max gain badly
# overdrives the RX front end at typical close range -- CLIPPING, no LOCK,
# nothing decodes -- while 50/65 TX/RX locked and decoded cleanly with no
# clipping. Still just a starting point (dial in via Settings, `s`, in the
# TUI, for your actual antenna distance/link budget), but one that's been
# shown to actually work on real hardware rather than one guaranteed to
# saturate the receiver. Deliberately NOT the same as DEFAULT_TX_GAIN/
# DEFAULT_RX_GAIN above -- see there.
TWO_RADIO_TX_GAIN = 50.0
TWO_RADIO_RX_GAIN = 65.0
GSC_BITRATE = 600  # GSC's fixed baud rate (a real spec value, see gsc.py's module
                    # docstring) -- unlike POCSAG's bitrate, this isn't user-configurable,
                    # so it's a plain module constant rather than a request_bitrate() knob.
GSC_INTER_REPEAT_GAP_S = 0.1  # real RF silence inserted between repeated GSC bursts --
                               # see PocsagTransmitter.send()'s comment for why (a real,
                               # characterized gsc_framer.v edge case at zero-gap
                               # transmission boundaries). The true minimum needed
                               # wasn't pinned down precisely -- empirically confirmed
                               # clean with 500ms in ad hoc testing; 100ms here is a
                               # deliberately generous margin above the shift register's
                               # own 46-symbol/~77ms width (at 600 baud), not a value
                               # trimmed down to the actual minimum.
DEFAULT_DEVIATION_HZ = 4500.0  # standard POCSAG deviation. This used to need to be much
                                # larger (25kHz) to work around an FM-discriminator
                                # threshold effect on real RF -- fixed properly by adding
                                # pocsag_channel_filter.v (a ~20kHz FPGA-side lowpass ahead
                                # of the discriminator) instead of fudging the deviation.
                                # Verified 100% exact-match decode at 4500Hz across a real
                                # TX(TRX)->air->RX(RX2) link after that fix.


class PocsagReceiver:
    """Owns the RX-drain + register-poll thread. Call start()/stop(). Feed
    it callbacks for pages, raw status, and log lines; it never touches
    curses/print directly so it's equally usable headless."""

    SPECTRUM_DISPLAY_SPAN_HZ = 150_000  # total displayed bandwidth -- POCSAG's channel is
                                         # only ~15-20kHz wide, no need to show the full
                                         # +/-500kHz Nyquist span at a 1MHz rate. Shrunk
                                         # from 500kHz alongside the finer 1kHz/bin request
                                         # below -- at the old 500kHz span, 1kHz/bin would
                                         # need 500 character-columns, wider than any
                                         # reasonable terminal; narrowing the span keeps it
                                         # on-screen instead of mostly scrolled off.
    SPECTRUM_NBINS = 150    # -> 1kHz/bin at the 150kHz span above
    SPECTRUM_INTERVAL_S = 0.15  # throttles UI updates, not the recv() loop itself
    # RX front-end saturation (gain set too hot for the actual link) is
    # detected in fabric now (clip_detect.v), not by scanning every sample
    # in Python -- see _run()'s RB_PHY_STATUS read. Empirically calibrated
    # against real hardware (extreme-overdrive test, both TX/RX gain
    # maxed): fabric's threshold and the host's old np.abs()-based one
    # agreed to within 1 LSB, and both independently caught the same
    # front-end saturation event (I/Q pegged at the ADC/DDC chain's own
    # clip point). See clip_detect.v's header for the full calibration
    # writeup.

    def __init__(self, usrp, regs, rx_streamer, bitrate=DEFAULT_BITRATE,
                 protocol="pocsag", alpha_function=3, address_filter=None,
                 on_page=None, on_status=None, on_log=None, on_spectrum=None):
        assert protocol in ("pocsag", "gsc"), protocol
        self.usrp = usrp
        self.regs = regs
        self.rx_streamer = rx_streamer
        self.bitrate = bitrate
        self.protocol = protocol  # selects which of the two concurrently-running fabric
                                   # bitsync/framer chains (see radio_legacy.v) this receiver
                                   # polls and decodes; the other chain is left disabled (see
                                   # start()) rather than run for nothing.
        self.alpha_function = alpha_function
        self.address_filter = address_filter
        self.on_page = on_page or (lambda page: None)
        self.on_status = on_status or (lambda **kw: None)
        self.on_log = on_log or (lambda msg: None)
        self.on_spectrum = on_spectrum  # may be left None -- the FFT in _run() is skipped
                                         # entirely when so (see there), not just called with
                                         # a no-op callback, so a caller that wants the
                                         # spectrum/waterfall UI disabled also gets the CPU
                                         # savings, not just a hidden panel.

        self._thread = None
        self._stop_event = threading.Event()
        self._pending_rx_gain = None
        self._pending_bitrate = None
        self._pending_freq = None
        self._lock = threading.Lock()
        self._streaming = False  # set in start(), read in stop() -- whether this session
                                  # actually issued stream_cmd(start_cont) (only when
                                  # on_spectrum was given at start() time) so stop() knows
                                  # whether stop_cont is meaningful to issue. See start()'s
                                  # comment on why "no spectrum/waterfall" skips USB
                                  # streaming entirely rather than draining it for nothing.

        self.n_codewords = 0
        self.n_pages = 0
        self.locked = False
        self.clipping = False
        self.channel_width_narrow = False  # default wide -- matches channel_width_detect.v's reset state
        self.channel_width_locked = False

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def request_gain(self, gain):
        with self._lock:
            self._pending_rx_gain = gain

    def request_bitrate(self, bitrate):
        with self._lock:
            self._pending_bitrate = bitrate

    def request_freq(self, freq):
        # Same pattern as request_gain/request_bitrate -- applied from
        # this receiver's own thread in _run() below, not here directly,
        # per the module docstring's threading contract (retuning is USB
        # I/O against the shared usrp, same as set_rx_gain already is).
        with self._lock:
            self._pending_freq = freq

    def set_address_filter(self, address):
        self.address_filter = address

    def start(self):
        if self.running:
            return
        self._stop_event.clear()
        # Enable only the selected protocol's chain -- both can run
        # concurrently in fabric for free (see radio_legacy.v), but there's
        # no reason to decode Golay/BCH for the one nothing's listening to,
        # so leave the other disabled.
        if self.protocol == "pocsag":
            sps = self._sps_for(self.bitrate)
            self.regs.poke32(REG_POCSAG_CTRL * 4, (1 << 16) | sps)
            self.regs.poke32(REG_GSC_CTRL * 4, 0)
        else:
            sps = self._sps_for(GSC_BITRATE)
            self.regs.poke32(REG_GSC_CTRL * 4, (1 << 16) | sps)
            self.regs.poke32(REG_POCSAG_CTRL * 4, 0)
        # Only actually stream raw IQ over USB if something needs the sample
        # content -- the spectrum/waterfall FFT. Decode itself (POCSAG or
        # GSC) never touches rx_streamer at all; it's entirely fabric PHY +
        # USER_SETTINGS register polling (see radio_legacy.v's
        # run_rx_fabric, split from the host-USB-streaming run_rx
        # specifically so this works: the custom PHY chain keeps running in
        # fabric regardless of whether the host asks for/drains a
        # packetized RX stream). Confirmed empirically that skipping this
        # cuts RX USB traffic from a continuous ~32Mbps (1MSPS, sc16 OTW) to
        # near-zero (occasional peek64/poke32 only) -- see the plan notes
        # for the full writeup. `self.on_spectrum is not None` is already
        # the exact "do I need raw IQ" signal (see __init__ and
        # tui.py's own want_spectrum = show_spectrum or
        # show_waterfall, which is what sets it).
        self._streaming = self.on_spectrum is not None
        if self._streaming:
            stream_cmd = uhd.types.StreamCMD(uhd.types.StreamMode.start_cont)
            stream_cmd.stream_now = True
            self.rx_streamer.issue_stream_cmd(stream_cmd)

            # Confirm real samples are actually flowing before returning. Found
            # the hard way (watching front-panel LEDs on a two-radio setup) that
            # a caller starting TX right after start() returns can otherwise
            # race ahead of RX actually being up -- device open/settle latency
            # isn't identical across boards (e.g. USB2 vs USB3), so a fixed
            # sleep elsewhere isn't a reliable substitute for this. Only
            # meaningful when actually streaming -- there's nothing to confirm
            # via recv() in register-poll-only mode.
            buf = np.zeros(self.rx_streamer.get_max_num_samps(), dtype=np.complex64)
            md = uhd.types.RXMetadata()
            confirmed = False
            for _ in range(20):
                n = self.rx_streamer.recv(buf, md, timeout=1.0)
                if n > 0 and md.error_code == uhd.types.RXMetadataErrorCode.none:
                    confirmed = True
                    break
            if not confirmed:
                self.on_log("WARNING: RX did not confirm streaming within 20s")

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self.on_log(f"RX started @ {self.bitrate}bps (sps={sps})")

    def stop(self):
        if not self.running:
            return
        self._stop_event.set()
        self._thread.join(timeout=3.0)
        self._thread = None
        if self._streaming:
            stream_cmd = uhd.types.StreamCMD(uhd.types.StreamMode.stop_cont)
            self.rx_streamer.issue_stream_cmd(stream_cmd)
            self._streaming = False
        self.regs.poke32(REG_POCSAG_CTRL * 4, 0)
        self.regs.poke32(REG_GSC_CTRL * 4, 0)
        self.on_log("RX stopped")

    def _sps_for(self, bitrate):
        return round(self.usrp.get_rx_rate() / bitrate)

    def _run(self):
        def on_decode_error(raw, errs):
            kind = "uncorrectable, 2+ bit errors" if errs == 2 else "unrecognized error pattern"
            self._safe_callback(self.on_log, f"decode FAILED: codeword 0x{raw:08x} ({kind})")

        def on_gsc_decode_error(w1, w2, errs):
            self._safe_callback(self.on_log,
                                 f"GSC decode FAILED: word1=0x{w1:06x} word2=0x{w2:06x} (errs={errs})")

        if self.protocol == "pocsag":
            parser = p.LiveParser(alpha_function=self.alpha_function, on_error=on_decode_error)
        else:
            parser = gsc.LiveParser(on_error=on_gsc_decode_error)
        last_count = None
        last_clip_count = None
        was_locked = False
        buf = np.zeros(self.rx_streamer.get_max_num_samps(), dtype=np.complex64)
        md = uhd.types.RXMetadata()
        last_spectrum_t = 0.0

        while not self._stop_event.is_set():
            # Apply any pending live changes -- from this thread only, per
            # the module docstring's threading contract.
            with self._lock:
                pending_gain, self._pending_rx_gain = self._pending_rx_gain, None
                pending_bitrate, self._pending_bitrate = self._pending_bitrate, None
                pending_freq, self._pending_freq = self._pending_freq, None
            if pending_gain is not None:
                self.usrp.set_rx_gain(pending_gain)
                self._safe_callback(self.on_log, f"RX gain -> {pending_gain} dB")
            if pending_freq is not None:
                self.usrp.set_rx_freq(uhd.types.TuneRequest(pending_freq))
                actual = self.usrp.get_rx_freq()
                self._safe_callback(self.on_log,
                                     f"RX freq -> {actual/1e6:.4f} MHz (requested {pending_freq/1e6:.4f})")
            if pending_bitrate is not None:
                if self.protocol == "pocsag":
                    self.bitrate = pending_bitrate
                    sps = self._sps_for(self.bitrate)
                    self.regs.poke32(REG_POCSAG_CTRL * 4, (1 << 16) | sps)
                    self._safe_callback(self.on_log, f"RX bitrate -> {self.bitrate}bps (sps={sps})")
                else:
                    self._safe_callback(self.on_log,
                                         "GSC bitrate is fixed at 600bps, ignoring request")

            if self._streaming:
                n = self.rx_streamer.recv(buf, md, timeout=0.3)
            else:
                # Register-poll-only mode (see start()) -- no USB stream to
                # drain, so recv()'s own blocking behavior isn't available
                # to pace this loop. Deliberately NOT matching recv()'s old
                # 0.3s timeout here -- found the hard way (two-radio testing)
                # that 0.3s is far slower than real data actually arrives:
                # the status registers hold only the latest codeword/word
                # pair (no queue), so any poll slower than the true
                # arrival rate doesn't just add latency, it genuinely loses
                # codewords/blocks. POCSAG's fastest supported bitrate
                # (2400bps) produces a 32-bit codeword every ~13.3ms; this
                # sleep needs to stay comfortably under that. peek64 itself
                # is a fast register read (no USB bulk transfer), so a
                # short sleep is cheap -- this still avoids a true busy-spin
                # while keeping enough margin to not miss data at any
                # supported bitrate.
                n = 0
                time.sleep(0.002)

            # Spectrum snapshot, computed right here (same thread, no new
            # device I/O) and throttled -- an FFT of a few thousand samples
            # is microseconds, but re-rendering the TUI on every recv() call
            # (hundreds/sec) would be wasteful.
            now = time.monotonic()
            if (n > 0 and self.on_spectrum is not None
                    and (now - last_spectrum_t) >= self.SPECTRUM_INTERVAL_S):
                last_spectrum_t = now
                mags = self._compute_spectrum(buf[:n])
                self._safe_callback(self.on_spectrum, mags, self.usrp.get_rx_rate())

            # Status layout differs per protocol -- see RB_POCSAG_STATUS/
            # RB_GSC_STATUS's comments in modem.py -- but both boil
            # down to the same (locked, free-running block count,
            # raw-codeword payload) shape this loop already diffs/decodes
            # generically below.
            if self.protocol == "pocsag":
                status = self.regs.peek64(RB_POCSAG_STATUS * 8)
                locked = bool((status >> 40) & 0x1)
                count = (status >> 32) & 0xFF
                codeword = status & 0xFFFFFFFF
            else:
                status = self.regs.peek64(RB_GSC_STATUS * 8)
                locked = bool((status >> 54) & 0x1)
                count = (status >> 46) & 0xFF
                word1 = (status >> 23) & 0x7FFFFF
                word2 = status & 0x7FFFFF

            if locked != was_locked:
                sync_kind = "batch sync" if self.protocol == "pocsag" else "block sync"
                self._safe_callback(self.on_log, f"{sync_kind} {'acquired' if locked else 'lost'}")
            was_locked = locked
            self.locked = locked

            # RX front-end clipping, computed in fabric (clip_detect.v) --
            # see its header and the class docstring above. clip_count is
            # free-running (wraps), same diff-against-last-seen-value idiom
            # already used for codeword count below.
            phy_status = self.regs.peek64(RB_PHY_STATUS * 8)
            clip_count = (phy_status >> 22) & 0xFF
            if last_clip_count is not None:
                self.clipping = ((clip_count - last_clip_count) & 0xFF) >= 1
            last_clip_count = clip_count

            # Channel-width auto-detection (channel_width_detect.v) --
            # narrow=12.5kHz-style, wide=25kHz-style. `locked` here is that
            # module's own debounce settling, unrelated to POCSAG's
            # batch-sync `locked` above. Log only when it just settled on a
            # (possibly new) classification -- not every register poll.
            new_width_narrow = bool((phy_status >> 31) & 0x1)
            new_width_locked = bool((phy_status >> 30) & 0x1)
            just_settled = new_width_locked and (
                not self.channel_width_locked or new_width_narrow != self.channel_width_narrow)
            if just_settled:
                kind = "narrowband (12.5kHz-style)" if new_width_narrow else "wideband (25kHz-style)"
                self._safe_callback(self.on_log, f"channel width -> {kind}")
            self.channel_width_narrow = new_width_narrow
            self.channel_width_locked = new_width_locked

            if last_count is None:
                last_count = count
            else:
                n_new = (count - last_count) & 0xFF
                if n_new >= 1:
                    if n_new > 1:
                        unit = "codeword(s)" if self.protocol == "pocsag" else "block(s)"
                        self._safe_callback(self.on_log, f"missed {n_new - 1} {unit}")
                    last_count = count
                    self.n_codewords += 1
                    page = parser.feed(codeword) if self.protocol == "pocsag" else parser.feed(word1, word2)
                    if page is not None:
                        self.n_pages += 1
                        if self.address_filter is None or page.address == self.address_filter:
                            self._safe_callback(self.on_page, page)

            self._safe_callback(self.on_status, locked=self.locked, n_codewords=self.n_codewords,
                                 n_pages=self.n_pages, bitrate=self.bitrate, clipping=self.clipping,
                                 channel_width_narrow=self.channel_width_narrow,
                                 channel_width_locked=self.channel_width_locked)

    def _compute_spectrum(self, samples):
        """FFT magnitude (dB), cropped to the center SPECTRUM_DISPLAY_SPAN_HZ
        of the full Nyquist span (POCSAG's own channel is ~15-20kHz wide --
        showing the full +/-500kHz-at-1MHz-rate span is mostly dead air) and
        binned down to SPECTRUM_NBINS columns (~SPECTRUM_DISPLAY_SPAN_HZ /
        SPECTRUM_NBINS resolution each) by averaging in the power domain --
        for a compact terminal display, not spectral analysis precision."""
        n = len(samples)
        win = np.hanning(n)
        spec = np.fft.fftshift(np.fft.fft(samples * win))
        power = np.abs(spec) ** 2 + 1e-20

        rate = self.usrp.get_rx_rate()
        frac = min(1.0, self.SPECTRUM_DISPLAY_SPAN_HZ / rate)
        crop_len = max(self.SPECTRUM_NBINS, int(round(n * frac)))
        crop_len = min(crop_len, n)
        start = (n - crop_len) // 2
        power = power[start:start + crop_len]

        nbins = self.SPECTRUM_NBINS
        ncrop = len(power)
        if ncrop >= nbins:
            trim = (ncrop // nbins) * nbins
            power = power[:trim].reshape(nbins, -1).mean(axis=1)
        else:
            power = np.interp(np.linspace(0, ncrop - 1, nbins), np.arange(ncrop), power)
        return (10 * np.log10(power)).tolist()

    def _safe_callback(self, fn, *args, **kwargs):
        """Consumers (a TUI, a CLI) can go away mid-flight -- e.g. the
        Textual event loop closing while this thread is mid-iteration.
        Don't let a broken callback crash the whole polling thread
        silently; log to stderr as a last resort and stop, rather than
        spin forever re-failing the same callback every iteration."""
        try:
            fn(*args, **kwargs)
        except Exception as e:
            import sys
            print(f"PocsagReceiver: callback {fn!r} failed ({e!r}), stopping.",
                  file=sys.stderr)
            self._stop_event.set()


class PocsagTransmitter:
    """Owns TX send() calls. send() blocks until the burst is fully sent
    (a few hundred ms to a couple seconds depending on message length and
    repeat count); call it from a worker thread (see send_async) if the
    caller (e.g. a TUI) can't block."""

    DEFAULT_REPEAT = 2  # a single one-shot burst has no retry margin -- real POCSAG
                         # systems commonly repeat a page for exactly this reliability
                         # reason; repeating the whole preamble+batch(es) N times in one
                         # burst (rather than relying on the caller to resend) is cheap
                         # and meaningfully more reliable in practice.

    def __init__(self, usrp, tx_streamer, tx_gain=DEFAULT_TX_GAIN, freq=DEFAULT_FREQ,
                 deviation_hz=DEFAULT_DEVIATION_HZ, on_log=None):
        self.usrp = usrp
        self.tx_streamer = tx_streamer
        self.tx_gain = tx_gain
        self.freq = freq  # re-tuned fresh on every send() below, same pattern as tx_gain --
                           # a caller (PocsagTransceiver.request_freq(), the TUI's Settings)
                           # just sets this attribute directly; there's no dedicated TX
                           # thread to hand it to ahead of time the way PocsagReceiver's
                           # request_gain/request_freq need one (send() already runs in its
                           # own thread per call, so retuning there is already the right
                           # thread per the module docstring's contract).
        self.deviation_hz = deviation_hz
        self.on_log = on_log or (lambda msg: None)
        self._send_lock = threading.Lock()
        self._async_thread = None

    CHUNK = 4000

    def send(self, address, function, message, bitrate=DEFAULT_BITRATE, gain=None,
              repeat=DEFAULT_REPEAT, protocol="pocsag"):
        """Blocking. Builds a full preamble+batch(es)/block(s) for one page
        in the given protocol and transmits it `repeat` times back-to-back
        in one burst."""
        assert protocol in ("pocsag", "gsc"), protocol
        with self._send_lock:
            g = gain if gain is not None else self.tx_gain
            self.usrp.set_tx_gain(g)
            self.usrp.set_tx_freq(uhd.types.TuneRequest(self.freq))

            if protocol == "pocsag":
                msg_cws = p.encode_alpha(message) if function == 3 else p.encode_numeric(message)
                bits = p.build_bitstream(address, function, msg_cws) * max(1, repeat)
                use_bitrate = bitrate
                sps = round(self.usrp.get_tx_rate() / use_bitrate)
                iq = modulate_cpfsk(bits, sps, self.usrp.get_tx_rate(), deviation_hz=self.deviation_hz)
                n_bits = len(bits)
            else:
                # gsc.build_bitstream takes the raw message directly (unlike
                # POCSAG's separate encode-then-build split above) and
                # assembles the whole preamble+control+address+data+control
                # sequence itself -- see gsc.py.
                use_bitrate = GSC_BITRATE  # fixed baud rate, see module docstring
                sps = round(self.usrp.get_tx_rate() / use_bitrate)
                bits = gsc.build_bitstream(address, function, message)
                burst_iq = modulate_cpfsk(bits, sps, self.usrp.get_tx_rate(),
                                           deviation_hz=self.deviation_hz)
                # Real RF silence between repeats, not just concatenated bits
                # like POCSAG does above. gsc_framer.v's comma-based
                # per-block resync (see there) can transiently -- and
                # safely, Golay's error threshold rejects it, never
                # corrupting data -- confuse a trailing control word
                # immediately followed by another transmission's own fresh
                # preamble, since both are alternating patterns; a real gap
                # sidesteps the ambiguity entirely rather than needing a
                # cleverer resync. POCSAG doesn't need this -- its framer
                # resyncs via a direct 32-bit sync-word correlation, immune
                # to "this looks alternating" confusion. Confirmed
                # empirically on real hardware (digital loopback):
                # concatenating repeats with zero gap silently dropped a
                # repeat's page (safely, not corrupting one -- just missing
                # it), a real gap between them didn't.
                gap_iq = np.zeros(int(GSC_INTER_REPEAT_GAP_S * self.usrp.get_tx_rate()),
                                   dtype=np.complex64)
                iq = burst_iq
                for _ in range(max(1, repeat) - 1):
                    iq = np.concatenate([iq, gap_iq, burst_iq])
                n_bits = len(bits) * max(1, repeat)

            actual_freq = self.usrp.get_tx_freq()
            self.on_log(f"TX[{protocol}] {actual_freq/1e6:.4f}MHz addr={address} "
                        f"func={function} {n_bits} bits ({repeat}x) @ {use_bitrate}bps, "
                        f"{g} dB: {message!r}")

            md = uhd.types.TXMetadata()
            md.start_of_burst = True
            md.has_time_spec = False
            for start in range(0, len(iq), self.CHUNK):
                self.tx_streamer.send(iq[start:start + self.CHUNK], md)
                md.start_of_burst = False
                # Explicit yield: found the hard way (a clipping indicator that
                # silently never fired) that this tight loop, run as a thread
                # sharing the GIL with PocsagReceiver's own thread, can starve
                # RX of scheduling time badly enough to stall its recv() calls
                # -- UHD's Python bindings don't reliably release the GIL
                # during blocking I/O (the same underlying issue behind the
                # module docstring's 3-thread deadlock finding). A bare yield
                # costs nothing and doesn't affect TX timing (UHD's own
                # buffering already tolerates scheduling jitter for continuous
                # streaming) but gives the scheduler a real chance to switch.
                time.sleep(0)
            md.end_of_burst = True
            self.tx_streamer.send(np.zeros(1, dtype=np.complex64), md)
            self.on_log("TX done")

    def send_async(self, address, function, message, bitrate=DEFAULT_BITRATE,
                    gain=None, repeat=DEFAULT_REPEAT, protocol="pocsag", on_done=None):
        """Non-blocking: runs send() in its own thread. Only one async
        send at a time -- if one's in flight, this joins it first."""
        if self._async_thread is not None and self._async_thread.is_alive():
            self._async_thread.join()

        def run():
            try:
                self.send(address, function, message, bitrate, gain, repeat, protocol)
            finally:
                if on_done:
                    on_done()

        self._async_thread = threading.Thread(target=run, daemon=True)
        self._async_thread.start()


class PocsagTransceiver:
    """Owns the MultiUSRP session and both PocsagReceiver/PocsagTransmitter.
    This is the thing a controller (TUI or CLI) actually talks to."""

    def __init__(self, freq=DEFAULT_FREQ, rate=DEFAULT_RATE,
                 tx_gain=DEFAULT_TX_GAIN, rx_gain=DEFAULT_RX_GAIN,
                 bitrate=DEFAULT_BITRATE, deviation_hz=DEFAULT_DEVIATION_HZ,
                 protocol="pocsag", on_log=None, serial=None):
        self.on_log = on_log or (lambda msg: None)
        self.freq = freq
        self.rate = rate

        self.usrp = open_usrp(freq, rate, gain=rx_gain, antenna="RX2", tx=True, serial=serial)
        self.usrp.set_tx_gain(tx_gain)
        self.regs = self.usrp.get_user_settings_iface(0)

        self.tx_streamer = self.usrp.get_tx_stream(uhd.usrp.StreamArgs("fc32", "sc16"))
        self.rx_streamer = self.usrp.get_rx_stream(uhd.usrp.StreamArgs("fc32", "sc16"))

        self.receiver = PocsagReceiver(self.usrp, self.regs, self.rx_streamer,
                                        bitrate=bitrate, protocol=protocol, on_log=self.on_log)
        self.transmitter = PocsagTransmitter(self.usrp, self.tx_streamer,
                                              tx_gain=tx_gain, freq=freq,
                                              deviation_hz=deviation_hz,
                                              on_log=self.on_log)

    def request_freq(self, freq):
        """Re-tune both RX and TX to a new shared frequency -- see
        transceiver.py's design note: this project deliberately keeps one
        shared frequency (not independent RX/TX) rather than a
        repeater-style split, matching how every part of this project
        already operates. TX picks the new value up fresh on its next
        send() (see PocsagTransmitter.send()); RX is applied from its own
        thread if currently running (see PocsagReceiver.request_freq()),
        or directly here if not (no RX thread active to hand it to, and
        no thread-safety concern retuning directly in that case).

        Operating outside a frequency band you're licensed for is the
        caller's responsibility -- this library has no way to know what
        you're actually authorized to transmit on. See the README's
        licensing note."""
        self.freq = freq
        self.transmitter.freq = freq
        if self.receiver.running:
            self.receiver.request_freq(freq)
        else:
            self.usrp.set_rx_freq(uhd.types.TuneRequest(freq))

    # -- convenience passthroughs -------------------------------------
    def start_rx(self, on_page=None, on_status=None, on_spectrum=None):
        if on_page:
            self.receiver.on_page = on_page
        if on_status:
            self.receiver.on_status = on_status
        # Unconditional, unlike on_page/on_status above: None here means
        # "don't compute spectrum at all" (see PocsagReceiver._run()), a
        # real off switch a caller needs to be able to select, not just a
        # value to leave alone when not given.
        self.receiver.on_spectrum = on_spectrum
        self.receiver.start()

    def stop_rx(self):
        self.receiver.stop()

    def send(self, address, function, message, bitrate=None, blocking=False,
              protocol=None, on_done=None):
        # Defaults TX protocol to whatever the receiver is currently set to
        # -- the common case (testing one protocol end-to-end) -- but a
        # caller can override per-call to send the other protocol without
        # switching the receiver's own selection.
        protocol = protocol or self.receiver.protocol
        bitrate = bitrate or self.receiver.bitrate
        if blocking:
            self.transmitter.send(address, function, message, bitrate, protocol=protocol)
        else:
            self.transmitter.send_async(address, function, message, bitrate,
                                         protocol=protocol, on_done=on_done)

    def close(self):
        self.receiver.stop()
        # let any in-flight async TX finish rather than yanking the device out from under it
        if self.transmitter._async_thread is not None:
            self.transmitter._async_thread.join(timeout=5.0)
