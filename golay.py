#!/usr/bin/env python3
# Copyright (C) 2026 Estefan Nastvogel
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Standard binary Golay(23,12,7) encode/decode -- the forward error
correction GSC (Golay Sequential Code) paging uses for its control word,
activation code, and address codewords (see gsc.py for the GSC-specific
framing built on top of this).

Deliberately a separate file from gsc.py: this math is textbook and
independent of any GSC-specific convention (generator polynomial, syndrome
decoding, the "perfect code" error-table construction below are all
standard coding theory, cross-checked against a known reference
implementation -- see _GOLAY_GEN's comment). gsc.py's *framing* -- comma
patterns, half-bit spacing, batch structure, address bit-packing -- is a
different matter: confirmed against a primary source (a Motorola patent
describing GSC as background art) where possible, but GSC infrastructure
is largely defunct and there's no independent reference implementation to
validate interop against (unlike POCSAG, where SDRangel/multimon-ng served
as real cross-checks). Keeping the solid, independently-verifiable math
here separate from the necessarily-more-provisional framing work in gsc.py
makes that split visible in the file layout, not just in comments.

Mirrors pocsag.py's bch_encode/bch_decode shape and conventions (same
(corrected_info_or_None, error_count) decode return convention, same
systematic-cyclic-code construction) rather than inventing a new API
style for this project.
"""
from itertools import combinations

# g(x) = x^11+x^10+x^6+x^5+x^4+x^2+1, 12-bit generator (bit 11 leading
# coefficient implicit, matching pocsag.py's _BCH_GEN convention). This is
# one of two reciprocal generator polynomials commonly cited for the
# (23,12) Golay code (the other, x^11+x^9+x^7+x^6+x^5+x+1, gives an
# equivalent but bit-reversed code) -- chosen to match a known, citable
# reference implementation (eccpage.com's golay23.c, GENPOL=0x00000c75)
# rather than picking arbitrarily between the two.
_GOLAY_GEN = 0xC75


def _golay_syndrome(cw23):
    """Remainder of the 23-bit Golay codeword divided by the generator
    polynomial (an 11-degree polynomial, 12-bit representation with the
    leading coefficient implicit). Zero iff cw23 is a valid codeword."""
    reg = cw23
    for i in range(22, 10, -1):
        if (reg >> i) & 1:
            reg ^= (_GOLAY_GEN << (i - 11))
    return reg & 0x7FF  # 11-bit remainder


def golay_encode(info12):
    """12-bit info word -> 23-bit systematic codeword: data in bits 0-11,
    parity in bits 12-22 (info12 | parity11 << 12). Data-low/parity-high,
    NOT the data-high/parity-low layout pocsag.py's bch_encode uses --
    deliberately matches multimon-ng's bch.c (bch_golay_encode: same
    generator polynomial 0xC75, confirmed by independent cross-check
    against eccpage.com's reference implementation; same systematic
    construction; this specific bit arrangement is what makes our encoding
    byte patterns match theirs, not just the underlying math) rather than
    inventing a third convention -- see gsc.py's module docstring for why
    this matters (multimon-ng is the closest thing to a real, independent
    GSC reference implementation available)."""
    assert 0 <= info12 < (1 << 12)
    return info12 | (_golay_syndrome(info12 << 11) << 12)


def _build_error_table():
    """Golay(23,12,7) is a PERFECT code: every one of the 2^11=2048
    possible syndromes corresponds to exactly one error pattern of Hamming
    weight 0..3. This isn't a coincidence -- it's the combinatorial
    identity C(23,0)+C(23,1)+C(23,2)+C(23,3) = 1+23+253+1771 = 2048 = 2^11,
    an exact match, which is the definition of a perfect code (every
    possible received word is within distance 3 of exactly one codeword).
    So rather than a single-error table like pocsag.py's
    _build_single_error_table, this exhaustively enumerates all weight
    0..3 error patterns once at import time and builds a COMPLETE
    syndrome -> error-pattern table -- every syndrome is covered, and the
    build asserts that (no gaps, no collisions) rather than hoping."""
    table = {0: 0}
    for weight in (1, 2, 3):
        for positions in combinations(range(23), weight):
            err = 0
            for p in positions:
                err |= (1 << p)
            syn = _golay_syndrome(err)
            assert syn not in table, f"perfect code property violated at weight {weight}"
            table[syn] = err
    assert len(table) == 2048, f"incomplete syndrome table: {len(table)}/2048"
    return table


_GOLAY_ERROR_TABLE = _build_error_table()


def golay_decode(cw23):
    """Returns (info12_or_None, error_count). error_count: 0-3 = corrected
    that many bit errors. Never returns None in practice -- Golay(23,12,7)
    is a perfect code, so _GOLAY_ERROR_TABLE covers every possible
    syndrome by construction (asserted at import time above); None/-1 is
    kept only for API symmetry with pocsag.bch_decode, which (being an
    imperfect code) can genuinely fail to find a correction."""
    assert 0 <= cw23 < (1 << 23)
    syn = _golay_syndrome(cw23)
    if syn == 0:
        return cw23 & 0xFFF, 0
    err = _GOLAY_ERROR_TABLE.get(syn)
    if err is None:
        return None, -1
    corrected = cw23 ^ err
    return corrected & 0xFFF, bin(err).count("1")
