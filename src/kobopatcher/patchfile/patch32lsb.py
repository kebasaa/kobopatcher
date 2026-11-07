"""The patch32lsb patch format (the older text format, *.patch).

A patch file is a sequence of blocks, one per patch:

    <Patch>
    patch_name = `My patch`
    patch_enable = `no`
    # comments start with a hash
    patch_group = `Optional group`
    replace_bytes = 1234AB, 00 20, 01 20
    find_base_address = `some string`
    replace_string = 10, `find`, `replace`
    </Patch>

Instructions are ``key = value`` (at least one space on each side of the
equals sign). Strings are wrapped in backticks and may contain the escapes
\\n \\r \\t \\v \\" \\' \\` \\0 \\\\ and \\xNN. Numbers are parsed like Go's
fmt.Sscanf (offsets are hexadecimal).

Differences from kobopatch (go_compat mode reverts 1, 2 and 4, see
kobopatcher.compat):

1. Negative offsets. kobopatch parsed every offset with Sscanf("%x") into an
   int32, which rejects 8-digit values like FFFFFFF0 ("integer overflow").
   For replace_string and replace_zlib it then replaced the first "FFFFFF" of
   an 8-character offset with "-", so FFFFFFF0 became -0xF0 = -240 instead of
   -16, FFFFFF00 became -0 = 0, 0FFFFFF0 became "0-0" = 0 and 7FFFFFFF
   became "7-" = 7 (Sscanf ignores what follows the number). kobopatcher
   drops that hack and reads an unsigned hexadecimal offset from 80000000 to
   FFFFFFFF as a 32-bit two's complement value (FFFFFFF0 = -16), for every
   instruction which takes an offset (replace_bytes, base_address,
   replace_float, replace_int, replace_string and replace_zlib). Offsets with
   an explicit sign are unchanged.
2. patch_name is reset at every <Patch>. kobopatch kept the name of the
   previous patch, so a block without a patch_name silently reused it (and
   failed with a confusing "duplicate patch" error, or picked up a name given
   between two blocks).
3. Patches are applied (and validated) in file order. kobopatch iterated over
   a Go map, i.e. in random order. (This is also done in go_compat mode, since
   a random order cannot be reproduced; the parity harness compares sets.)
4. Situations where kobopatch crashed with a Go panic (a lone backtick as a
   string, a backslash at the end of a string, a truncated \\x escape) raise
   GoPanic in go_compat mode and a normal error otherwise. (When looking for
   the first string of a list, such a prefix is simply not a valid string.)

_fixed(n) tells whether fix n is in effect.
"""

import math
import re
import struct

from .. import patchfile as _pf
from ..compat import go_compat
from ..errors import GoPanic, KobopatcherError
from ..gofmt import quote
from . import out

__all__ = ["Patch32lsbError", "Instruction", "PatchSet", "parse", "unescape", "unescape_first"]


class Patch32lsbError(KobopatcherError):
    pass


def _log(msg: str) -> None:
    _pf.log(msg)


def _s(b: bytes) -> str:
    """A Go string (bytes) as a Python str."""
    return bytes(b).decode("utf-8", "surrogateescape")


def _b(s: str) -> bytes:
    return s.encode("utf-8", "surrogateescape")


def _fixed(n: int) -> bool:
    """Whether fix n of the module docstring is in effect (not in go_compat
    mode). The parity harness replaces this to revert single fixes."""
    return not go_compat()


def _tf(v: bool) -> str:
    return "true" if v else "false"


# --- Go runes -----------------------------------------------------------------

_RUNE_ERROR = 0xFFFD


def _decode_rune(b: bytes, i: int):
    """utf8.DecodeRune(b[i:]): (rune, width); invalid input is (U+FFFD, 1)."""
    c = b[i]
    if c < 0x80:
        return c, 1
    if 0xC2 <= c <= 0xDF:
        n = 2
    elif 0xE0 <= c <= 0xEF:
        n = 3
    elif 0xF0 <= c <= 0xF4:
        n = 4
    else:
        return _RUNE_ERROR, 1
    try:
        ch = b[i : i + n].decode("utf-8")
    except UnicodeDecodeError:
        return _RUNE_ERROR, 1
    return ord(ch), n


def _runes(b: bytes):
    out_ = []
    i = 0
    while i < len(b):
        r, w = _decode_rune(b, i)
        out_.append((r, w))
        i += w
    return out_


