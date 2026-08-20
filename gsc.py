#!/usr/bin/env python3
"""
GSC (Golay Sequential Code) protocol library -- the link-layer half of the
stack, mirroring pocsag.py's structure and API shape (same
encode_numeric/encode_alpha/Page/LiveParser conventions) but for GSC
instead of POCSAG. Sits on golay.py the same way pocsag.py sits on its own
BCH(31,21) math.

Framing facts below come from two sources, and are labeled accordingly --
read this before trusting any specific number:

(1) US Patent 4,427,980 ("Encoder for Transmitted Message Activation
    Code", Motorola/Fennell et al., 1984) -- describes GSC as background
    art, not the patent's own invention, so independent of what the
    patent itself claims.

(2) multimon-ng's demod_gsc.c/bch.c (github.com/EliasOenal/multimon-ng,
    public domain) -- a REAL, independent, field-used GSC decoder
    implementation (not a paraphrase), whose own comments cite "Table
    VIII, Section 3.2" of what was clearly a real GSC technical spec the
    author had access to. Confirmed generator polynomial 0xC75 matches
    (1)'s independent citation too (eccpage.com's reference Golay
    implementation) -- three independent sources agreeing is about as
    solid as this gets for a protocol this obscure.

CONFIRMED / ADOPTED (matches multimon-ng bit-for-bit or numerically,
verified in this project's own test suite -- see golay.py's cross-check
against bch_golay_encode()):
  - Golay(23,12) generator polynomial 0xC75, systematic encoding with
    DATA IN THE LOW 12 BITS, parity in the high 11 (golay.py was
    originally built the other way around and corrected specifically for
    this -- see golay_encode's docstring).
  - Comma: 28-bit alternating pattern, 600 baud.
  - Each Golay codeword bit is transmitted TWICE in a row (600 baud
    symbol rate throughout -- NOT two different bit rates as an earlier
    reading of the patent alone suggested; "300bps address rate" is the
    EFFECTIVE rate after 2x repetition, not a separate clock). Confirmed
    directly from demod_gsc.c's read_dup_golay(): "the 46-bit block is a
    23-bit Golay codeword with each bit transmitted twice (600 baud, 300
    bps effective)". This simplifies the half-bit gap too: it's exactly
    ONE symbol period at the same fixed 600-baud rate, not a fractional-
    bit waveform -- no new sub-bit modulation support needed after all.
  - Half-bit gap polarity: opposite Word 2's first bit (patent); comma's
    first bit matches Word 1's first bit (patent).
  - Bit order: LSB-to-the-left (patent) -- confirmed independently by
    multimon-ng's read_dup_golay building the codeword LSB-first from the
    bitstream (`codeword |= (bit << i)` for i=0..22 in transmission order).
  - Control/start word: fixed constant. multimon-ng's real value is
    GSC_START_CODE = 713 -- adopted directly (CONTROL_WORD_INFO below)
    rather than inventing our own, since it costs nothing and gives real
    grounding. Word 2 = Word 1's codeword bitwise-complemented (patent;
    verified computationally in golay.py's test suite that this always
    yields another valid codeword, since the all-ones 23-bit word is
    itself a valid Golay codeword).
  - Activation code: multimon-ng's real value is 2563 -- adopted directly
    (ACTIVATION_CODE_INFO below).
  - Preamble: patent describes it as a system-configurable battery-saver
    group identifier, not a universal constant -- but multimon-ng hardcodes
    10 real preamble_values it decodes against, meaning the industry did
    converge on 10 standard values in practice. Adopted directly
    (PREAMBLE_VALUES below) instead of inventing our own.
  - Alpha/numeric character tables: multimon-ng's real GSC tables (6-bit
    alpha, two 4-bit numeric tables with a shift prefix) are DIFFERENT
    from POCSAG's own (7-bit ASCII / single 4-bit table) -- adopted
    directly (ALPHA_TABLE/NUMERIC_TABLE/NUMERIC_SHIFT_TABLE below) rather
    than reusing pocsag.py's tables as originally planned.
  - Real GSC's address Word 1 comes from a genuine 100-word subset (not a
    guess -- multimon-ng hardcodes the real 50 low-range values,
    word1s[50] in its source, with a matching high range at +50); adopted
    directly (WORD1_TABLE below).

STILL OUR OWN CONVENTION (not yet replicated -- see below for why):
  - Real GSC's Word 2 -> address-digit arithmetic (multimon-ng's
    reverse_word2(): a mixed-radix decode -- raw=w2 or w2-50, split into
    two 2-digit fields, one doubled, recombined, then divided down into
    three decimal digits, validated against two illegal-value tables
    citing "Table VIII" of a real spec) is a genuine, real, non-guessed
    algorithm -- but intricate enough (a real base-conversion trick, not
    a simple bit-slice) that fully inverting it into an ENCODER wasn't
    done here. Given the actual payoff -- real GSC networks are defunct,
    so this only affects which specific pager NUMBER an address maps to,
    never whether our own TX/RX talk to each other correctly -- this was
    judged not worth the additional reverse-engineering risk right now.
    address_to_words()/words_to_address() below use a direct bit-packed
    mapping instead (own convention, clearly not attempting this specific
    piece of compliance). Left as a documented, well-scoped follow-on if
    ever wanted: the algorithm above is real and citable, not a dead end.
  - Real GSC uses a separate, smaller (15,7) BCH code for data blocks
    (distinct from address/control/activation's Golay(23,12) -- confirmed
    by both the patent AND multimon-ng's bch.c, which has a full
    bch_gsc_encode/correct implementation for it, generator 0x117). Not
    adopted: we use Golay(23,12) uniformly for data blocks too, to avoid
    a second FEC implementation for comparatively little benefit in a
    self-consistent system.
  - Deviation: not in the patent; a separate SDR decoder-config reference
    (not multimon-ng, not a spec) gave ~2000Hz shift, 2600Hz bandwidth --
    kept as DEFAULT_DEVIATION_HZ below, still unverified against real
    hardware, the same way POCSAG's own deviation needed real-hardware
    tuning before it worked.
  - rtl_433 was checked directly (its actual devices/ source list) and
    has no pager/POCSAG/GSC/FLEX decoder at all -- it targets short
    OOK/ASK ISM-band bursts (weather stations, tire sensors), a different
    problem than continuous 2-FSK paging streams.
"""
import golay
from golay import golay_encode, golay_decode

