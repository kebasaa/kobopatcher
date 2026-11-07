# Port of the deflate algorithm of zlib 1.3.1 (Jean-loup Gailly and Mark Adler),
# zlib license (see LICENSE.zlib next to this file).
#
#   Copyright (C) 1995-2024 Jean-loup Gailly and Mark Adler
#
# This is a pure-Python transcription of deflate.c (deflate_slow,
# longest_match, fill_window and the hash chains) and trees.c (_tr_tally,
# _tr_flush_block, build_tree, gen_bitlen, gen_codes, scan_tree, send_tree,
# build_bl_tree, send_all_trees, compress_block, _tr_stored_block) for the
# parameters of compress2(..., Z_DEFAULT_COMPRESSION): level 6, windowBits 15,
# memLevel 8, Z_DEFAULT_STRATEGY. Its output is byte-identical to classic
# zlib's (1.2.x and 1.3.x) so that kobopatcher works when Python's zlib module
# is linked against zlib-ng, which compresses differently.
#
# Differences in form, not in output:
#
# * Positions are absolute offsets into the input instead of offsets into a
#   64 KiB sliding window. The window slides are still simulated (`slid`, the
#   absolute offset of window[0]) because they decide which hash entries are
#   NIL and whether a block may still be sent stored (block_start >= 0).
# * The rolling hash ins_h is computed per position up front; with all input
#   available in one call (compress2) every position is hashed in sequence,
#   so this gives the same values as UPDATE_HASH.
# * The hash chains (head[]/prev[]) are kept as per-hash lists of positions,
#   see _deflate_slow; longest_match walks the same candidates in the same
#   order with the same chain_length/limit cut-offs and picks the same match.
# * zlib reads up to MAX_MATCH bytes past the end of the input in
#   longest_match (stale window data); any such comparison makes the match at
#   least `lookahead` long, which ends the search (nice_match is clamped to
#   lookahead) and is truncated to `lookahead`, so the bytes read do not
#   influence the result. Here comparisons stop at the end of the input.
# * The 'sym_buf' (zlib >= 1.2.12) / 'd_buf'+'l_buf' (zlib 1.2.8) symbol
#   buffer is two Python lists; both versions flush a block after
#   lit_bufsize - 1 = 16383 symbols.
#
# zlib versions: as far as known, classic zlib's output for these parameters
# has not changed between 1.2.x (kobopatch used go-libz, i.e. Sortix libz,
# a zlib 1.2.8 fork) and 1.3.1. The changes in that range that touch the
# code ported here do not affect it: deflate_stored() was rewritten (level 0
# only); the window slide moved to slide_hash() and copies only the valid
# part of the window (only stale bytes past the data differ, see above);
# 1.2.12 replaced d_buf/l_buf by sym_buf (same 16383-symbol block limit); the
# Z_FIXED test in _tr_flush_block() moved (Z_FIXED only); 1.3.1's LIT_MEM
# option (off by default) only changes the symbol buffer layout. All the
# zlib streams in Kobo's nickel binaries (made by an older zlib) recompress
# to the same bytes with this port and with zlib 1.3.1.

import struct
from bisect import bisect_right
from collections import defaultdict
import sys
from array import array

try:
    from zlib import adler32 as _adler32
except ImportError:  # pragma: no cover - zlib is part of every CPython build
    _adler32 = None

__all__ = ["compress"]

# --- deflate.h / zutil.h ---------------------------------------------------

Z_DEFLATED = 8
MIN_MATCH = 3
MAX_MATCH = 258
TOO_FAR = 4096

W_BITS = 15  # windowBits
W_SIZE = 1 << W_BITS
W_MASK = W_SIZE - 1
WINDOW_SIZE = 2 * W_SIZE
MEM_LEVEL = 8  # DEF_MEM_LEVEL
HASH_BITS = MEM_LEVEL + 7
HASH_SIZE = 1 << HASH_BITS
HASH_MASK = HASH_SIZE - 1
HASH_SHIFT = (HASH_BITS + MIN_MATCH - 1) // MIN_MATCH  # 5
LIT_BUFSIZE = 1 << (MEM_LEVEL + 6)  # 16384
SYM_END = LIT_BUFSIZE - 1  # sym_end / 3 (1.2.12+), lit_bufsize-1 (1.2.8)
MIN_LOOKAHEAD = MAX_MATCH + MIN_MATCH + 1
MAX_DIST = W_SIZE - MIN_LOOKAHEAD