# unicode.White_Space (unicode.IsSpace and fmt's isSpace)
_SPACE = ((0x09, 0x0D), (0x20, 0x20), (0x85, 0x85), (0xA0, 0xA0), (0x1680, 0x1680), (0x2000, 0x200A), (0x2028, 0x2029), (0x202F, 0x202F), (0x205F, 0x205F), (0x3000, 0x3000))


def _is_space(r: int) -> bool:
    for lo, hi in _SPACE:
        if r < lo:
            return False
        if r <= hi:
            return True
    return False


def _trim_space(b: bytes) -> bytes:
    """strings.TrimSpace."""
    rs = _runes(b)
    start = 0
    pos = 0
    for r, w in rs:
        if not _is_space(r):
            break
        start += 1
        pos += w
    end = len(rs)
    epos = len(b)
    while end > start and _is_space(rs[end - 1][0]):
        end -= 1
        epos -= rs[end][1]
    return b[pos:epos]


def _lower_key(b: bytes) -> str:
    """strings.ToLower, only as far as needed to compare with ASCII keywords.

    The only non-ASCII runes Go lowercases to ASCII are U+0130 (to i) and the
    Kelvin sign U+212A (to k); everything else stays non-ASCII.
    """
    res = []
    for r, _ in _runes(b):
        if r < 0x80:
            res.append(chr(r).lower())
        elif r == 0x130:
            res.append("i")
        elif r == 0x212A:
            res.append("k")
        else:
            res.append("�")
    return "".join(res)


# --- fmt.Sscanf ------------------------------------------------------------------
#
# A port of the parts of Go's fmt scanning used by the format: the verbs %x
# (into an int32 or a []byte), %d (into a uint8) and %f (into a float64), and
# literal characters. Error texts are Go's.


class _ScanError(Exception):
    pass


_EOF = -1
_HEX = "0123456789aAbBcCdDeEfF"
_DEC = "0123456789"
_SIGN = "+-"


class _Scanner:
    def __init__(self, b: bytes, twos: bool = False):
        self.r = [r for r, _ in _runes(b)]
        self.i = 0
        self.buf = bytearray()
        self.twos = twos

    def get(self) -> int:
        if self.i >= len(self.r):
            return _EOF
        c = self.r[self.i]
        self.i += 1
        return c

    def unread(self) -> None:
        self.i -= 1

    def must_read(self) -> int:
        c = self.get()
        if c == _EOF:
            raise _ScanError("unexpected EOF")
        return c

    def not_eof(self) -> None:
        if self.get() == _EOF:
            raise _ScanError("EOF")  # panic(io.EOF), caught by errorHandler
        self.unread()

    def accept(self, ok: str) -> bool:
        c = self.get()
        if c == _EOF:
            return False
        if c < 0x80 and chr(c) in ok:
            self.buf.append(c)
            return True
        self.unread()
        return False

    def peek_nl(self) -> bool:
        c = self.get()
        if c != _EOF:
            self.unread()
        return c == 0x0A

    def skip_space(self) -> None:
        while True:
            c = self.get()
            if c == _EOF:
                return
            if c == 0x0D and self.peek_nl():
                continue
            if c == 0x0A:
                raise _ScanError("unexpected newline")
            if not _is_space(c):
                self.unread()
                return

    def advance(self, fmt: str) -> int:
        # (the formats used here contain no spaces and no %%)
        i = 0
        while i < len(fmt):
            fc = fmt[i]
            if fc == "%":
                if i + 1 == len(fmt):
                    raise _ScanError("missing verb: % at end of format string")
                return i
            c = self.must_read()
            if ord(fc) != c:
                self.unread()
                return -1
            i += 1
        return i

    def scan_number(self, digits: str) -> str:
        self.not_eof()
        if not self.accept(digits):
            raise _ScanError("expected integer")
        while self.accept(digits):
            pass
        return self.buf.decode("ascii")

    def scan_int32(self) -> int:
        self.skip_space()
        self.not_eof()
        self.accept(_SIGN)
        tok = self.scan_number(_HEX)
        i = int(tok, 16)
        if not -(1 << 63) <= i < (1 << 63):
            raise _ScanError("strconv.ParseInt: parsing %s: value out of range" % quote(tok))
        if not -(1 << 31) <= i < (1 << 31):
            if self.twos and tok[0] not in _SIGN and i < (1 << 32):
                return i - (1 << 32)
            raise _ScanError("integer overflow on token " + tok)
        return i

    def scan_uint8(self) -> int:
        self.skip_space()
        self.not_eof()
        tok = self.scan_number(_DEC)
        i = int(tok, 10)
        if i >= 1 << 64:
            raise _ScanError("strconv.ParseUint: parsing %s: value out of range" % quote(tok))
        if i > 0xFF:
            raise _ScanError("unsigned integer overflow on token " + tok)
        return i

    def hex_byte(self):
        c1 = self.get()
        if c1 == _EOF:
            return None
        v1 = _hexval(c1)
        if v1 is None:
            self.unread()
            return None
        v2 = _hexval(self.must_read())
        if v2 is None:
            raise _ScanError("illegal hex digit")
        return v1 << 4 | v2

    def scan_bytes(self) -> bytes:
        self.skip_space()
        self.not_eof()
        self.not_eof()
        while True:
            b = self.hex_byte()
            if b is None:
                break
            self.buf.append(b)
        if not self.buf:
            raise _ScanError("no hex data for %x string")
        return bytes(self.buf)

    def float_token(self) -> str:
        self.buf = bytearray()
        if self.accept("nN") and self.accept("aA") and self.accept("nN"):
            return self.buf.decode("ascii")
        self.accept(_SIGN)
        if self.accept("iI") and self.accept("nN") and self.accept("fF"):
            return self.buf.decode("ascii")
        digits = _DEC + "_"
        exp = "eEpP"
        if self.accept("0") and self.accept("xX"):
            digits = _HEX + "_"
            exp = "pP"
        while self.accept(digits):
            pass
        if self.accept("."):
            while self.accept(digits):
                pass
        if self.accept(exp):
            self.accept(_SIGN)
            while self.accept(_DEC + "_"):
                pass
        return self.buf.decode("ascii")

    def scan_float64(self) -> float:
        self.skip_space()
        self.not_eof()
        return _convert_float(self.float_token())

    def scan_one(self, verb: str, kind: str):
        self.buf = bytearray()
        if kind == "int32":
            return self.scan_int32()
        if kind == "uint8":
            return self.scan_uint8()
        if kind == "float64":
            return self.scan_float64()
        if kind == "bytes":
            return self.scan_bytes()
        raise AssertionError(kind)


