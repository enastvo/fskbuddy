#!/usr/bin/env python3
"""
POCSAG protocol library: BCH(31,21) encode/decode, address/message codeword
packing, and batch/preamble assembly. This is the link-layer half of the
stack -- bit synchronization and frame/batch sync detection are done in
FPGA fabric (pocsag_bitsync.v / pocsag_framer.v); this module is what turns
raw 32-bit codewords (as captured by pocsag_framer, or about to be sent to
it) into/from actual pages (address + message).

Bit order convention used throughout: codewords are 32-bit integers where
bit 31 is the FIRST bit transmitted (the flag bit) and bit 0 is the LAST
(the parity bit) -- i.e. the natural "read left to right" MSB-first order
that also matches SYNC_WORD's usual hex representation.
"""

SYNC_WORD = 0x7CD215D8
IDLE_CODEWORD = 0x7A89C197  # standard POCSAG filler for unused frame slots

# BCH(31,21,5) generator polynomial: x^10+x^9+x^8+x^6+x^5+x^3+1, as an
# 11-bit value with the degree-10 leading coefficient at bit 10.
_BCH_GEN = 0x769


def _bch_syndrome(cw31):
    """Remainder of the 31-bit BCH codeword (21 info + 10 check bits) divided
    by the generator polynomial. Zero iff cw31 is a valid codeword."""
    reg = cw31
    for i in range(30, 9, -1):
        if (reg >> i) & 1:
            reg ^= (_BCH_GEN << (i - 10))
    return reg & 0x3FF


def bch_encode(info21):
    """21-bit info word (bit20 = flag, bits19..0 = data) -> 32-bit codeword
    (info21 << 11 | check10 << 1 | parity)."""
    assert 0 <= info21 < (1 << 21)
    cw31 = (info21 << 10) | _bch_syndrome(info21 << 10)
    parity = bin(cw31).count("1") & 1
    return (cw31 << 1) | parity


def _build_single_error_table():
    table = {}
    for pos in range(31):
        syn = _bch_syndrome(1 << pos)
        table[syn] = pos
    return table


_SINGLE_ERROR_SYNDROME = _build_single_error_table()


def bch_decode(cw32):
    """Returns (info21_or_None, error_count). error_count: 0 = clean,
    1 = corrected a single-bit error (data or parity), 2 = detected but
    uncorrectable (even number of errors, most likely 2), -1 = uncorrectable
    single-error pattern not in the table (shouldn't normally happen)."""
    assert 0 <= cw32 < (1 << 32)
    cw31 = cw32 >> 1
    syn = _bch_syndrome(cw31)
    total_parity = bin(cw32).count("1") & 1

    if syn == 0:
        if total_parity == 0:
            return cw31 >> 10, 0
        else:
            return cw31 >> 10, 1  # error confined to the parity bit itself
    else:
        if total_parity == 1:
            # odd total number of errors -> assume exactly one, in the 31-bit part
            if syn in _SINGLE_ERROR_SYNDROME:
                corrected = cw31 ^ (1 << _SINGLE_ERROR_SYNDROME[syn])
                return corrected >> 10, 1
            return None, -1
        else:
            return None, 2  # even number of errors (likely 2) -- detected, not corrected


# ---------------------------------------------------------------------------
# Address / function codewords

def encode_address(address, function):
    """address: 21-bit pager capcode (0..0x1FFFFF). function: 2-bit (0..3).
    Returns (codeword, frame_number) -- frame_number (0..7) is where this
    codeword must be placed within its batch; it's the low 3 bits of the
    address and is NOT part of the codeword itself."""
    assert 0 <= address < (1 << 21)
    assert 0 <= function < 4
    frame_number = address & 0x7
    addr18 = address >> 3
    info21 = (0 << 20) | (addr18 << 2) | function  # flag=0 -> address word
    return bch_encode(info21), frame_number


