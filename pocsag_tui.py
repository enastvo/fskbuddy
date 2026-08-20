#!/usr/bin/env python3
"""POCSAG Transceiver TUI -- styled after a retro green-phosphor radio
control terminal (see the reference image this was modeled on: boxed,
titled panels; a live clock; a function-key-style footer). Built with
Textual, the closest thing Python has to ratatui (reactive widgets,
CSS-like styling, a widget tree instead of hand-rolled curses drawing).

Runs the shared PocsagTransceiver (see transceiver.py) -- the same classes
the headless CLI (`pocsag_ctl.py send`/`listen`) uses -- so the TUI is a
thin presentation layer over the same TX/RX machinery, not a separate
implementation.
"""
import datetime
import json
import threading
import time
from collections import deque, namedtuple
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, Label, RichLog, Static

import pocsag as p
from transceiver import (
    PocsagTransceiver, PocsagReceiver, DEFAULT_FREQ, DEFAULT_RATE,
    DEFAULT_BITRATE, DEFAULT_DEVIATION_HZ,
)

ADDRESS_BOOK_PATH = Path(__file__).parent / "addresses.json"
STATION_PATH = Path(__file__).parent / "station.json"
LOGS_DIR = Path(__file__).parent / "logs"
BITRATES = [512, 1200, 2400]

# TUI-specific gain defaults. Used to be pinned to the B200mini's hardware
# ceilings (TX 89.75dB, RX 76dB) on the theory that there's no single sane
# default across setups so it should start hot and let the user dial down.
# That theory didn't survive contact with a real two-radio link: an
# empirical gain sweep between two boards on a bench (see the two-board
# troubleshooting session) found max gain badly overdrives the RX front
# end at typical close range -- CLIPPING, no LOCK, nothing decodes -- while
# 50/65 TX/RX locked and decoded cleanly with no clipping. Still just a
# starting point (dial in Settings, `s`, for your actual antenna distance/
# link budget), but one that's been shown to actually work on real
# hardware rather than one guaranteed to saturate the receiver. Deliberately
# NOT the same as transceiver.py's DEFAULT_TX_GAIN/DEFAULT_RX_GAIN (15/35),
# which the CLI send/listen subcommands still use and which are tuned for
# a same-board loopback link, a much shorter/stronger path than two
# separate radios over the air.
TUI_DEFAULT_TX_GAIN = 50.0
TUI_DEFAULT_RX_GAIN = 65.0

# Waterfall history: one row every WATERFALL_INTERVAL_S, WATERFALL_MAX_LINES
# kept -- a 10-minute horizon. The live spectrum bars above still refresh at
# PocsagReceiver's own (fast) SPECTRUM_INTERVAL_S; only how often a row gets
# appended to the waterfall's scrolling history is slowed down here.
WATERFALL_MAX_LINES = 200
WATERFALL_INTERVAL_S = 600.0 / WATERFALL_MAX_LINES  # 3s/row -> 200 rows = 10 minutes
WATERFALL_TICK_EVERY = round(60.0 / WATERFALL_INTERVAL_S)  # a time-scale label every ~60s
WATERFALL_LABEL_COLOR = "#22aa22"

# Spectrum scope: a live multi-row bar chart using eighth-block
# sub-character resolution, one character-column per FFT bin -- the SAME
# one-column-per-bin grid the waterfall below uses, so the two align
# exactly regardless of terminal font/width.
SPECTRUM_BAR_ROWS = 8
SPECTRUM_BAR_COLOR = "#33ff33"
_EIGHTHS = " ▁▂▃▄▅▆▇█"  # index = eighths filled, 0..8

# Messages panel: a decluttered RX/TX-only view, color-coded, separate from
# the noisier technical ACTIVITY LOG (sync/gain/decode-failure lines etc).
# Three colors, in priority order (see PocsagTUI._message_color):
#   1. OWN_MSG_COLOR  -- we sent it ourselves (direction == "TX"). Always
#      wins even in the edge case of paging your own capcode -- "this is
#      mine" is the more useful signal than "this was addressed to me".
#   2. TO_US_MSG_COLOR -- someone else's page addressed to our own capcode
#      (Settings' "Our address" / station.json) -- likely meant for us.
#   3. RX_MSG_COLOR   -- everything else received (promiscuous RX means
#      that's most traffic on a shared frequency).
RX_MSG_COLOR = "#4499ff"     # blue -- received, not addressed to us specifically
OWN_MSG_COLOR = "#888888"    # gray -- we sent it (dimmed -- you already know what you sent)
TO_US_MSG_COLOR = "#ffdd33"  # yellow -- received, addressed to our own capcode