DEFAULT_DEVIATION_HZ = 2000.0  # see module docstring -- unverified against real hardware yet

COMMA_LEN = 28  # bits, alternating pattern, 600 baud -- see module docstring
BIT_REPEAT = 2  # each Golay codeword bit sent twice in a row -- see module docstring

# -- Adopted from multimon-ng (see module docstring) -----------------------
CONTROL_WORD_INFO = 713      # GSC_START_CODE
ACTIVATION_CODE_INFO = 2563  # GSC_ACTIVATION_CODE

PREAMBLE_VALUES = (2030, 1628, 3198, 647, 191, 3315, 1949, 2540, 1560, 2335)

WORD1_TABLE = (
    721, 2731, 2952, 1387, 1578, 1708, 2650, 1747, 2580, 1376, 2692, 696, 1667, 3800, 3552, 3424, 1384,
    3595, 876, 3124, 2285, 2608, 899, 3684, 3129, 2124, 1287, 2616, 1647, 3216, 375, 1232, 2824, 1840,
    408, 3127, 3387, 882, 3468, 3267, 1575, 3463, 3152, 2572, 1252, 2592, 1552, 835, 1440, 160,
)  # low range (g1g0 0-49); the high range (50-99) is understood to add 50 -- see reverse_word2 in multimon-ng

ALPHA_TABLE = (
    " !\"#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\r]\0_"
)  # 6-bit alpha -> ASCII, from multimon-ng's alpha_table

NUMERIC_TABLE = "0123456789\0U -*\0"       # 4-bit -> char, unshifted
NUMERIC_SHIFT_TABLE = "ABCDE FGHJ\0LNPR?"  # 4-bit -> char, after a 0xF shift prefix


def _bit_reverse(value, width):
    """LSB-to-the-left bit order (see module docstring) -- reverses the
    bit order of a `width`-bit value."""
    result = 0
    for i in range(width):
        if (value >> i) & 1:
            result |= 1 << (width - 1 - i)
    return result


def comma_bits(first_bit):
    """28-bit alternating comma, starting with `first_bit` (0 or 1) --
    must match the polarity of the following Word 1's own first bit."""
    return [(first_bit + i) % 2 for i in range(COMMA_LEN)]