def decode_address_word(info21):
    """info21 from a decoded address codeword (flag already stripped/known 0).
    Returns (addr18, function). Combine addr18 with the frame number the
    codeword was found in to get the full 21-bit address."""
    function = info21 & 0x3
    addr18 = (info21 >> 2) & 0x3FFFF
    return addr18, function


# ---------------------------------------------------------------------------
# Numeric messages: 4-bit-per-digit, digits transmitted bit-reversed within
# each nibble. Character set per the standard POCSAG numeric table (0x0-0x9
# digits, 0xA spare/reserved -- decode-only, not offered for encoding --
# 0xB 'U' (urgency), 0xC space, 0xD hyphen, 0xE ']', 0xF '['). Verified
# against two independent transcriptions of the spec table; 0xC=space also
# matches the spec's own stated padding value (code 1100), which the
# earlier (wrong) table didn't -- a good independent cross-check.
NUMERIC_CHARS = "0123456789?U -]["
_NUMERIC_ENCODE = {c: i for i, c in enumerate(NUMERIC_CHARS) if c != "?"}  # 0xA is decode-only


def _reverse4(v):
    return int(f"{v:04b}"[::-1], 2)


def encode_numeric(text):
    """text: digits and the special chars in NUMERIC_CHARS. Returns a list
    of message codewords (flag=1)."""
    text = text.upper()
    nibbles = []
    for ch in text:
        if ch not in _NUMERIC_ENCODE:
            raise ValueError(f"{ch!r} not in POCSAG numeric character set {NUMERIC_CHARS!r}")
        nibbles.append(_reverse4(_NUMERIC_ENCODE[ch]))
    # pad to a multiple of 5 nibbles (20 bits) per codeword with the space char
    while len(nibbles) % 5:
        nibbles.append(_reverse4(_NUMERIC_ENCODE[" "]))

    codewords = []
    for i in range(0, len(nibbles), 5):
        data20 = 0
        for nib in nibbles[i:i + 5]:
            data20 = (data20 << 4) | nib
        info21 = (1 << 20) | data20  # flag=1 -> message word
        codewords.append(bch_encode(info21))
    return codewords


def decode_numeric(info21_list):
    """Inverse of encode_numeric: list of 20-bit data fields (flag already
    stripped) -> digit string (including trailing pad chars -- caller may
    want to rstrip())."""
    out = []
    for info21 in info21_list:
        data20 = info21 & 0xFFFFF
        for shift in (16, 12, 8, 4, 0):
            nib = (data20 >> shift) & 0xF
            val = _reverse4(nib)
            out.append(NUMERIC_CHARS[val] if val < len(NUMERIC_CHARS) else "?")
    return "".join(out)


# ---------------------------------------------------------------------------
# Alphanumeric messages: continuous stream of 7-bit ASCII, LSB-first,
# packed across message codewords' 20-bit data fields (MSB of the data
# field = earliest bit in the stream), zero-padded at the end.

def encode_alpha(text):
    bits = []
    for ch in text:
        code = ord(ch) & 0x7F
        for b in range(7):  # LSB first
            bits.append((code >> b) & 1)
    while len(bits) % 20:
        bits.append(0)

    codewords = []
    for i in range(0, len(bits), 20):
        data20 = 0
        for bit in bits[i:i + 20]:
            data20 = (data20 << 1) | bit
        info21 = (1 << 20) | data20
        codewords.append(bch_encode(info21))
    return codewords


def decode_alpha(info21_list):
    bits = []
    for info21 in info21_list:
        data20 = info21 & 0xFFFFF
        for shift in range(19, -1, -1):
            bits.append((data20 >> shift) & 1)

    chars = []
    for i in range(0, len(bits) - 6, 7):
        code = 0
        for b in range(7):  # LSB first
            code |= bits[i + b] << b
        if code == 0:
            continue  # padding
        chars.append(chr(code))
    return "".join(chars)


# ---------------------------------------------------------------------------
# Batch/transmission assembly

PREAMBLE_BITS = 576  # POCSAG minimum; alternating 1010...