def _hexval(c: int):
    if 0x30 <= c <= 0x39:
        return c - 0x30
    if 0x61 <= c <= 0x66:
        return c - 0x61 + 10
    if 0x41 <= c <= 0x46:
        return c - 0x41 + 10
    return None


def _sscanf(inp: bytes, fmt: str, kinds, twos: bool = False):
    """fmt.Sscanf(inp, fmt, ...): returns the values, raises _ScanError.

    With twos, an unsigned %x int32 from 80000000 to FFFFFFFF is read as a
    two's complement value instead of failing (kobopatcher fix 1).
    """
    s = _Scanner(bytes(inp), twos)
    vals = []
    i = 0
    end = len(fmt) - 1
    while i <= end:
        w = s.advance(fmt[i:])
        if w > 0:
            i += w
            continue
        if fmt[i] != "%":
            if w < 0:
                raise _ScanError("input does not match format")
            break
        i += 1
        c = fmt[i]
        i += 1
        if c != "c":
            s.skip_space()
        if len(vals) >= len(kinds):
            raise _ScanError("too few operands for format '%" + fmt[i - 1 :] + "'")
        vals.append(s.scan_one(c, kinds[len(vals)]))
    if len(vals) < len(kinds):
        raise _ScanError("too many operands")
    return vals


# --- strconv.ParseFloat / Atoi ------------------------------------------------------

_NAN = struct.unpack("<d", struct.pack("<Q", 0x7FF8000000000001))[0]  # Go's math.NaN()


def _lower(c: str) -> str:
    return chr(ord(c) | 0x20)


def _underscore_ok(s: str) -> bool:
    saw = "^"
    i = 0
    if s and s[0] in "+-":
        s = s[1:]
    hexm = False
    if len(s) >= 2 and s[0] == "0" and _lower(s[1]) in "box":
        i = 2
        saw = "0"
        hexm = _lower(s[1]) == "x"
    while i < len(s):
        c = s[i]
        if "0" <= c <= "9" or (hexm and "a" <= _lower(c) <= "f"):
            saw = "0"
        elif c == "_":
            if saw != "0":
                return False
            saw = "_"
        else:
            if saw == "_":
                return False
            saw = "!"
        i += 1
    return saw != "_"