# One record per logged message, kept around (PocsagTUI.messages) so the
# panel can be fully re-rendered on demand -- needed for the "hide own
# messages" filter to apply retroactively to everything already logged,
# not just new messages from the moment you toggle it. is_own is exactly
# (direction == "TX") -- POCSAG/GSC pages carry no real sender field, so
# "from us" is knowable only for messages this station itself transmitted;
# a received page's actual origin is simply unknowable from the protocol.
Message = namedtuple("Message", "ts direction to_address to_name message is_own")


def render_spectrum_bars(mags, floor, ceil, rows=SPECTRUM_BAR_ROWS, color=SPECTRUM_BAR_COLOR):
    span = max(ceil - floor, 1.0)
    total_levels = rows * 8
    levels = []
    for m in mags:
        lvl = int((m - floor) / span * total_levels)
        levels.append(max(0, min(lvl, total_levels)))
    lines = []
    for row in range(rows - 1, -1, -1):  # top row first
        base = row * 8
        chars = []
        for lvl in levels:
            sub = lvl - base
            if sub <= 0:
                chars.append(" ")
            elif sub >= 8:
                chars.append("█")
            else:
                chars.append(_EIGHTHS[sub])
        lines.append("".join(chars))
    return f"[{color}]" + "\n".join(lines) + f"[/{color}]"