def half_bit_gap_polarity(word2_first_bit):
    """The gap between Word 1 and Word 2 is the OPPOSITE polarity of
    Word 2's first bit -- and per the module docstring, exactly ONE
    symbol period at the same fixed 600-baud rate everything else runs
    at (not a true fractional-bit duration)."""
    return 1 - word2_first_bit


def _codeword_symbols(cw23):
    """23-bit Golay codeword -> 46 transmission symbols: LSB-to-the-left
    bit order, each bit repeated twice in a row (see module docstring)."""
    reversed_cw = _bit_reverse(cw23, 23)
    symbols = []
    for i in range(23):
        bit = (reversed_cw >> (22 - i)) & 1
        symbols.append(bit)
        symbols.append(bit)
    return symbols


def build_word_pair_bits(word1_cw23, word2_cw23):
    """Comma + Word 1 (46 symbols) + half-bit gap (1 symbol) + Word 2 (46
    symbols) -- all at one uniform symbol rate now (see module docstring),
    so this is a plain bit list suitable for the same kind of per-bit sps
    modulation modem.py's modulate_cpfsk already does. No special
    fractional-duration handling needed."""
    w1 = _codeword_symbols(word1_cw23)
    w2 = _codeword_symbols(word2_cw23)
    out = list(comma_bits(w1[0]))
    out += w1
    out.append(half_bit_gap_polarity(w2[0]))
    out += w2
    return out


# ---------------------------------------------------------------------------
# Control word / activation code (batch/frame delimiters, analogous role to
# POCSAG's SYNC_WORD)

def control_word():
    """Returns (word1_cw23, word2_cw23) -- Word 2 is Word 1's bitwise
    complement (see module docstring)."""
    w1 = golay_encode(CONTROL_WORD_INFO)
    return w1, w1 ^ 0x7FFFFF


def activation_code():
    w1 = golay_encode(ACTIVATION_CODE_INFO)
    return w1, w1 ^ 0x7FFFFF


# ---------------------------------------------------------------------------
# Full transmission assembly (mirrors pocsag.py's build_bitstream). Batch
# structure: lead-in preamble (our own convention -- see PREAMBLE_BITS)
# then one control-word block, one address block, N data blocks, and a
# trailing control-word block (which serves no addressing purpose here --
# it just forces gsc_framer.v/LiveParser to recognize a new block boundary
# and flush the pending page, the same role a following page's own control
# word would otherwise play). Every block (control, address, data) gets
# its own full comma+Word1+gap+Word2 -- unlike real GSC, which can chain
# multiple blocks off a single preamble within a batch (see multimon-ng's
# 856-bit preamble + per-block "1-bit inverted comma" instead of a full
# 28-bit comma for blocks after the first) -- our own simpler, self-
# consistent convention: gsc_framer.v resyncs on every block instead of
# tracking batch position, trading a little airtime for a simpler framer
# (see the project's plan notes for the framer design this enables).

PREAMBLE_BITS = 200  # alternating lead-in for bit-sync convergence before
                      # the first comma -- our own value, no real-GSC spec
                      # source for it (real GSC's own 18x-repeated preamble
                      # codewords serve a battery-saving role we don't
                      # implement, not a bit-sync role specifically) --
                      # shorter than POCSAG's 576 since the comma itself
                      # is also alternating and gives pocsag_bitsync.v-style
                      # timing recovery a further head start.


def build_bitstream(address, function, message):
    """Full GSC transmission: preamble + control word + address + message
    data blocks + trailing control word, as a flat list of 0/1 ints ready
    for modulate_cpfsk (same shape pocsag.py's build_bitstream returns).
    function 3 = alphanumeric, matching this project's own convention
    (same as pocsag.py's -- see its own note on this not being a real
    POCSAG/GSC standard, just this project's choice)."""
    bits = [(i % 2) for i in range(PREAMBLE_BITS)]
    bits += build_word_pair_bits(*control_word())
    bits += build_word_pair_bits(*encode_address(address, function))

    payloads = encode_alpha(message) if function == 3 else encode_numeric(message)
    for payload in payloads:
        bits += build_word_pair_bits(*encode_data_payload(payload))

    bits += build_word_pair_bits(*control_word())  # flush marker -- see docstring
    return bits