def _special(s: str):
    """strconv's special(): (value, n) or None."""
    if not s:
        return None
    sign = 1.0
    t = s
    nsign = 0
    if t[0] in "+-":
        if t[0] == "-":
            sign = -1.0
        nsign = 1
        t = t[1:]
        lead = "i"
    else:
        lead = t[0]
    if lead in "iI":
        n = 0
        for a, b in zip(t, "infinity"):
            if a.lower() != b:
                break
            n += 1
        if 3 < n < 8:
            n = 3
        if n in (3, 8):
            return math.copysign(math.inf, sign), nsign + n
    elif lead in "nN":
        if t[:3].lower() == "nan":
            return _NAN, 3
    return None


def _read_float(s: str):
    """The syntax part of strconv's readFloat: (ok, n, hex)."""
    n = len(s)
    i = 0
    underscores = False
    if i >= n:
        return False, i, False
    if s[i] in "+-":
        i += 1
    base = 10
    expchar = "e"
    hexm = False
    if i + 2 < n and s[i] == "0" and _lower(s[i + 1]) == "x":
        base = 16
        i += 2
        expchar = "p"
        hexm = True
    sawdot = sawdigits = False
    while i < n:
        c = s[i]
        if c == "_":
            underscores = True
        elif c == ".":
            if sawdot:
                break
            sawdot = True
        elif "0" <= c <= "9":
            sawdigits = True
        elif base == 16 and "a" <= _lower(c) <= "f":
            sawdigits = True
        else:
            break
        i += 1
    if not sawdigits:
        return False, i, hexm
    if i < n and _lower(s[i]) == expchar:
        i += 1
        if i >= n:
            return False, i, hexm
        if s[i] in "+-":
            i += 1
        if i >= n or not "0" <= s[i] <= "9":
            return False, i, hexm
        while i < n and ("0" <= s[i] <= "9" or s[i] == "_"):
            if s[i] == "_":
                underscores = True
            i += 1
    elif base == 16:
        return False, i, hexm
    if underscores and not _underscore_ok(s[:i]):
        return False, i, hexm
    return True, i, hexm


def _parse_float(s: str):
    """strconv.ParseFloat(s, 64): (value, error text or None)."""
    sp = _special(s)
    if sp is not None:
        if sp[1] != len(s):
            return 0.0, "invalid syntax"
        return sp[0], None
    ok, n, hexm = _read_float(s)
    if not ok or n != len(s):
        return 0.0, "invalid syntax"
    clean = s.replace("_", "")
    if hexm:
        try:
            f = float.fromhex(clean)
        except OverflowError:
            return math.copysign(math.inf, -1.0 if clean[0] == "-" else 1.0), "value out of range"
    else:
        f = float(clean)
    if math.isinf(f):
        return f, "value out of range"
    return f, None


def _atoi(s: str):
    """strconv.Atoi (64-bit int): (value, error text or None)."""
    if 0 < len(s) < 19:
        t = s
        if t[0] in "+-":
            t = t[1:]
            if not t:
                return 0, "invalid syntax"
        if not all("0" <= c <= "9" for c in t):
            return 0, "invalid syntax"
        v = int(t)
        return (-v if s[0] == "-" else v), None
    if s == "":
        return 0, "invalid syntax"
    t = s
    neg = False
    if t[0] in "+-":
        neg = t[0] == "-"
        t = t[1:]
    if t == "":
        return 0, "invalid syntax"
    un = 0
    rng = False
    for c in t:
        if not "0" <= c <= "9":
            return 0, "invalid syntax"
        if un >= (1 << 64) // 10 + 1:
            rng = True
            break
        un = un * 10 + ord(c) - 0x30
        if un >= 1 << 64:
            rng = True
            break
    if rng:
        return 0, "value out of range"
    if not neg and un >= 1 << 63:
        return 0, "value out of range"
    if neg and un > 1 << 63:
        return 0, "value out of range"
    return (-un if neg else un), None


def _wrap64(v: int) -> int:
    v &= (1 << 64) - 1
    return v - (1 << 64) if v >= 1 << 63 else v


def _ldexp(frac: float, exp: int) -> float:
    """math.Ldexp (with Go's int64 arithmetic)."""
    if frac == 0 or math.isinf(frac) or math.isnan(frac):
        return frac
    e = 0
    if abs(frac) < 2.2250738585072014e-308:
        frac *= float(1 << 52)
        e = -52
    exp = _wrap64(exp + e)
    x = struct.unpack("<Q", struct.pack("<d", frac))[0]
    exp = _wrap64(exp + ((x >> 52) & 0x7FF) - 1023)
    if exp < -1075:
        return math.copysign(0.0, frac)
    if exp > 1023:
        return -math.inf if frac < 0 else math.inf
    m = 1.0
    if exp < -1022:
        exp += 53
        m = 1.0 / (1 << 53)
    x &= ~(0x7FF << 52) & ((1 << 64) - 1)
    x |= (exp + 1023) << 52
    return m * struct.unpack("<d", struct.pack("<Q", x))[0]