def build_batches(address, function, message_codewords):
    """Places one address codeword at its mandated frame slot, then the
    message codewords in the slots immediately following (spilling into
    further batches as needed), idle-filling everything else. Returns a
    flat list of codewords: [SYNC, cw, cw, ..., SYNC, cw, cw, ...]."""
    addr_cw, frame_number = encode_address(address, function)

    slots = []

    def ensure(n):
        while len(slots) <= n:
            slots.append(None)

    addr_slot = frame_number * 2
    ensure(addr_slot)
    slots[addr_slot] = addr_cw

    idx = addr_slot + 1
    for cw in message_codewords:
        ensure(idx)
        slots[idx] = cw
        idx += 1

    while len(slots) % 16:
        slots.append(None)

    flat = []
    for b in range(0, len(slots), 16):
        flat.append(SYNC_WORD)
        for cw in slots[b:b + 16]:
            flat.append(cw if cw is not None else IDLE_CODEWORD)
    return flat


def build_bitstream(address, function, message_codewords, preamble_bits=PREAMBLE_BITS):
    """Full transmit bit sequence (preamble + batches) as a list of 0/1 ints,
    MSB-first per codeword (matches the FPGA framer's capture order)."""
    bits = [(i % 2) for i in range(preamble_bits)]  # 1010...
    for cw in build_batches(address, function, message_codewords):
        for b in range(31, -1, -1):
            bits.append((cw >> b) & 1)
    return bits


# ---------------------------------------------------------------------------
# Receive-side: turn a stream of raw codewords (as read from the FPGA
# framer) into decoded pages.

class Page:
    def __init__(self, address, function, msg_type, message):
        self.address = address
        self.function = function
        self.msg_type = msg_type
        self.message = message

    def __repr__(self):
        return f"Page(address={self.address}, function={self.function}, " \
               f"type={self.msg_type!r}, message={self.message!r})"


def parse_codewords(codewords, alpha_function=3):
    """codewords: sequence of raw 32-bit values -- either as captured live
    from the FPGA framer (which only reports the 16 payload words per
    batch; it never emits the SYNC_WORD itself, since codeword_valid only
    pulses once locked) or as produced by build_batches() (which does
    include SYNC_WORD entries). Batch boundaries are tracked with a
    modulo-16 counter that a literal SYNC_WORD also resets, so both
    sources work through the same code path. Returns a list of Page
    objects: a pending address is paired with whatever message codewords
    immediately follow it, up to the next address word or end of input (a
    message with no preceding address word it can attach to is dropped)."""
    pages = []
    pending_addr = None      # (addr18, function)
    pending_frame_addr = None
    pending_msgs = []        # list of raw info21 message data words

    def flush():
        nonlocal pending_addr, pending_msgs
        if pending_addr is not None and pending_msgs:
            addr18, function = pending_addr
            full_addr = (addr18 << 3) | pending_frame_addr
            if function == alpha_function:
                msg = decode_alpha(pending_msgs)
                msg_type = "alpha"
            else:
                msg = decode_numeric(pending_msgs)
                msg_type = "numeric"
            pages.append(Page(full_addr, function, msg_type, msg))
        pending_addr = None
        pending_msgs = []

    frame_in_batch = 0
    for raw in codewords:
        if raw == SYNC_WORD:
            frame_in_batch = 0
            continue
        if raw == IDLE_CODEWORD:
            frame_in_batch += 1
            if frame_in_batch >= 16:
                frame_in_batch = 0
            continue

        info21, errs = bch_decode(raw)
        frame_number = frame_in_batch // 2
        frame_in_batch += 1
        if frame_in_batch >= 16:
            frame_in_batch = 0
        if info21 is None:
            # See LiveParser.feed()'s matching comment -- can't tell if this
            # was an address or message word, so discard whatever's pending
            # rather than risk splicing an unrelated later page's message
            # onto this (now unknown) one's stale address.
            pending_addr = None
            pending_msgs = []
            continue  # uncorrectable, drop (and any page in progress)

        flag = (info21 >> 20) & 1
        if flag == 0:
            flush()
            pending_addr = decode_address_word(info21)
            pending_frame_addr = frame_number
        else:
            if pending_addr is not None:
                pending_msgs.append(info21)
    flush()
    return pages