# ---------------------------------------------------------------------------
# Address / data blocks. Address digit arithmetic is NOT the real GSC
# scheme (see module docstring) -- this is our own direct bit-packed
# convention: 24 combined info bits (12+12):
#   bit23 (MSB)   = flag: 0 = address block, 1 = data block
#   address block: bits22:21 = function (2 bits), bits20:0 = address (21 bits)
#   data block:    bits22:0  = raw payload (23 bits)

def encode_address(address, function):
    """address: 21-bit capcode. function: 2-bit, this project's own
    convention (function 3 = alphanumeric), same as pocsag.py's. Returns
    (word1_cw23, word2_cw23)."""
    assert 0 <= address < (1 << 21)
    assert 0 <= function < 4
    combined = (0 << 23) | (function << 21) | address
    return golay_encode(combined >> 12), golay_encode(combined & 0xFFF)


def decode_address_words(word1_info12, word2_info12):
    """Inverse of encode_address. Returns (address, function). Caller must
    have already confirmed flag==0 (see decode_block_flag)."""
    combined = (word1_info12 << 12) | word2_info12
    function = (combined >> 21) & 0x3
    address = combined & 0x1FFFFF
    return address, function


def encode_data_payload(payload23):
    assert 0 <= payload23 < (1 << 23)
    combined = (1 << 23) | payload23
    return golay_encode(combined >> 12), golay_encode(combined & 0xFFF)


def decode_data_payload(word1_info12, word2_info12):
    """Caller must have already confirmed flag==1."""
    combined = (word1_info12 << 12) | word2_info12
    return combined & 0x7FFFFF


def decode_block_flag(word1_info12, word2_info12):
    return (word1_info12 >> 11) & 0x1


# ---------------------------------------------------------------------------
# Numeric / alphanumeric data payloads -- continuous bitstream packed
# across 23-bit blocks, using the REAL GSC character tables above (not
# POCSAG's -- see module docstring). Numeric only uses the unshifted table
# for now (no 0xF-shift-prefix support yet -- a real gap, not silently
# assumed complete: any char requiring the shift table raises).

def encode_numeric(text):
    text = text.upper()
    bits = []
    for ch in text:
        if ch not in NUMERIC_TABLE or ch == "\0":
            raise ValueError(f"{ch!r} not in GSC's unshifted numeric table "
                              f"{NUMERIC_TABLE!r} (shift-prefixed chars not supported yet)")
        val = NUMERIC_TABLE.index(ch)
        for b in range(3, -1, -1):
            bits.append((val >> b) & 1)
    while len(bits) % 23:
        bits.append(0)
    return [_bits_to_int(bits[i:i + 23]) for i in range(0, len(bits), 23)]


def decode_numeric(payload23_list, n_chars):
    """n_chars: how many characters to decode (payload is bit-packed
    without natural alignment to 23-bit block boundaries, so the caller
    must track the original message length)."""
    bits = []
    for payload in payload23_list:
        bits.extend(_int_to_bits(payload, 23))
    chars = []
    for i in range(0, n_chars * 4, 4):
        if i + 4 > len(bits):
            break
        val = _bits_to_int(bits[i:i + 4])
        chars.append(NUMERIC_TABLE[val] if val < len(NUMERIC_TABLE) and NUMERIC_TABLE[val] != "\0" else "?")
    return "".join(chars)


def encode_alpha(text):
    text = text.upper()  # GSC's real alpha table (unlike POCSAG's 7-bit ASCII) has no lowercase at all
    bits = []
    for ch in text:
        if ch not in ALPHA_TABLE:
            raise ValueError(f"{ch!r} not in GSC's alpha character set {ALPHA_TABLE!r}")
        val = ALPHA_TABLE.index(ch)
        for b in range(5, -1, -1):  # 6-bit, MSB first
            bits.append((val >> b) & 1)
    while len(bits) % 23:
        bits.append(0)
    return [_bits_to_int(bits[i:i + 23]) for i in range(0, len(bits), 23)]


def decode_alpha(payload23_list):
    bits = []
    for payload in payload23_list:
        bits.extend(_int_to_bits(payload, 23))
    chars = []
    for i in range(0, len(bits) - 5, 6):
        val = _bits_to_int(bits[i:i + 6])
        ch = ALPHA_TABLE[val] if val < len(ALPHA_TABLE) else "?"
        if ch == "\0":
            continue  # padding
        chars.append(ch)
    return "".join(chars)