# configuration_table: good_length, max_lazy, nice_length, max_chain, func
CONFIGURATION_TABLE = (
    (0, 0, 0, 0, "deflate_stored"),  # 0 store only
    (4, 4, 8, 4, "deflate_fast"),  # 1 max speed, no lazy matches
    (4, 5, 16, 8, "deflate_fast"),  # 2
    (4, 6, 32, 32, "deflate_fast"),  # 3
    (4, 4, 16, 16, "deflate_slow"),  # 4 lazy matches
    (8, 16, 32, 32, "deflate_slow"),  # 5
    (8, 16, 128, 128, "deflate_slow"),  # 6
    (8, 32, 128, 256, "deflate_slow"),  # 7
    (32, 128, 258, 1024, "deflate_slow"),  # 8
    (32, 258, 258, 4096, "deflate_slow"),  # 9 max compression
)
LEVEL = 6  # Z_DEFAULT_COMPRESSION

# --- trees.c ----------------------------------------------------------------

LENGTH_CODES = 29
LITERALS = 256
L_CODES = LITERALS + 1 + LENGTH_CODES  # 286
D_CODES = 30
BL_CODES = 19
HEAP_SIZE = 2 * L_CODES + 1
MAX_BITS = 15
MAX_BL_BITS = 7
END_BLOCK = 256
REP_3_6 = 16
REPZ_3_10 = 17
REPZ_11_138 = 18
STORED_BLOCK = 0
STATIC_TREES = 1
DYN_TREES = 2

EXTRA_LBITS = (0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 5, 0)
EXTRA_DBITS = (0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12, 12, 13, 13)
EXTRA_BLBITS = (0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2, 3, 7)
BL_ORDER = (16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15)


def _bi_reverse(code, length):
    """Reverse the first `length` bits of a code (1 <= length <= 15)."""
    res = 0
    while True:
        res |= code & 1
        code >>= 1
        res <<= 1
        length -= 1
        if length <= 0:
            break
    return res >> 1


_REV8 = [_bi_reverse(i, 8) for i in range(256)]


def _bi_reverse_fast(code, length):
    return ((_REV8[code & 0xFF] << 8) | _REV8[(code >> 8) & 0xFF]) >> (16 - length)


def _gen_codes(length, code, max_code, bl_count):
    next_code = [0] * (MAX_BITS + 1)
    c = 0
    for bits in range(1, MAX_BITS + 1):
        c = (c + bl_count[bits - 1]) << 1
        next_code[bits] = c & 0xFFFF
    for n in range(max_code + 1):
        ln = length[n]
        if ln == 0:
            continue
        code[n] = _bi_reverse_fast(next_code[ln], ln)
        next_code[ln] += 1


def _tr_static_init():
    """tr_static_init(): the length/distance tables and static trees."""
    base_length = [0] * LENGTH_CODES
    length_code = [0] * 256
    length = 0
    for code in range(LENGTH_CODES - 1):
        base_length[code] = length
        for _ in range(1 << EXTRA_LBITS[code]):
            length_code[length] = code
            length += 1
    # length 258 (lc 255) gets its own code
    length_code[length - 1] = LENGTH_CODES - 1

    base_dist = [0] * D_CODES
    dist_code = [0] * 512
    dist = 0
    for code in range(16):
        base_dist[code] = dist
        for _ in range(1 << EXTRA_DBITS[code]):
            dist_code[dist] = code
            dist += 1
    dist >>= 7
    for code in range(16, D_CODES):
        base_dist[code] = dist << 7
        for _ in range(1 << (EXTRA_DBITS[code] - 7)):
            dist_code[256 + dist] = code
            dist += 1

    static_llen = [0] * (L_CODES + 2)
    static_lcode = [0] * (L_CODES + 2)
    bl_count = [0] * (MAX_BITS + 1)
    for n in range(L_CODES + 2):
        ln = 8 if n <= 143 else 9 if n <= 255 else 7 if n <= 279 else 8
        static_llen[n] = ln
        bl_count[ln] += 1
    _gen_codes(static_llen, static_lcode, L_CODES + 1, bl_count)
    static_dlen = [5] * D_CODES
    static_dcode = [_bi_reverse(n, 5) for n in range(D_CODES)]
    return base_length, length_code, base_dist, dist_code, static_llen, static_lcode, static_dlen, static_dcode