def _convert_float(tok: str) -> float:
    p = tok.find("p")
    if p >= 0 and "x" not in tok and "X" not in tok:
        f, err = _parse_float(tok[:p])
        if err:
            raise _ScanError("strconv.ParseFloat: parsing %s: %s" % (quote(tok), err))
        m, err = _atoi(tok[p + 1 :])
        if err:
            raise _ScanError("strconv.Atoi: parsing %s: %s" % (quote(tok), err))
        return _ldexp(f, m)
    f, err = _parse_float(tok)
    if err:
        raise _ScanError("strconv.ParseFloat: parsing %s: %s" % (quote(tok), err))
    return f


# --- strings ----------------------------------------------------------------------


class _UnescapeError(Exception):
    pass


def _panic(go_msg: str, msg: str):
    """kobopatch panicked here (fix 4)."""
    if not _fixed(4):
        raise GoPanic(go_msg)
    raise _UnescapeError(msg)


_ESCAPES = {
    ord("n"): b"\n",
    ord("r"): b"\r",
    ord("t"): b"\t",
    ord("v"): b"\v",
    ord('"'): b'"',
    ord("'"): b"'",
    ord("`"): b"`",
    ord("0"): b"\x00",
    ord("\\"): b"\\",
}


def _unescape(s: bytes) -> bytes:
    if not (s.startswith(b"`") and s.endswith(b"`")):
        raise _UnescapeError("string not wrapped in backticks")
    if len(s) == 1:
        _panic("runtime error: index out of range [-1]", "string not wrapped in backticks")
    if s[-2] == 0x5C and s[-3] != 0x5C:
        raise _UnescapeError("string not wrapped in backticks")
    t = s[1:-1]
    res = bytearray()
    i = 0
    n = len(t)
    while i < n:
        c = t[i]
        if c != 0x5C:
            res.append(c)
            i += 1
            continue
        if i + 1 >= n:
            _panic("runtime error: index out of range [1] with length 1", "incomplete escape sequence at end of string")
        e = t[i + 1]
        if e in _ESCAPES:
            res += _ESCAPES[e]
            i += 2
        elif e == ord("x"):
            if n - i < 4:
                _panic("runtime error: slice bounds out of range [:4] with length %d" % (n - i), "incomplete \\x escape sequence")
            try:
                res += _sscanf(t[i + 2 : i + 4], "%x", ["bytes"])[0]
            except _ScanError as err:
                raise _UnescapeError(str(err)) from None
            i += 4
        else:
            raise _UnescapeError("unknown escape " + chr(e))
    return bytes(res)


def _unescape_first(s: bytes):
    for i in range(2, len(s) + 1):
        try:
            return _unescape(s[:i]), s[i:]
        except _UnescapeError:
            continue
    raise _UnescapeError("could not find valid string")


def unescape(s: str) -> str:
    """Unescape a backtick-wrapped string (raises Patch32lsbError)."""
    try:
        return _s(_unescape(_b(s)))
    except _UnescapeError as e:
        raise Patch32lsbError(str(e)) from None


def unescape_first(s: str):
    """Unescape the first backtick-wrapped string: (string, rest)."""
    try:
        a, b = _unescape_first(_b(s))
    except _UnescapeError as e:
        raise Patch32lsbError(str(e)) from None
    return _s(a), _s(b)


# --- instructions -------------------------------------------------------------------

KINDS = (
    "Enabled",
    "PatchGroup",
    "BaseAddress",
    "Comment",
    "FindBaseAddress",
    "FindZlib",
    "FindZlibHash",
    "ReplaceBytes",
    "ReplaceFloat",
    "ReplaceInt",
    "ReplaceString",
    "ReplaceZlib",
    "FindReplaceString",
)


class Instruction:
    """One line of a patch. kind is one of KINDS; value is a bool (Enabled),
    a str (PatchGroup, Comment, FindBaseAddress, FindZlib, FindZlibHash), an
    int (BaseAddress), (offset, find, replace) for the Replace* kinds (bytes
    for ReplaceBytes, floats for ReplaceFloat, ints for ReplaceInt, strs
    otherwise) or (find, replace) for FindReplaceString."""

    __slots__ = ("kind", "value")

    def __init__(self, kind: str, value):
        self.kind, self.value = kind, value

    def __repr__(self):
        return "Instruction(%r, %r)" % (self.kind, self.value)

    def __eq__(self, other):
        return isinstance(other, Instruction) and (self.kind, self.value) == (other.kind, other.value)