class LiveParser:
    """Streaming counterpart to parse_codewords(), for polling loops that
    feed codewords in one at a time as they're captured. Critically, unlike
    parse_codewords() it never flushes on "end of input" -- there is no
    such thing while still listening -- only when a genuinely new address
    codeword arrives. feed() returns a completed Page, or None if nothing
    completed yet. IDLE_CODEWORD/SYNC_WORD entries increment/reset the
    16-slot batch counter exactly like parse_codewords(); the caller is
    free to skip feeding SYNC_WORD if its source (e.g. the FPGA framer)
    never reports it -- the modulo-16 rollover still tracks batches.

    on_error(raw, errs), if given, fires for a codeword BCH couldn't
    correct (errs 2 or -1, see bch_decode's docstring) -- feed()'s return
    value itself stays Page-or-None either way (unchanged, so existing
    callers -- pocsag_rx.py, pocsag_test_ota.py, pocsag_test_loopback.py --
    don't need to change) with the bad codeword just silently dropped as
    before unless a caller opts in to on_error. Deliberately NOT fired for
    an address with no message words before the next address (that's
    normal for a spec-compliant tone-only/ring-only page, not a decode
    failure)."""

    def __init__(self, alpha_function=3, on_error=None):
        self.alpha_function = alpha_function
        self.on_error = on_error or (lambda raw, errs: None)
        self._frame_in_batch = 0
        self._pending_addr = None
        self._pending_frame_addr = None
        self._pending_msgs = []

    def _flush(self):
        page = None
        if self._pending_addr is not None and self._pending_msgs:
            addr18, function = self._pending_addr
            full_addr = (addr18 << 3) | self._pending_frame_addr
            if function == self.alpha_function:
                msg = decode_alpha(self._pending_msgs)
                msg_type = "alpha"
            else:
                msg = decode_numeric(self._pending_msgs)
                msg_type = "numeric"
            page = Page(full_addr, function, msg_type, msg)
        self._pending_addr = None
        self._pending_msgs = []
        return page

    def feed(self, raw):
        if raw == SYNC_WORD:
            self._frame_in_batch = 0
            return None
        if raw == IDLE_CODEWORD:
            self._frame_in_batch = (self._frame_in_batch + 1) % 16
            return None

        info21, errs = bch_decode(raw)
        frame_number = self._frame_in_batch // 2
        self._frame_in_batch = (self._frame_in_batch + 1) % 16
        if info21 is None:
            self.on_error(raw, errs)
            # We can't tell whether the codeword that failed was meant to
            # be an address word (a new page starting) or a message word
            # (continuing the current one) -- the flag bit lives inside
            # the very payload we couldn't decode. Leaving _pending_addr/
            # _pending_msgs alone and just dropping this codeword (the old
            # behavior) meant: if it WAS an address word, the next real
            # message codewords -- meant for that new, now-unknown page --
            # would silently get appended onto the STALE previous page's
            # buffer instead, and the next _flush() would emit one page
            # with the old address but a garbled splice of two unrelated
            # pages' message text. Confirmed with a real repro (page A's
            # clean "AAAAA" became "AAAAA@PPPP\x10" once page B's address
            # word was corrupted). Discarding whatever was pending is a
            # real loss (a page that might otherwise have completed
            # cleanly), but silently mis-attributing garbled content to
            # the wrong capcode is worse -- prefer dropping the page over
            # returning corrupted output.
            self._pending_addr = None
            self._pending_msgs = []
            return None  # uncorrectable, drop (and any page in progress)

        flag = (info21 >> 20) & 1
        if flag == 0:
            page = self._flush()
            self._pending_addr = decode_address_word(info21)
            self._pending_frame_addr = frame_number
            return page
        else:
            if self._pending_addr is not None:
                self._pending_msgs.append(info21)
            return None