(
    BASE_LENGTH,
    LENGTH_CODE,
    BASE_DIST,
    DIST_CODE,
    STATIC_LLEN,
    STATIC_LCODE,
    STATIC_DLEN,
    STATIC_DCODE,
) = _tr_static_init()


def _d_code(dist):
    return DIST_CODE[dist] if dist < 256 else DIST_CODE[256 + (dist >> 7)]


# d_code(dist - 1) for a match distance 1..32768, and the literal/length
# symbol for a match length - MIN_MATCH
DCODE_OF_DIST = [0] + [_d_code(d - 1) for d in range(1, W_SIZE + 1)]
LSYM_OF_LC = [LENGTH_CODE[lc] + LITERALS + 1 for lc in range(256)]

# static_tree_desc: static tree lengths, extra bits, extra base, elems, max_length
STATIC_L_DESC = (STATIC_LLEN, EXTRA_LBITS, LITERALS + 1, L_CODES, MAX_BITS)
STATIC_D_DESC = (STATIC_DLEN, EXTRA_DBITS, 0, D_CODES, MAX_BITS)
STATIC_BL_DESC = (None, EXTRA_BLBITS, 0, BL_CODES, MAX_BL_BITS)


_ZEROS = [0] * L_CODES


class _Tree:
    """ct_data dyn_tree[] (Freq/Code and Dad/Len kept in separate lists) + tree_desc."""

    __slots__ = ("freq", "code", "length", "dad", "max_code", "stat_desc")

    def __init__(self, size, stat_desc):
        self.freq = [0] * size
        self.code = [0] * size
        self.length = [0] * size
        self.dad = [0] * size
        self.max_code = 0
        self.stat_desc = stat_desc