def _go_bytes(b: bytes) -> str:
    """%#v of a []byte."""
    return "[]byte{%s}" % ", ".join("0x%x" % x for x in b)


def go_float(f: float) -> str:
    """%v (and %#v) of a float64: strconv.FormatFloat(f, 'g', -1, 64)."""
    if f != f:
        return "NaN"
    if math.isinf(f):
        return "+Inf" if f > 0 else "-Inf"
    if f == 0:
        return "-0" if math.copysign(1.0, f) < 0 else "0"
    # repr gives the same shortest round-trip digits as Go
    mant, _, e = repr(abs(f)).partition("e")
    ip, _, fp = mant.partition(".")
    digits = ip + fp
    dp = len(ip) + (int(e) if e else 0)
    lz = len(digits) - len(digits.lstrip("0"))
    digits = digits[lz:].rstrip("0")
    dp -= lz
    nd = len(digits)
    x = dp - 1
    sign = "-" if f < 0 else ""
    if x < -4 or x >= 6:  # the shortest format uses %e from exponent 6 on
        s = digits[0] + ("." + digits[1:] if nd > 1 else "")
        return "%s%se%s%02d" % (sign, s, "+" if x >= 0 else "-", abs(x))
    if dp <= 0:
        return sign + "0." + "0" * (-dp) + digits
    if dp >= nd:
        return sign + digits + "0" * (dp - nd)
    return sign + digits[:dp] + "." + digits[dp:]


def _hexs(s: str) -> str:
    return _b(s).hex()


def _apply_instruction(pt, i: Instruction) -> None:
    k, v = i.kind, i.value
    if k in ("Enabled", "PatchGroup", "Comment"):
        _log("  skipping non-instruction Enabled(), PatchGroup() or Comment()\n")
    elif k == "BaseAddress":
        _log("  BaseAddress(%d)\n" % v)
        pt.base_address(v)
    elif k == "FindBaseAddress":
        _log("  FindBaseAddressString(%s) | hex:%s\n" % (quote(v), _hexs(v)))
        pt.find_base_address_string(v)
    elif k == "ReplaceBytes":
        _log("  ReplaceBytes(%d, %s, %s)\n" % (v[0], _go_bytes(v[1]), _go_bytes(v[2])))
        pt.replace_bytes(v[0], v[1], v[2])
    elif k == "ReplaceFloat":
        _log("  ReplaceFloat(%d, %s, %s)\n" % (v[0], go_float(v[1]), go_float(v[2])))
        pt.replace_float(v[0], v[1], v[2])
    elif k == "ReplaceInt":
        _log("  ReplaceInt(%d, 0x%x, 0x%x)\n" % v)
        pt.replace_int(v[0], v[1], v[2])
    elif k == "ReplaceString":
        _log("  ReplaceString(%d, %s, %s)\n" % (v[0], quote(v[1]), quote(v[2])))
        pt.replace_string(v[0], v[1], v[2])
    elif k == "FindZlib":
        _log("  FindZlib(%s) | hex:%s\n" % (quote(v), _hexs(v)))
        pt.find_zlib(v)
    elif k == "FindZlibHash":
        _log("  FindZlibHash(%s) | hex:%s\n" % (quote(v), _hexs(v)))
        pt.find_zlib_hash(v)
    elif k == "ReplaceZlib":
        _log("  ReplaceZlib(%d, %s, %s)\n" % (v[0], quote(v[1]), quote(v[2])))
        pt.replace_zlib(v[0], v[1], v[2])
    elif k == "FindReplaceString":
        find, replace = v
        _log("  FindReplaceString(%s, %s)\n" % (quote(find), quote(replace)))
        _log("    FindBaseAddressString(%s)\n" % quote(find))
        try:
            pt.find_base_address_string(find)
        except GoPanic:
            raise
        except KobopatcherError as e:
            raise Patch32lsbError("FindReplaceString: %s" % e) from None
        _log("    ReplaceString(0, %s, %s)\n" % (quote(find), quote(replace)))
        try:
            pt.replace_string(0, find, replace)
        except GoPanic:
            raise
        except KobopatcherError as e:
            raise Patch32lsbError("FindReplaceString: %s" % e) from None
    else:
        raise Patch32lsbError("invalid instruction: %r" % (i,))