def render_freq_axis(center_hz, span_hz, width_chars):
    """One line of left/center/right frequency labels, positioned by
    character index so it lines up under render_spectrum_bars' columns
    regardless of the panel's actual rendered width."""
    lo = (center_hz - span_hz / 2) / 1e6
    hi = (center_hz + span_hz / 2) / 1e6
    left = f"{lo:.4f}"
    center = f"{center_hz / 1e6:.4f} MHz"
    right = f"{hi:.4f}"
    width_chars = max(width_chars, len(left) + len(center) + len(right) + 4)
    line = [" "] * width_chars

    def place(s, start):
        for i, ch in enumerate(s):
            if 0 <= start + i < width_chars:
                line[start + i] = ch

    place(left, 0)
    place(center, (width_chars - len(center)) // 2)
    place(right, width_chars - len(right))
    return "".join(line)


# Waterfall: one RichLog line per (slow-throttled) spectrum snapshot, each
# column a solid block colored by an RGB thermal heatmap (blue -> cyan ->
# green -> yellow -> red) -- explicitly NOT the rest of the UI's green
# theme, a real spectrum-analyzer-style color scale as requested.
def _make_heatmap_table(n=32):
    table = []
    for i in range(n):
        t = i / (n - 1)
        if t < 0.25:
            u = t / 0.25
            r, g, b = 0, int(255 * u), 255
        elif t < 0.5:
            u = (t - 0.25) / 0.25
            r, g, b = 0, 255, int(255 * (1 - u))
        elif t < 0.75:
            u = (t - 0.5) / 0.25
            r, g, b = int(255 * u), 255, 0
        else:
            u = (t - 0.75) / 0.25
            r, g, b = 255, int(255 * (1 - u)), 0
        table.append(f"#{r:02x}{g:02x}{b:02x}")
    return table


HEATMAP_COLORS = _make_heatmap_table(32)
WATERFALL_BLOCK = "█"


def render_waterfall_line(mags, floor, ceil):
    """One line of heatmap-colored block characters for a spectrum
    snapshot, scaled against a slowly-adapting (floor, ceil) rather than
    each snapshot's own min/max -- so a real signal shows up as a bright
    band against a stable background instead of every line always
    spanning the full color range regardless of whether anything's
    actually there."""
    span = max(ceil - floor, 1.0)
    nlevels = len(HEATMAP_COLORS)
    parts = []
    for m in mags:
        level = int((m - floor) / span * (nlevels - 1))
        level = max(0, min(level, nlevels - 1))
        color = HEATMAP_COLORS[level]
        parts.append(f"[{color}]{WATERFALL_BLOCK}[/{color}]")
    return "".join(parts)


def format_waterfall_time_label(elapsed_s):
    m, s = divmod(int(round(elapsed_s)), 60)
    return f"-{m}:{s:02d}"


def render_waterfall_rows(rows, tick_every=WATERFALL_TICK_EVERY,
                           interval_s=WATERFALL_INTERVAL_S, label_color=WATERFALL_LABEL_COLOR):
    """rows: newest-first iterable of render_waterfall_line() output (see
    the deque this is fed from in PocsagTUI -- appendleft'd on arrival, so
    index 0 is always "now"). Returns lines in the same newest-first order,
    with an elapsed-time label appended every `tick_every` rows -- a
    trailing suffix, not a prefix, so it doesn't disturb the left-edge
    column alignment with the spectrum bars above (see render_spectrum_bars/
    render_waterfall_line's shared one-character-per-bin grid)."""
    lines = []
    for i, blocks in enumerate(rows):
        if i % tick_every == 0:
            label = format_waterfall_time_label(i * interval_s)
            lines.append(f"{blocks} [{label_color}]{label}[/{label_color}]")
        else:
            lines.append(blocks)
    return lines


def load_address_book():
    try:
        with open(ADDRESS_BOOK_PATH) as f:
            data = json.load(f)
        return {int(k): v for k, v in data.items()}
    except Exception:
        return {}


def save_address_book(book):
    try:
        with open(ADDRESS_BOOK_PATH, "w") as f:
            json.dump({str(k): v for k, v in book.items()}, f, indent=2)
    except Exception:
        pass


def lookup_name(book, address):
    return book.get(address, "")


def load_station():
    """This station's own capcode -- purely identifying/informational (see
    the README: nothing filters RX by it, that's a separate concern from
    the Settings "Address filter"). Persisted separately from
    addresses.json since it's a single station-wide value, not another
    entry in the address book."""
    try:
        with open(STATION_PATH) as f:
            data = json.load(f)
        addr = data.get("own_address")
        return int(addr) if addr is not None else None
    except Exception:
        return None


def save_station(own_address):
    try:
        with open(STATION_PATH, "w") as f:
            json.dump({"own_address": own_address}, f, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Modals

class TransmitModal(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, address_book):
        super().__init__()
        self.address_book = address_book

    def compose(self) -> ComposeResult:
        panel = Vertical(id="tx-modal")
        panel.border_title = "TRANSMIT PAGE"
        with panel:
            yield Label("Address (capcode number, or a saved nickname):")
            yield Input(placeholder="1234567 or nickname", id="tx-address")
            yield Label("Message type -- a=alpha, n=numeric:")
            yield Input(value="a", id="tx-type")
            yield Label("Message:")
            yield Input(placeholder="message text", id="tx-message")
            yield Label("", id="tx-error")
            yield Label("[Enter on Message to send -- Esc to cancel]", classes="hint")

    def action_cancel(self):
        self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "tx-message":
            self.focus_next()
            return
        addr_raw = self.query_one("#tx-address", Input).value.strip()
        type_raw = (self.query_one("#tx-type", Input).value.strip().lower() or "a")
        message = self.query_one("#tx-message", Input).value

        address = None
        if addr_raw.isdigit():
            address = int(addr_raw)
        else:
            for addr, name in self.address_book.items():
                if name.lower() == addr_raw.lower():
                    address = addr
                    break
        err = self.query_one("#tx-error", Label)
        if address is None:
            err.update(f"Unknown address/nickname: {addr_raw!r}")
            return
        if not (0 <= address < (1 << 21)):
            err.update("Address must be 0..2097151 (21-bit capcode)")
            return
        if not message:
            err.update("Message can't be empty")
            return
        function = 3 if type_raw.startswith("a") else 0
        self.dismiss((address, function, message))


class SettingsModal(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, tx_gain, rx_gain, bitrate, address_filter, own_address, freq):
        super().__init__()
        self.init_tx_gain = tx_gain
        self.init_rx_gain = rx_gain
        self.init_bitrate = bitrate
        self.init_filter = address_filter
        self.init_own_address = own_address
        self.init_freq = freq

    def compose(self) -> ComposeResult:
        panel = Vertical(id="settings-modal")
        panel.border_title = "RADIO SETTINGS"
        with panel:
            yield Label("Frequency (MHz) -- only transmit where you're licensed to:")
            yield Input(value=f"{self.init_freq/1e6:.4f}", id="s-freq")
            yield Label("TX gain (dB, 0-89.75):")
            yield Input(value=str(self.init_tx_gain), id="s-txgain")
            yield Label("RX gain (dB, 0-76):")
            yield Input(value=str(self.init_rx_gain), id="s-rxgain")
            yield Label("Bitrate (512/1200/2400):")
            yield Input(value=str(self.init_bitrate), id="s-bitrate")
            yield Label("Address filter (blank = show all pages):")
            yield Input(value=("" if self.init_filter is None else str(self.init_filter)),
                        id="s-filter")
            yield Label("Our address (this station's own capcode, blank = not set):")
            yield Input(value=("" if self.init_own_address is None else str(self.init_own_address)),
                        id="s-ownaddr")
            yield Label("", id="s-error")
            yield Label("[Enter on last field to apply -- Esc to cancel]", classes="hint")

    def action_cancel(self):
        self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "s-ownaddr":
            self.focus_next()
            return
        err = self.query_one("#s-error", Label)
        try:
            freq = float(self.query_one("#s-freq", Input).value) * 1e6
            tx_gain = float(self.query_one("#s-txgain", Input).value)
            rx_gain = float(self.query_one("#s-rxgain", Input).value)
            bitrate = int(self.query_one("#s-bitrate", Input).value)
            filt_raw = self.query_one("#s-filter", Input).value.strip()
            address_filter = int(filt_raw) if filt_raw else None
            own_raw = self.query_one("#s-ownaddr", Input).value.strip()
            own_address = int(own_raw) if own_raw else None
        except ValueError:
            err.update("Frequency, TX gain, RX gain, bitrate, address filter, our address "
                       "must be numbers")
            return
        if bitrate not in BITRATES:
            err.update(f"Bitrate must be one of {BITRATES}")
            return
        # B200mini's RF front end range (Ettus spec) -- a sanity bound, not
        # a substitute for actually knowing what you're licensed to
        # transmit on (see this field's own label, and the README).
        if not (70e6 <= freq <= 6e9):
            err.update("Frequency must be 70-6000 MHz (B200mini RF range)")
            return
        if own_address is not None and not (0 <= own_address < (1 << 21)):
            err.update("Our address must be 0..2097151 (21-bit capcode)")
            return
        self.dismiss((freq, tx_gain, rx_gain, bitrate, address_filter, own_address))


class HelpModal(ModalScreen):
    BINDINGS = [Binding("escape", "close", "Close"), Binding("q", "close", "Close")]

    def compose(self) -> ComposeResult:
        panel = Vertical(id="help-modal")
        panel.border_title = "HELP"
        with panel:
            yield Static(
                "POCSAG TRANSCEIVER\n\n"
                "t   transmit a page\n"
                "r   toggle RX on/off\n"
                "s   settings (frequency, gains, bitrate, address filter, our address --\n"
                "    only transmit on a frequency you're actually licensed to use)\n"
                "c   add current/last-seen address to address book\n"
                "o   hide/show messages we sent ourselves in MESSAGES\n"
                "h/? this help\n"
                "q   quit\n\n"
                "MESSAGES panel: each entry shows To:/From: clearly -- From\n"
                "is US for anything we transmitted (dimmed gray) or RF for\n"
                "anything received (POCSAG/GSC carry no real sender field,\n"
                "so RF is as specific as it gets). A received page addressed\n"
                "to our own capcode (Settings' \"Our address\") is highlighted\n"
                "yellow instead of the default blue.\n\n"
                "PHY (bit sync, batch/frame sync, ~20kHz channel filter) runs\n"
                "in FPGA fabric; BCH decode and message assembly happen here\n"
                "in software. See pocsag/README.md for the full writeup.\n\n"
                "[Esc or q to close]"
            )

    def action_close(self):
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Main app

class PocsagTUI(App):
    CSS_PATH = "pocsag_tui.css"
    TITLE = "POCSAG TRANSCEIVER"
    BINDINGS = [
        Binding("t", "transmit", "Transmit"),
        Binding("r", "toggle_rx", "Toggle RX"),
        Binding("s", "settings", "Settings"),
        Binding("c", "add_contact", "Add contact"),
        Binding("o", "toggle_own_filter", "Hide own"),
        Binding("h,question_mark", "help", "Help"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, freq=DEFAULT_FREQ, rate=DEFAULT_RATE, tx_gain=TUI_DEFAULT_TX_GAIN,
                 rx_gain=TUI_DEFAULT_RX_GAIN, bitrate=DEFAULT_BITRATE,
                 deviation_hz=DEFAULT_DEVIATION_HZ, autostart_rx=True, serial=None,
                 show_spectrum=True, show_waterfall=True):
        super().__init__()
        self.freq = freq
        self.rate = rate
        self.tx_gain = tx_gain
        self.rx_gain = rx_gain
        self.bitrate = bitrate
        self.deviation_hz = deviation_hz
        self.autostart_rx = autostart_rx
        self.serial = serial
        self.show_spectrum = show_spectrum
        self.show_waterfall = show_waterfall

        self.address_book = load_address_book()
        self.transceiver = None
        self.address_filter = None
        self.own_address = load_station()
        self.rig_online = False
        self.locked = False
        self.clipping = False
        self.channel_width_narrow = False
        self.channel_width_locked = False
        self.rx_running = False
        self.n_codewords = 0
        self.n_pages_rx = 0
        self.n_pages_tx = 0
        self.last_seen_address = None
        self.messages = []  # full history, see the Message namedtuple above -- re-rendered
                             # from this on every new message and every filter toggle
        self.hide_own_messages = False
        self._spec_floor = None
        self._spec_ceil = None
        self._last_waterfall_t = 0.0
        # Newest-first (appendleft'd) -- see render_waterfall_rows. maxlen
        # matches WATERFALL_MAX_LINES so the buffer itself enforces the
        # 10-minute horizon; no separate trim needed.
        self._waterfall_rows = deque(maxlen=WATERFALL_MAX_LINES)

        # Persistent log file, one per session -- ACTIVITY LOG and MESSAGES
        # are both in-memory RichLogs that vanish on quit, so there was
        # previously nothing to go back and review after the fact. Opened
        # here (not lazily) so a session that fails before on_mount still
        # leaves a file; plain text, no Rich markup, full date+time (the
        # on-screen HH:MM:SS timestamps don't carry a date since a single
        # run never spans midnight in practice, but a log file might be
        # read back days later).
        self._log_file = None
        try:
            LOGS_DIR.mkdir(exist_ok=True)
            log_path = LOGS_DIR / f"pocsag_{datetime.datetime.now():%Y%m%d_%H%M%S}.log"
            self._log_file = open(log_path, "a", buffering=1)  # line-buffered
            self.log_file_path = log_path
        except Exception:
            self.log_file_path = None

    # -- layout ---------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Static(self._topbar_text(), id="topbar")
        with Horizontal(id="row1"):
            freq_panel = Vertical(id="freq-panel", classes="panel")
            freq_panel.border_title = "FREQUENCY / MODE"
            with freq_panel:
                yield Static(id="freq-content")

            settings_panel = Vertical(id="settings-panel", classes="panel")
            settings_panel.border_title = "RADIO SETTINGS"
            with settings_panel:
                yield Static(id="settings-content")

            status_panel = Vertical(id="status-panel", classes="panel")
            status_panel.border_title = "STATUS"
            with status_panel:
                yield Static(id="status-content")

        with Vertical(id="row2"):
            if self.show_spectrum:
                spectrum_panel = Vertical(id="spectrum-panel", classes="panel")
                spectrum_panel.border_title = "SPECTRUM SCOPE"
                with spectrum_panel:
                    yield Static(id="spectrum-bars")
                    yield Static(id="spectrum-freq-axis")

            if self.show_waterfall:
                waterfall_panel = Vertical(id="waterfall-panel", classes="panel")
                waterfall_panel.border_title = "WATERFALL (10 min, falling)"
                with waterfall_panel:
                    # auto_scroll=False deliberately -- newest row is written
                    # first (top) on every rebuild, not appended at the
                    # bottom, so the view should stay pinned at the top
                    # rather than Textual's usual "follow the latest write"
                    # behavior. See _handle_spectrum.
                    yield RichLog(id="waterfall", wrap=False, highlight=False, markup=True,
                                  max_lines=WATERFALL_MAX_LINES, auto_scroll=False)

        with Horizontal(id="row3"):
            addr_panel = Vertical(id="addr-panel", classes="panel")
            addr_panel.border_title = "MEMORY (ADDRESS BOOK)"
            with addr_panel:
                yield Static(id="addr-content")

            messages_panel = Vertical(id="messages-panel", classes="panel")
            messages_panel.border_title = "MESSAGES (own gray / to-you yellow) ('o' hides own)"
            with messages_panel:
                # min_width=1, not the RichLog default of 78: with wrap=True
                # and the default shrink=True on write(), RichLog still
                # forces its rendered width back up to min_width afterward
                # (see write()'s `render_width = max(render_width,
                # self.min_width)`), which silently defeats wrapping/
                # shrinking and forces a horizontal scrollbar in any panel
                # narrower than 78 columns -- both this and #log now are,
                # since row3 went from 2 columns to 3.
                yield RichLog(id="messages", wrap=True, highlight=False, markup=True, min_width=1)

            log_panel = Vertical(id="log-panel", classes="panel")
            log_panel.border_title = "ACTIVITY LOG"
            with log_panel:
                yield RichLog(id="log", wrap=True, highlight=False, markup=False, min_width=1)

        yield Footer()

    def on_mount(self):
        self.set_interval(1.0, self._tick_clock)
        self._refresh_panels()
        if self.log_file_path is not None:
            self._log(f"Logging this session to {self.log_file_path}")
        self._connect_hardware()

    def _safe_call(self, fn, *args):
        """Run fn(*args) on the Textual UI thread. Some callbacks (e.g.
        PocsagReceiver.start()/stop()'s on_log calls) fire synchronously on
        whatever thread calls them -- the UI thread for a TUI action like
        toggling RX, but the receiver's own worker thread for its live
        polling loop -- so unconditionally wrapping every call in
        call_from_thread() breaks the UI-thread case (Textual raises if
        call_from_thread is invoked from the app's own thread). Check
        first, same test Textual itself uses internally."""
        if threading.get_ident() == self._thread_id:
            fn(*args)
        else:
            self.call_from_thread(fn, *args)

    # -- hardware connection ---------------------------------------------
    @work(thread=True)
    def _connect_hardware(self):
        self._safe_call(self._log, "Opening USRP...")
        try:
            tc = PocsagTransceiver(
                freq=self.freq, rate=self.rate, tx_gain=self.tx_gain,
                rx_gain=self.rx_gain, bitrate=self.bitrate,
                deviation_hz=self.deviation_hz, serial=self.serial,
                on_log=lambda msg: self._safe_call(self._log, msg),
            )
        except Exception as e:
            self._safe_call(self._log, f"FAILED to open device: {e}")
            return
        self._safe_call(self._hardware_ready, tc)

    def _hardware_ready(self, tc):
        self.transceiver = tc
        self.rig_online = True
        self._log("Device ready.")
        self._refresh_panels()
        if self.autostart_rx:
            self.action_toggle_rx()

    # -- periodic / helpers -----------------------------------------------
    def _topbar_text(self):
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        link = "CONNECTED" if self.rig_online else "CONNECTING"
        return f"POCSAG TRANSCEIVER        {now}        LINK: {link}"

    def _tick_clock(self):
        try:
            self.query_one("#topbar", Static).update(self._topbar_text())
        except Exception:
            pass

    def _write_log_file(self, line):
        if self._log_file is not None:
            try:
                now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self._log_file.write(f"{now}  {line}\n")
            except Exception:
                pass

    def _log(self, msg):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        try:
            self.query_one("#log", RichLog).write(f"{ts}  {msg}")
        except Exception:
            pass
        self._write_log_file(msg)

    def _log_message(self, direction, to_address, message):
        """Records one message (RX or TX) and re-renders the whole
        MESSAGES panel from history -- see the Message namedtuple and
        _render_messages for why a full rebuild rather than an append
        (the "hide own" filter needs to apply retroactively). The log
        file, unlike the live panel, always gets every message regardless
        of the filter -- filtering is a display concern, not what's kept
        for after-the-fact review."""
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        is_own = (direction == "TX")
        name = lookup_name(self.address_book, to_address)
        record = Message(ts, direction, to_address, name, message, is_own)
        self.messages.append(record)
        who = " (you)" if to_address == self.own_address else (f" ({name})" if name else "")
        self._write_log_file(f"MSG {direction}  to={to_address}{who}: {message!r}")
        self._render_messages()

    def _message_color(self, record):
        """Priority order -- see the color constants' own comments for
        the reasoning: own (gray) beats addressed-to-us (yellow) beats
        plain received (blue)."""
        if record.is_own:
            return OWN_MSG_COLOR
        if record.to_address == self.own_address:
            return TO_US_MSG_COLOR
        return RX_MSG_COLOR

    def _render_messages(self):
        """Full rebuild from self.messages, same "clear + rewrite"
        approach render_waterfall_rows already uses for a similar need
        (a toggle -- there, WATERFALL_MAX_LINES trimming; here,
        hide_own_messages -- that has to apply to everything already
        logged, not just what's appended from here on)."""
        try:
            log = self.query_one("#messages", RichLog)
        except Exception:
            return
        log.clear()
        for record in self.messages:
            if self.hide_own_messages and record.is_own:
                continue
            color = self._message_color(record)
            from_label = "US" if record.is_own else "RF"
            who = " (you)" if record.to_address == self.own_address else (
                f" ({record.to_name})" if record.to_name else "")
            log.write(f"[{color}]{record.ts}  To: {record.to_address}{who}   "
                      f"From: {from_label}[/{color}]")
            log.write(f"[{color}]  {record.message}[/{color}]")
        log.scroll_end(animate=False)

    def _refresh_panels(self):
        try:
            hidden_note = " -- own hidden ('o' to show)" if self.hide_own_messages else " ('o' hides own)"
            self.query_one("#messages-panel", Vertical).border_title = (
                f"MESSAGES (own gray / to-you yellow){hidden_note}")
            if not self.channel_width_locked:
                channel_str = "detecting..."
            elif self.channel_width_narrow:
                channel_str = "NARROW (12.5kHz)"
            else:
                channel_str = "WIDE (25kHz)"
            self.query_one("#freq-content", Static).update(
                f"Freq     {self.freq/1e6:.4f} MHz\n"
                f"Bitrate  {self.bitrate} bps\n"
                f"Deviation {self.deviation_hz:.0f} Hz\n"
                f"Rate     {self.rate/1e3:.0f} kHz\n"
                f"Channel  {channel_str}"
            )
            # "ALL" rather than a bare "none" -- makes it explicit at a
            # glance that with no filter set, every decoded page (any
            # capcode) is received and logged, not just ones already in the
            # address book. Nothing in the FPGA or software filters by
            # address unless this is set.
            filt = "ALL (promiscuous)" if self.address_filter is None else str(self.address_filter)
            own = "not set" if self.own_address is None else str(self.own_address)
            self.query_one("#settings-content", Static).update(
                f"TX gain  {self.tx_gain:.1f} dB\n"
                f"RX gain  {self.rx_gain:.1f} dB\n"
                f"Addr filter  {filt}\n"
                f"Our address  {own}"
            )
            lock_str = "YES" if self.locked else "no"
            rx_str = "RUNNING" if self.rx_running else "stopped"
            clip_str = "[#ff5555 bold]YES -- lower RX gain![/]" if self.clipping else "no"
            self.query_one("#status-content", Static).update(
                f"RIG      {'ONLINE' if self.rig_online else 'connecting...'}\n"
                f"RX       {rx_str}\n"
                f"LOCK     {lock_str}\n"
                f"CLIPPING {clip_str}\n"
                f"Codewords {self.n_codewords}\n"
                f"Pages RX  {self.n_pages_rx}   TX  {self.n_pages_tx}"
            )
            if self.address_book:
                lines = [f"{addr:>8}  {name}" for addr, name in
                         sorted(self.address_book.items(), key=lambda kv: kv[1])]
            else:
                lines = ["(empty -- press 'c' after receiving", " a page to save its address)"]
            self.query_one("#addr-content", Static).update("\n".join(lines))
        except Exception:
            pass

    # -- receiver callbacks (normally invoked on the receiver's worker
    # thread, hence _safe_call rather than a bare call_from_thread) -------
    def _on_page(self, page):
        self._safe_call(self._handle_page, page)

    def _handle_page(self, page):
        self.n_pages_rx += 1
        self.last_seen_address = page.address
        name = lookup_name(self.address_book, page.address)
        who = f" ({name})" if name else ""
        self._log(f"RX  addr={page.address}{who} func={page.function} "
                   f"type={page.msg_type} msg={page.message!r}")
        self._log_message("RX", page.address, page.message)
        self._refresh_panels()

    def _on_status(self, **kw):
        self._safe_call(self._handle_status, kw)

    def _handle_status(self, kw):
        self.locked = kw.get("locked", self.locked)
        self.clipping = kw.get("clipping", self.clipping)
        self.n_codewords = kw.get("n_codewords", self.n_codewords)
        self.channel_width_narrow = kw.get("channel_width_narrow", self.channel_width_narrow)
        self.channel_width_locked = kw.get("channel_width_locked", self.channel_width_locked)
        self._refresh_panels()

    def _on_spectrum(self, mags, rate):
        self._safe_call(self._handle_spectrum, mags, rate)

    def _handle_spectrum(self, mags, rate):
        if not mags:
            return
        lo, hi = min(mags), max(mags)
        alpha = 0.05  # slow EMA -- see render_waterfall_line's docstring for why
        if self._spec_floor is None:
            self._spec_floor, self._spec_ceil = lo, hi
        else:
            self._spec_floor = (1 - alpha) * self._spec_floor + alpha * lo
            self._spec_ceil = (1 - alpha) * self._spec_ceil + alpha * hi

        if self.show_spectrum:
            try:
                # Live bars: every call (PocsagReceiver's own
                # SPECTRUM_INTERVAL_S cadence, ~7/sec) -- this is the
                # "right now" reading.
                bars = render_spectrum_bars(mags, self._spec_floor, self._spec_ceil)
                self.query_one("#spectrum-bars", Static).update(bars)
                axis = render_freq_axis(self.freq, PocsagReceiver.SPECTRUM_DISPLAY_SPAN_HZ,
                                         len(mags))
                self.query_one("#spectrum-freq-axis", Static).update(axis)
            except Exception:
                pass

        if self.show_waterfall:
            # Throttled separately to WATERFALL_INTERVAL_S so
            # WATERFALL_MAX_LINES of history covers a full 10 minutes,
            # instead of scrolling out of view in well under a minute at
            # the live bars' own fast refresh rate.
            now = time.monotonic()
            if now - self._last_waterfall_t >= WATERFALL_INTERVAL_S:
                self._last_waterfall_t = now
                line = render_waterfall_line(mags, self._spec_floor, self._spec_ceil)
                # Newest at the front -- falling waterfall (new row enters at
                # the top, existing rows fall toward the bottom, oldest
                # evicted off the bottom once WATERFALL_MAX_LINES is full)
                # rather than the previous rising/scroll-up behavior. Full
                # rebuild each tick (only every ~3s, so cheap) since RichLog
                # itself only supports appending at the bottom, not
                # prepending at the top.
                self._waterfall_rows.appendleft(line)
                try:
                    wf = self.query_one("#waterfall", RichLog)
                    wf.clear()
                    for rendered in render_waterfall_rows(self._waterfall_rows):
                        wf.write(rendered)
                    wf.scroll_home(animate=False)  # pin the view at the top (newest)
                except Exception:
                    pass

    # -- actions ------------------------------------------------------
    def action_transmit(self):
        if self.transceiver is None:
            self._log("Can't transmit -- device not ready yet.")
            return

        def handle_result(result):
            if result is None:
                return
            address, function, message = result
            name = lookup_name(self.address_book, address)
            who = f" ({name})" if name else ""
            self._log(f"TX  addr={address}{who} func={function} sending...")
            self._log_message("TX", address, message)
            self.n_pages_tx += 1
            self._refresh_panels()
            self.transceiver.send(address, function, message, bitrate=self.bitrate,
                                   on_done=lambda: self._safe_call(self._log, "TX done."))

        self.push_screen(TransmitModal(self.address_book), handle_result)

    def action_toggle_rx(self):
        if self.transceiver is None:
            self._log("Can't start RX -- device not ready yet.")
            return
        if self.rx_running:
            self.transceiver.stop_rx()
            self.rx_running = False
        else:
            # Only wire the spectrum callback (and thus pay for the FFT at
            # all -- see PocsagReceiver._run()) if some panel actually wants
            # it.
            want_spectrum = self.show_spectrum or self.show_waterfall
            self.transceiver.start_rx(on_page=self._on_page, on_status=self._on_status,
                                       on_spectrum=self._on_spectrum if want_spectrum else None)
            self.rx_running = True
        self._refresh_panels()

    def action_settings(self):
        def handle_result(result):
            if result is None:
                return
            freq, tx_gain, rx_gain, bitrate, address_filter, own_address = result
            freq_changed = (freq != self.freq)
            self.freq = freq
            self.tx_gain = tx_gain
            self.rx_gain = rx_gain
            self.bitrate = bitrate
            self.address_filter = address_filter
            if own_address != self.own_address:
                self.own_address = own_address
                save_station(own_address)
            if self.transceiver is not None:
                if freq_changed:
                    self.transceiver.request_freq(freq)
                self.transceiver.transmitter.tx_gain = tx_gain
                self.transceiver.receiver.request_gain(rx_gain)
                self.transceiver.receiver.request_bitrate(bitrate)
                self.transceiver.receiver.set_address_filter(address_filter)
            self._log(f"Settings updated: freq={freq/1e6:.4f}MHz TX={tx_gain}dB "
                       f"RX={rx_gain}dB bitrate={bitrate}bps filter={address_filter} "
                       f"our_address={own_address}")
            self._refresh_panels()

        self.push_screen(
            SettingsModal(self.tx_gain, self.rx_gain, self.bitrate, self.address_filter,
                          self.own_address, self.freq),
            handle_result,
        )

    def action_add_contact(self):
        if self.last_seen_address is None:
            self._log("No address seen yet to save.")
            return
        addr = self.last_seen_address
        name = f"contact-{addr}"
        self.address_book[addr] = name
        save_address_book(self.address_book)
        self._log(f"Saved {addr} as {name!r} (edit addresses.json to rename).")
        self._refresh_panels()

    def action_toggle_own_filter(self):
        self.hide_own_messages = not self.hide_own_messages
        self._render_messages()
        self._refresh_panels()
        self._log(f"MESSAGES: own-sent messages {'hidden' if self.hide_own_messages else 'shown'}.")

    def action_help(self):
        self.push_screen(HelpModal())

    def action_quit(self):
        if self.transceiver is not None:
            self._log("Closing device...")
            self.transceiver.close()
        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:
                pass
        self.exit()


def main():
    app = PocsagTUI()
    app.run()


if __name__ == "__main__":
    main()