class _State:
    """The trees.c half of deflate_state plus the output (pending) buffer."""

    def __init__(self):
        self.dyn_ltree = _Tree(HEAP_SIZE, STATIC_L_DESC)
        self.dyn_dtree = _Tree(2 * D_CODES + 1, STATIC_D_DESC)
        self.bl_tree = _Tree(2 * BL_CODES + 1, STATIC_BL_DESC)
        self.heap = [0] * (2 * L_CODES + 1)
        self.depth = [0] * (2 * L_CODES + 1)
        self.bl_count = [0] * (MAX_BITS + 1)
        self.opt_len = 0
        self.static_len = 0
        self.sym_dist = []
        self.sym_lc = []
        self.out = bytearray()
        self.bi_buf = 0
        self.bi_valid = 0
        self.init_block()

    # --- bit output -------------------------------------------------------

    def send_bits(self, value, length):
        self.bi_buf |= value << self.bi_valid
        self.bi_valid += length
        if self.bi_valid >= 64:
            self.out += (self.bi_buf & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "little")
            self.bi_buf >>= 64
            self.bi_valid -= 64

    def bi_windup(self):
        nbytes = (self.bi_valid + 7) >> 3
        if nbytes:
            self.out += self.bi_buf.to_bytes(nbytes, "little")
        self.bi_buf = 0
        self.bi_valid = 0

    # --- trees ------------------------------------------------------------

    def init_block(self):
        lf = self.dyn_ltree.freq
        lf[:L_CODES] = _ZEROS[:L_CODES]
        self.dyn_dtree.freq[:D_CODES] = _ZEROS[:D_CODES]
        self.bl_tree.freq[:BL_CODES] = _ZEROS[:BL_CODES]
        lf[END_BLOCK] = 1
        self.opt_len = self.static_len = 0
        self.sym_dist.clear()
        self.sym_lc.clear()

    def pqdownheap(self, freq, k, heap_len):
        heap = self.heap
        depth = self.depth
        v = heap[k]
        fv = freq[v]
        dv = depth[v]
        j = k << 1
        while j <= heap_len:
            hj = heap[j]
            if j < heap_len:
                hj1 = heap[j + 1]
                # smaller(tree, heap[j+1], heap[j], depth)
                if freq[hj1] < freq[hj] or (freq[hj1] == freq[hj] and depth[hj1] <= depth[hj]):
                    j += 1
                    hj = hj1
            # smaller(tree, v, heap[j], depth)
            if fv < freq[hj] or (fv == freq[hj] and dv <= depth[hj]):
                break
            heap[k] = hj
            k = j
            j <<= 1
        heap[k] = v

    def gen_bitlen(self, desc, heap_max):
        freq, length, dad = desc.freq, desc.length, desc.dad
        max_code = desc.max_code
        stree, extra, base, _elems, max_length = desc.stat_desc
        heap = self.heap
        bl_count = self.bl_count
        for bits in range(MAX_BITS + 1):
            bl_count[bits] = 0
        overflow = 0
        opt_len = self.opt_len
        static_len = self.static_len

        length[heap[heap_max]] = 0  # root of the heap
        for h in range(heap_max + 1, HEAP_SIZE):
            n = heap[h]
            bits = length[dad[n]] + 1
            if bits > max_length:
                bits = max_length
                overflow += 1
            length[n] = bits
            if n > max_code:
                continue  # not a leaf node
            bl_count[bits] += 1
            xbits = extra[n - base] if n >= base else 0
            f = freq[n]
            opt_len += f * (bits + xbits)
            if stree is not None:
                static_len += f * (stree[n] + xbits)
        self.static_len = static_len
        if overflow == 0:
            self.opt_len = opt_len
            return
        # find the first bit length which could increase
        while True:
            bits = max_length - 1
            while bl_count[bits] == 0:
                bits -= 1
            bl_count[bits] -= 1
            bl_count[bits + 1] += 2
            bl_count[max_length] -= 1
            overflow -= 2
            if overflow <= 0:
                break
        h = HEAP_SIZE
        for bits in range(max_length, 0, -1):
            n = bl_count[bits]
            while n != 0:
                h -= 1
                m = heap[h]
                if m > max_code:
                    continue
                if length[m] != bits:
                    opt_len += (bits - length[m]) * freq[m]
                    length[m] = bits
                n -= 1
        self.opt_len = opt_len

    def build_tree(self, desc):
        freq, length, dad = desc.freq, desc.length, desc.dad
        stree, _extra, _base, elems, _max_length = desc.stat_desc
        heap = self.heap
        depth = self.depth
        heap_len = 0
        heap_max = HEAP_SIZE
        max_code = -1

        for n in range(elems):
            if freq[n] != 0:
                heap_len += 1
                heap[heap_len] = max_code = n
                depth[n] = 0
            else:
                length[n] = 0

        # force at least two codes of non-zero frequency
        while heap_len < 2:
            if max_code < 2:
                max_code += 1
                node = max_code
            else:
                node = 0
            heap_len += 1
            heap[heap_len] = node
            freq[node] = 1
            depth[node] = 0
            self.opt_len -= 1
            if stree is not None:
                self.static_len -= stree[node]
        desc.max_code = max_code

        for n in range(heap_len // 2, 0, -1):
            self.pqdownheap(freq, n, heap_len)

        node = elems
        while True:
            # pqremove
            n = heap[1]
            heap[1] = heap[heap_len]
            heap_len -= 1
            self.pqdownheap(freq, 1, heap_len)
            m = heap[1]

            heap_max -= 1
            heap[heap_max] = n
            heap_max -= 1
            heap[heap_max] = m

            freq[node] = (freq[n] + freq[m]) & 0xFFFF
            dn, dm = depth[n], depth[m]
            depth[node] = ((dn if dn >= dm else dm) + 1) & 0xFF
            dad[n] = dad[m] = node
            heap[1] = node
            node += 1
            self.pqdownheap(freq, 1, heap_len)
            if heap_len < 2:
                break

        heap_max -= 1
        heap[heap_max] = heap[1]

        self.gen_bitlen(desc, heap_max)
        _gen_codes(length, desc.code, max_code, self.bl_count)

    def scan_tree(self, desc, max_code):
        length = desc.length
        bl_freq = self.bl_tree.freq
        prevlen = -1
        nextlen = length[0]
        count = 0
        max_count, min_count = 7, 4
        if nextlen == 0:
            max_count, min_count = 138, 3
        length[max_code + 1] = 0xFFFF  # guard
        for n in range(max_code + 1):
            curlen = nextlen
            nextlen = length[n + 1]
            count += 1
            if count < max_count and curlen == nextlen:
                continue
            elif count < min_count:
                bl_freq[curlen] += count
            elif curlen != 0:
                if curlen != prevlen:
                    bl_freq[curlen] += 1
                bl_freq[REP_3_6] += 1
            elif count <= 10:
                bl_freq[REPZ_3_10] += 1
            else:
                bl_freq[REPZ_11_138] += 1
            count = 0
            prevlen = curlen
            if nextlen == 0:
                max_count, min_count = 138, 3
            elif curlen == nextlen:
                max_count, min_count = 6, 3
            else:
                max_count, min_count = 7, 4

    def send_tree(self, desc, max_code):
        length = desc.length
        bl_code, bl_len = self.bl_tree.code, self.bl_tree.length
        send_bits = self.send_bits
        prevlen = -1
        nextlen = length[0]
        count = 0
        max_count, min_count = 7, 4
        # tree[max_code + 1].Len = -1;  guard already set
        if nextlen == 0:
            max_count, min_count = 138, 3
        for n in range(max_code + 1):
            curlen = nextlen
            nextlen = length[n + 1]
            count += 1
            if count < max_count and curlen == nextlen:
                continue
            elif count < min_count:
                while True:
                    send_bits(bl_code[curlen], bl_len[curlen])
                    count -= 1
                    if count == 0:
                        break
            elif curlen != 0:
                if curlen != prevlen:
                    send_bits(bl_code[curlen], bl_len[curlen])
                    count -= 1
                send_bits(bl_code[REP_3_6], bl_len[REP_3_6])
                send_bits(count - 3, 2)
            elif count <= 10:
                send_bits(bl_code[REPZ_3_10], bl_len[REPZ_3_10])
                send_bits(count - 3, 3)
            else:
                send_bits(bl_code[REPZ_11_138], bl_len[REPZ_11_138])
                send_bits(count - 11, 7)
            count = 0
            prevlen = curlen
            if nextlen == 0:
                max_count, min_count = 138, 3
            elif curlen == nextlen:
                max_count, min_count = 6, 3
            else:
                max_count, min_count = 7, 4

    def build_bl_tree(self):
        self.scan_tree(self.dyn_ltree, self.dyn_ltree.max_code)
        self.scan_tree(self.dyn_dtree, self.dyn_dtree.max_code)
        self.build_tree(self.bl_tree)
        bl_len = self.bl_tree.length
        max_blindex = BL_CODES - 1
        while max_blindex >= 3 and bl_len[BL_ORDER[max_blindex]] == 0:
            max_blindex -= 1
        self.opt_len += 3 * (max_blindex + 1) + 5 + 5 + 4
        return max_blindex

    def send_all_trees(self, lcodes, dcodes, blcodes):
        send_bits = self.send_bits
        send_bits(lcodes - 257, 5)
        send_bits(dcodes - 1, 5)
        send_bits(blcodes - 4, 4)
        bl_len = self.bl_tree.length
        for rank in range(blcodes):
            send_bits(bl_len[BL_ORDER[rank]], 3)
        self.send_tree(self.dyn_ltree, lcodes - 1)
        self.send_tree(self.dyn_dtree, dcodes - 1)

    def compress_block(self, lcode, llen, dcode, dlen):
        # per-block tables: literal/length symbol + length extra bits for
        # each lc, distance code bits for each distance code
        lc_val = [0] * 256
        lc_n = [0] * 256
        for lc in range(256):
            c = LENGTH_CODE[lc]
            sym = c + LITERALS + 1
            ln = llen[sym]
            extra = EXTRA_LBITS[c]
            lc_val[lc] = lcode[sym] | (((lc - BASE_LENGTH[c]) << ln) if extra else 0)
            lc_n[lc] = ln + extra
        base_dist = BASE_DIST
        extra_dbits = EXTRA_DBITS
        dcode_of = DCODE_OF_DIST
        out = self.out
        acc = self.bi_buf
        nb = self.bi_valid
        for dist, lc in zip(self.sym_dist, self.sym_lc):
            if dist == 0:
                acc |= lcode[lc] << nb
                nb += llen[lc]
            else:
                acc |= lc_val[lc] << nb
                nb += lc_n[lc]
                c = dcode_of[dist]
                acc |= (dcode[c] | ((dist - 1 - base_dist[c]) << dlen[c])) << nb
                nb += dlen[c] + extra_dbits[c]
            if nb >= 64:
                out += (acc & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "little")
                acc >>= 64
                nb -= 64
        self.bi_buf = acc
        self.bi_valid = nb
        self.send_bits(lcode[END_BLOCK], llen[END_BLOCK])

    def tr_stored_block(self, buf, stored_len, last):
        self.send_bits((STORED_BLOCK << 1) + last, 3)
        self.bi_windup()
        self.out += struct.pack("<HH", stored_len & 0xFFFF, ~stored_len & 0xFFFF)
        self.out += buf

    def tr_flush_block(self, buf, stored_len, last):
        """_tr_flush_block(); buf is None when the block left the window."""
        # level > 0
        self.build_tree(self.dyn_ltree)
        self.build_tree(self.dyn_dtree)
        max_blindex = self.build_bl_tree()

        opt_lenb = (self.opt_len + 3 + 7) >> 3
        static_lenb = (self.static_len + 3 + 7) >> 3
        if static_lenb <= opt_lenb:  # or strategy == Z_FIXED
            opt_lenb = static_lenb

        if stored_len + 4 <= opt_lenb and buf is not None:
            self.tr_stored_block(buf, stored_len, last)
        elif static_lenb == opt_lenb:
            self.send_bits((STATIC_TREES << 1) + last, 3)
            self.compress_block(STATIC_LCODE, STATIC_LLEN, STATIC_DCODE, STATIC_DLEN)
        else:
            self.send_bits((DYN_TREES << 1) + last, 3)
            self.send_all_trees(self.dyn_ltree.max_code + 1, self.dyn_dtree.max_code + 1, max_blindex + 1)
            lt, dt = self.dyn_ltree, self.dyn_dtree
            self.compress_block(lt.code, lt.length, dt.code, dt.length)
        self.init_block()
        if last:
            self.bi_windup()


def _adler32_py(data):
    a, b = 1, 0
    for i in range(0, len(data), 5552):
        for c in data[i : i + 5552]:
            a += c
            b += a
        a %= 65521
        b %= 65521
    return (b << 16) | a


def _hashes(d, m):
    """ins_h of the string at each position 0..m-1 (UPDATE_HASH of 3 bytes).

    h = ((d[p] << 10) ^ (d[p+1] << 5) ^ d[p+2]) & HASH_MASK, computed for all
    positions at once on a big integer with one 16-bit lane per position.
    """
    out = array("H")
    if m <= 0:
        return out
    wide = bytearray(2 * (m + 2))
    wide[0::2] = d[: m + 2]
    lanes = int.from_bytes(wide, "little")
    low5 = int.from_bytes(b"\x1f\x00" * (m + 2), "little")  # d & 31: the bits that survive << 10
    h = ((lanes & low5) << (2 * HASH_SHIFT)) ^ ((lanes >> 16) << HASH_SHIFT) ^ (lanes >> 32)
    h &= (1 << (16 * m)) - 1
    out.frombytes(h.to_bytes(2 * m, "little"))
    if sys.byteorder != "little":
        out.byteswap()
    return out


def _deflate_slow(st, data):
    """deflate_slow() for the whole input with flush == Z_FINISH.

    Hash chains: in zlib, head[h] -> prev[] -> ... lists every position
    inserted so far with hash h, most recent first. Here occ[h] lists them in
    insertion (= increasing) order, so a chain is a slice of occ[h] read
    backwards and needs no pointer chasing. Positions are absolute: an entry
    <= `slid` is one zlib has set to NIL when sliding the hash table
    (position 0 is NIL from the start), and since such entries are also
    <= `limit` the chain walk stops before them as zlib does. zlib reuses
    prev[p & w_mask] for p + W_SIZE, but only after p fell behind `limit`.
    """
    good_match, max_lazy_match, nice_length, max_chain_length, _func = CONFIGURATION_TABLE[LEVEL]
    n = len(data)
    d = bytes(data) + bytes(MAX_MATCH + MIN_MATCH)
    m = n - (MIN_MATCH - 1) if n >= MIN_MATCH else 0  # positions that get inserted
    H = _hashes(d, m).tolist()
    # occ[h]: the positions inserted with hash h (a dict for small inputs,
    # where allocating HASH_SIZE lists would dominate)
    occ = [[] for _ in range(HASH_SIZE)] if n > 20000 else defaultdict(list)
    lfreq = st.dyn_ltree.freq
    dfreq = st.dyn_dtree.freq
    ap_dist = st.sym_dist.append
    ap_lc = st.sym_lc.append
    dcode_of = DCODE_OF_DIST
    lsym_of = LSYM_OF_LC
    from_bytes = int.from_bytes
    flush = st.tr_flush_block

    slid = 0  # absolute offset of window[0]
    wend = 0  # absolute end of the data read into the window (strstart + lookahead)
    strstart = 0
    block_start = 0
    match_length = prev_length = MIN_MATCH - 1
    match_start = prev_match = 0
    match_available = False
    nsym = 0
    lookahead = 0

    while True:
        if wend - strstart < MIN_LOOKAHEAD:
            # fill_window()
            while True:
                more = WINDOW_SIZE - (wend - slid)
                if strstart - slid >= W_SIZE + MAX_DIST:
                    # slide the window (and the hash table) down by W_SIZE
                    slid += W_SIZE
                    more += W_SIZE
                if wend == n:
                    break
                nread = n - wend  # read_buf()
                if nread > more:
                    nread = more
                wend += nread
                if wend - strstart >= MIN_LOOKAHEAD or wend == n:
                    break
            if wend == strstart:
                break
        lookahead = wend - strstart

        # INSERT_STRING(s, s->strstart, hash_head)
        hash_head = 0  # NIL
        if lookahead >= MIN_MATCH:
            chain = occ[H[strstart]]
            k = len(chain)
            if k:
                hash_head = chain[-1]
            chain.append(strstart)

        prev_length = match_length
        prev_match = match_start
        match_length = MIN_MATCH - 1

        if hash_head > slid and prev_length < max_lazy_match and strstart - hash_head <= MAX_DIST:
            # longest_match(s, hash_head)
            chain_length = max_chain_length
            if prev_length >= good_match:
                chain_length >>= 2
            nice_match = nice_length if nice_length <= lookahead else lookahead
            best_len = prev_length
            maxlen = MAX_MATCH if lookahead >= MAX_MATCH else lookahead
            # (best_len >= maxlen only at the end of the input, when
            # prev_length > lookahead: nothing can be longer then, and
            # deflate_slow emits the previous match whatever this returns)
            if best_len < maxlen:
                # A candidate becomes the best match when its match is longer
                # than best_len. zlib tests scan_end, scan_end1 and the first
                # two bytes before comparing from the fourth byte on (the
                # third is equal when the first two are, as the hashes are);
                # comparing the first best_len + 1 bytes and then the rest
                # gives the same lengths.
                limit = strstart - MAX_DIST
                if limit < slid:
                    limit = slid
                scan = d[strstart : strstart + maxlen]
                plen = best_len + 1
                pat = scan[:plen]
                scan_end = scan[best_len]
                # the candidates: hash_head = chain[k-1], chain[k-2], ..., at
                # most chain_length of them, and after the first only those
                # > limit (while ((cur_match = prev[...]) > limit &&
                # --chain_length != 0)); hash_head itself may be at distance
                # MAX_DIST, i.e. == limit
                lo = k - chain_length
                if lo < 0:
                    lo = 0
                if chain[lo] <= limit:
                    lo = bisect_right(chain, limit, lo, k)
                    if lo == k:
                        lo = k - 1
                for cur_match in chain[k - 1 : lo - 1 : -1] if lo else chain[k - 1 :: -1]:
                    if d[cur_match + best_len] == scan_end and d[cur_match : cur_match + plen] == pat:
                        if plen < maxlen:
                            mb = d[cur_match + plen : cur_match + maxlen]
                            sb = scan[plen:]
                            if mb == sb:
                                ln = maxlen
                            else:
                                x = from_bytes(mb, "little") ^ from_bytes(sb, "little")
                                ln = plen + (((x & -x).bit_length() - 1) >> 3)
                        else:
                            ln = plen
                        match_start = cur_match
                        best_len = ln
                        if ln >= nice_match or ln >= maxlen:
                            break
                        plen = ln + 1
                        pat = scan[:plen]
                        scan_end = scan[ln]
            match_length = best_len if best_len <= lookahead else lookahead

            if match_length == MIN_MATCH and strstart - match_start > TOO_FAR:
                # match_length <= 5 && (strategy == Z_FILTERED || ...)
                match_length = MIN_MATCH - 1

        if prev_length >= MIN_MATCH and match_length <= prev_length:
            # emit the previous match and insert the strings inside it up to
            # max_insert (= wend - MIN_MATCH; it only limits the insertions
            # at the end of the input, where it is m - 1)
            dist = strstart - 1 - prev_match
            lc = prev_length - MIN_MATCH
            ap_dist(dist)
            ap_lc(lc)
            lfreq[lsym_of[lc]] += 1
            dfreq[dcode_of[dist]] += 1
            nsym += 1
            stop = strstart + prev_length - 1
            if stop > m:
                stop = m
            for p in range(strstart + 1, stop):
                occ[H[p]].append(p)
            strstart += prev_length - 1
            match_available = False
            match_length = MIN_MATCH - 1
            if nsym == SYM_END:
                flush(d[block_start:strstart] if block_start >= slid else None, strstart - block_start, 0)
                block_start = strstart
                nsym = 0
        elif match_available:
            c = d[strstart - 1]
            ap_dist(0)
            ap_lc(c)
            lfreq[c] += 1
            nsym += 1
            if nsym == SYM_END:
                flush(d[block_start:strstart] if block_start >= slid else None, strstart - block_start, 0)
                block_start = strstart
                nsym = 0
            strstart += 1
        else:
            match_available = True
            strstart += 1

    if match_available:
        c = d[strstart - 1]
        ap_dist(0)
        ap_lc(c)
        lfreq[c] += 1
        nsym += 1
    flush(d[block_start:strstart] if block_start >= slid else None, strstart - block_start, 1)


def compress(data) -> bytes:
    """zlib.compress(data, 6) / compress2(..., Z_DEFAULT_COMPRESSION) of classic zlib."""
    data = bytes(data)
    st = _State()
    # zlib header: CM 8, CINFO 7, FLEVEL 2 (level 6), no dictionary
    header = (Z_DEFLATED + ((W_BITS - 8) << 4)) << 8
    header |= 2 << 6
    header += 31 - (header % 31)
    _deflate_slow(st, data)
    adler = _adler32(data) if _adler32 is not None else _adler32_py(data)
    return header.to_bytes(2, "big") + bytes(st.out) + (adler & 0xFFFFFFFF).to_bytes(4, "big")