# --- patch sets ---------------------------------------------------------------------


class PatchSet:
    """Patches by name, in file order (a list of Instructions each)."""

    def __init__(self):
        self.patches = {}

    def names(self):
        """Patch names in file order."""
        return list(self.patches)

    def set_enabled(self, patch: str, enabled: bool) -> None:
        p = self.patches.get(patch)
        if p is not None:
            for i in p:
                if i.kind == "Enabled":
                    i.value = bool(enabled)
                    return
            raise Patch32lsbError("could not set enabled state of '%s' to %s: no Enabled instruction in patch" % (patch, _tf(enabled)))
        if enabled:
            raise Patch32lsbError("could not set enabled state of '%s' to %s: no such patch" % (patch, _tf(enabled)))

    def validate(self) -> None:
        enabled_groups = set()
        for n, p in self.patches.items():
            pgc = ec = 0
            e = False
            pg = ""
            for i in p:
                if i.kind == "Enabled":
                    ec += 1
                    e = i.value
                elif i.kind == "PatchGroup":
                    pgc += 1
                    pg = i.value
                elif i.kind == "FindZlibHash":
                    if len(_b(i.value)) != 40:
                        raise Patch32lsbError("hash must be 40 chars in FindZlibHash in `%s`" % n)
            if ec != 1:
                raise Patch32lsbError("you must have exactly 1 patch_enable option in each patch (%s)" % n)
            if pgc > 1:
                raise Patch32lsbError("you must have at most 1 patch_group option in each patch (%s)" % n)
            if pg != "" and e:
                if pg in enabled_groups:
                    raise Patch32lsbError("more than one patch enabled in patch_group '%s'" % pg)
                enabled_groups.add(pg)

    def apply_to(self, pt) -> None:
        _log("validating patch file\n")
        try:
            self.validate()
        except Patch32lsbError as e:
            err = Patch32lsbError("invalid patch file: %s" % e)
            out("  Error: %s\n" % err)
            raise err from None
        _log("looping over patches\n")
        total = len(self.patches)
        for num, (n, p) in enumerate(self.patches.items(), 1):
            _log("  ResetBaseAddress()\n")
            pt.reset_base_address()
            enabled = any(i.kind == "Enabled" and i.value for i in p)
            _log("  Enabled: %s\n" % _tf(enabled))
            if not enabled:
                _log("  skipping patch `%s`\n" % n)
                out("  [%d/%d] Skipping disabled patch `%s`\n" % (num, total, n))
                continue
            _log("  applying patch `%s`\n" % n)
            out("  [%d/%d] Applying patch `%s`\n" % (num, total, n))
            _log("looping over instructions\n")
            for i in p:
                try:
                    _apply_instruction(pt, i)
                except GoPanic:
                    raise
                except KobopatcherError as e:
                    _log("could not apply patch: %s\n" % e)
                    out("    Error: could not apply patch: %s\n" % e)
                    raise


_EQ = re.compile(rb" +?= +?")


def _offset_string_args(ln: int, what: str, arg: bytes):
    """replace_string/replace_zlib: offset, `find`, `replace`."""
    ab = arg.split(b", ", 1)
    if len(ab) != 2:
        raise Patch32lsbError("line %d: %s malformed" % (ln, what))
    a0 = ab[0]
    if not _fixed(1) and len(a0) == 8:
        # ugly hack to fix negative offsets (fix 1)
        a0 = a0.replace(b"FFFFFF", b"-", 1)
    try:
        offset = _sscanf(a0, "%x", ["int32"], twos=_fixed(1))[0]
    except _ScanError as e:
        raise Patch32lsbError("line %d: %s offset malformed: %s" % (ln, what, e)) from None
    find, replace = _string_pair(ln, what, ab[1])
    return offset, find, replace


def _string_pair(ln: int, what: str, arg: bytes):
    try:
        find, left = _unescape_first(arg)
    except _UnescapeError as e:
        raise Patch32lsbError("line %d: %s find malformed: %s" % (ln, what, e)) from None
    left = left.lstrip(b", ")
    try:
        replace, left = _unescape_first(left)
    except _UnescapeError as e:
        raise Patch32lsbError("line %d: %s replace malformed: %s" % (ln, what, e)) from None
    if left != b"":
        raise Patch32lsbError("line %d: %s malformed: extraneous characters after last argument" % (ln, what))
    return _s(find), _s(replace)


def _scan(ln: int, what: str, arg: bytes, fmt: str, kinds):
    try:
        return _sscanf(arg, fmt, kinds, twos=_fixed(1))
    except _ScanError as e:
        raise Patch32lsbError("line %d: %s malformed: %s" % (ln, what, e)) from None