def _bits_to_int(bits):
    v = 0
    for b in bits:
        v = (v << 1) | b
    return v


def _int_to_bits(value, width):
    return [(value >> shift) & 1 for shift in range(width - 1, -1, -1)]


# ---------------------------------------------------------------------------
# Receive-side: turn a stream of decoded (word1_info12, word2_info12) pairs
# into Page objects. One pair per "block" (address, control, or data).

class Page:
    def __init__(self, address, function, msg_type, message):
        self.address = address
        self.function = function
        self.msg_type = msg_type
        self.message = message

    def __repr__(self):
        return f"Page(address={self.address}, function={self.function}, " \
               f"type={self.msg_type!r}, message={self.message!r})"


class LiveParser:
    """Streaming GSC decoder: feed it raw (word1_raw23, word2_raw23)
    codeword pairs as captured (still Golay-encoded -- FEC decode happens
    here, same division of labor as pocsag.LiveParser gets from
    pocsag_framer.v). Mirrors pocsag.LiveParser's contract closely,
    INCLUDING its defensive intent -- discard whatever page is pending on
    an untrustworthy-looking pair, rather than leave it around to silently
    absorb a later, unrelated block's data -- but NOT the same trigger
    condition, because Golay(23,12,7) is a PERFECT code (see golay.py):
    unlike POCSAG's BCH(31,21), which can genuinely detect-and-fail
    (return None) on some corrupted codewords, Golay ALWAYS "succeeds" --
    every possible 23-bit value is within distance 3 of exactly one valid
    codeword, so golay_decode() essentially never returns None in
    practice (confirmed empirically: a 6-bit corruption in this project's
    own test suite decoded "cleanly" with 0 reported errors instead of
    failing). Copying pocsag.LiveParser's `if info is None` trigger
    verbatim here would silently disable the whole defensive mechanism it
    exists to provide -- the analogous, actually-meaningful signal for a
    perfect code is a HIGH CORRECTED ERROR COUNT: a 3-bit correction (the
    maximum Golay can do) is real signal that the received word was
    heavily corrupted, even though it "succeeded" -- treated here with the
    same suspicion pocsag.LiveParser gives an outright failure."""

    # Golay(23,12,7) corrects up to 3 bit errors; treat hitting that
    # ceiling as low-confidence (see class docstring) rather than as a
    # trustworthy decode.
    MAX_TRUSTED_ERRORS = 2

    def __init__(self, alpha_function=3, on_error=None):
        self.alpha_function = alpha_function
        self.on_error = on_error or (lambda word1, word2, errs: None)
        self._pending_addr = None       # (address, function)
        self._pending_payloads = []     # list of 23-bit data payload ints

    def _flush(self):
        page = None
        if self._pending_addr is not None and self._pending_payloads:
            address, function = self._pending_addr
            if function == self.alpha_function:
                msg = decode_alpha(self._pending_payloads)
                msg_type = "alpha"
            else:
                msg = decode_numeric(self._pending_payloads,
                                      n_chars=len(self._pending_payloads) * 23 // 4)
                msg_type = "numeric"
            page = Page(address, function, msg_type, msg)
        self._pending_addr = None
        self._pending_payloads = []
        return page

    def feed(self, word1_raw23, word2_raw23):
        info1, errs1 = golay_decode(word1_raw23)
        info2, errs2 = golay_decode(word2_raw23)
        # info is (essentially) never None -- see class docstring on why a
        # perfect code needs the error-count check below instead.
        untrusted = (info1 is None or info2 is None
                     or errs1 > self.MAX_TRUSTED_ERRORS or errs2 > self.MAX_TRUSTED_ERRORS)
        if untrusted:
            self.on_error(word1_raw23, word2_raw23, max(errs1, errs2))
            self._pending_addr = None
            self._pending_payloads = []
            return None

        if info1 == CONTROL_WORD_INFO or info1 == ACTIVATION_CODE_INFO:
            return self._flush()  # delimits blocks, like a mini-batch-boundary

        flag = decode_block_flag(info1, info2)
        if flag == 0:
            page = self._flush()
            self._pending_addr = decode_address_words(info1, info2)
            return page
        else:
            if self._pending_addr is not None:
                self._pending_payloads.append(decode_data_payload(info1, info2))
            return None