def _unesc(ln: int, prefix: str, arg: bytes) -> str:
    try:
        return _s(_unescape(arg))
    except _UnescapeError as e:
        raise Patch32lsbError("line %d: %s: %s" % (ln, prefix, e)) from None


def parse(buf: bytes) -> PatchSet:
    """Parse a patch32lsb patch file."""
    ps = PatchSet()
    patch_name = b""
    in_patch = False
    cur = []
    text = bytes(buf).replace(b"\r\n", b"\n")
    for idx, line in enumerate(text.split(b"\n")):
        ln = idx + 1
        line = _trim_space(line)
        if line.startswith(b"#") or line == b"":
            c = line.lstrip(b"# ")
            if c.startswith(b"patch_group"):
                raise Patch32lsbError("line %d: patch_group should not be a comment" % ln)
            cur.append(Instruction("Comment", _s(c)))
            continue
        low = _lower_key(line)
        if low == "<patch>":
            if in_patch:
                raise Patch32lsbError("line %d: unexpected <Patch> (already in patch)" % ln)
            cur = []
            in_patch = True
            if _fixed(2):
                patch_name = b""  # fix 2
            continue
        if low == "</patch>":
            if not in_patch:
                raise Patch32lsbError("line %d: unexpected </Patch> (not in patch)" % ln)
            if patch_name == b"":
                raise Patch32lsbError("line %d: no patch_name for patch" % ln)
            name = _s(patch_name)
            if name in ps.patches:
                raise Patch32lsbError("line %d: duplicate patch with name '%s'" % (ln, name))
            ps.patches[name] = list(cur)
            in_patch = False
            continue
        m = _EQ.search(line)
        if m is None:
            raise Patch32lsbError("line %d: bad instruction: no equals sign" % ln)
        key, arg = line[: m.start()], line[m.end() :]
        k = _lower_key(key)
        if k == "patch_name":
            patch_name = _b(_unesc(ln, "error unescaping patch_name", arg))
        elif k == "patch_group":
            cur.append(Instruction("PatchGroup", _unesc(ln, "error unescaping patch_group", arg)))
        elif k == "patch_enable":
            if patch_name == b"":
                raise Patch32lsbError("line %d: patch_enable set before patch_name" % ln)
            if arg == b"`yes`":
                cur.append(Instruction("Enabled", True))
            elif arg == b"`no`":
                cur.append(Instruction("Enabled", False))
            else:
                raise Patch32lsbError("line %d: unexpected patch_enable value '%s' (should be yes or no)" % (ln, _s(arg)))
        elif k == "replace_bytes":
            v = _scan(ln, "replace_bytes", arg.replace(b" ", b""), "%x,%x,%x", ["int32", "bytes", "bytes"])
            cur.append(Instruction("ReplaceBytes", tuple(v)))
        elif k == "base_address":
            v = _scan(ln, "base_address", arg, "%x", ["int32"])
            cur.append(Instruction("BaseAddress", v[0]))
        elif k == "replace_float":
            v = _scan(ln, "replace_float", arg.replace(b" ", b""), "%x,%f,%f", ["int32", "float64", "float64"])
            cur.append(Instruction("ReplaceFloat", tuple(v)))
        elif k == "replace_int":
            v = _scan(ln, "replace_int", arg.replace(b" ", b""), "%x,%d,%d", ["int32", "uint8", "uint8"])
            cur.append(Instruction("ReplaceInt", tuple(v)))
        elif k == "find_base_address":
            cur.append(Instruction("FindBaseAddress", _unesc(ln, "find_base_address malformed", arg)))
        elif k == "replace_string":
            cur.append(Instruction("ReplaceString", _offset_string_args(ln, "replace_string", arg)))
        elif k == "find_zlib":
            cur.append(Instruction("FindZlib", _unesc(ln, "find_zlib malformed", arg)))
        elif k == "find_zlib_hash":
            cur.append(Instruction("FindZlibHash", _unesc(ln, "find_zlib_hash malformed", arg)))
        elif k == "replace_zlib":
            cur.append(Instruction("ReplaceZlib", _offset_string_args(ln, "replace_zlib", arg)))
        elif k == "find_replace_string":
            cur.append(Instruction("FindReplaceString", _string_pair(ln, "find_replace_string", arg)))
        else:
            raise Patch32lsbError("line %d: unexpected instruction: %s" % (ln, _s(key)))
    return ps
