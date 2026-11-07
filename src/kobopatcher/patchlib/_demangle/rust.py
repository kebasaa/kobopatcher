# Port of github.com/ianlancetaylor/demangle at version
# v0.0.0-20250628045327-2d64ad6b7ec5 (BSD-3-Clause, see LICENSE in this
# directory).  This module corresponds to rust.go.
#
# demangle.ToString reaches this code for names starting with "_R"
# (Rust v0 mangling) and for old-style Rust names ("_ZN...17h<hash>E").
# The NoTemplateParams, LLVMStyle and MaxLength options are not
# supported (default options only).  Go's int64/uint64 wrap-around is
# reproduced where the mangled input controls the values.
#
# Strings hold one byte per character (see __init__.py).  The state
# keeps the original string plus a [pos, end) window instead of Go's
# rst.str slice, so Go's rst.off is pos.
"""Demangler for Rust symbol names."""

from __future__ import annotations

from typing import Tuple

from .errors import DemangleError, InternalError, NotMangledNameError

_MASK64 = (1 << 64) - 1
_MAX_INT32 = 0x7FFFFFFF
_MAX_RUNE = 0x10FFFF


def _i64(v: int) -> int:
    """Wrap v to a Go int64."""
    v &= _MASK64
    if v >= 1 << 63:
        v -= 1 << 64
    return v


def _is_digit(c: str) -> bool:
    return "0" <= c <= "9"


def rust_to_string(name: str) -> str:
    """rustToString with default options: demangle a Rust symbol."""
    if not name.startswith("_R"):
        raise NotMangledNameError()

    dot = name.find(".")
    if dot >= 0:
        # The suffix is only printed in LLVM style.
        name = name[:dot]

    name = name[2:]
    rst = RustState(name)

    rst.symbol_name()

    if rst.pos < rst.end:
        rst.fail("unparsed characters at end of mangled name")

    return "".join(rst.buf)


class RustState:
    """A rustState holds the current state of demangling a Rust string."""

    __slots__ = ("orig", "pos", "end", "buf", "skip", "lifetimes", "last")

    def __init__(self, name: str) -> None:
        self.orig = name  # the original string being demangled
        self.pos = 0  # start of the remainder (Go's rst.off)
        self.end = len(name)  # end of the remainder
        self.buf = []  # demangled string being built
        self.skip = False  # don't print, just skip
        self.lifetimes = 0  # number of bound lifetimes (int64)
        self.last = ""  # last byte written to buffer

    def rem(self) -> int:
        """len(rst.str)"""
        return self.end - self.pos

    def peek(self) -> str:
        """rst.str[0], or "" if rst.str is empty."""
        if self.pos < self.end:
            return self.orig[self.pos]
        return ""

    def fail(self, err: str):
        """Raise a demangleErr at the current offset."""
        raise DemangleError(err, self.pos)

    def advance(self, add: int) -> None:
        """Advance the current string offset."""
        if self.end - self.pos < add:
            raise InternalError("internal error")
        self.pos += add

    def check_char(self, c: str) -> None:
        """Require that the next character in the string be c, and
        advance past it."""
        if self.peek() != c:
            self.fail("expected " + c)
        self.advance(1)

    def write_byte(self, c: str) -> None:
        if self.skip:
            return
        self.last = c
        self.buf.append(c)

    def write_string(self, s: str) -> None:
        if self.skip:
            return
        if s:
            self.last = s[-1]
            self.buf.append(s)

    # symbolName parses:
    #
    #	<symbol-name> = "_R" [<decimal-number>] <path> [<instantiating-crate>]
    #	<instantiating-crate> = <path>
    #
    # We've already skipped the "_R".
    def symbol_name(self) -> None:
        if self.rem() < 1:
            self.fail("expected symbol-name")

        if _is_digit(self.peek()):
            self.fail("unsupported Rust encoding version")

        self.path(True)

        if self.rem() > 0:
            self.skip = True
            self.path(False)

    # path parses:
    #
    #	<path> = "C" <identifier>                    // crate root
    #	       | "M" <impl-path> <type>              // <T> (inherent impl)
    #	       | "X" <impl-path> <type> <path>       // <T as Trait> (trait impl)
    #	       | "Y" <type> <path>                   // <T as Trait> (trait definition)
    #	       | "N" <namespace> <path> <identifier> // ...::ident (nested path)
    #	       | "I" <path> {<generic-arg>} "E"      // ...<T, U> (generic args)
    #	       | <backref>
    #	<namespace> = "C"      // closure
    #	            | "S"      // shim
    #	            | <A-Z>    // other special namespaces
    #	            | <a-z>    // internal namespaces
    #
    # needs_separator is true if we need to write out :: for a generic;
    # it is passed as false if we are in the middle of a type.
    def path(self, needs_separator: bool) -> None:
        if self.rem() < 1:
            self.fail("expected path")
        c = self.peek()
        if c == "C":
            self.advance(1)
            _, ident = self.identifier()
            self.write_string(ident)
        elif c == "M" or c == "X":
            self.advance(1)
            self.impl_path()
            self.write_byte("<")
            self.demangle_type()
            if c == "X":
                self.write_string(" as ")
                self.path(False)
            self.write_byte(">")
        elif c == "Y":
            self.advance(1)
            self.write_byte("<")
            self.demangle_type()
            self.write_string(" as ")
            self.path(False)
            self.write_byte(">")
        elif c == "N":
            self.advance(1)

            if self.rem() < 1:
                self.fail("expected namespace")
            ns = self.peek()
            if not ("a" <= ns <= "z" or "A" <= ns <= "Z"):
                self.fail("invalid namespace character")
            self.advance(1)

            self.path(needs_separator)

            dis, ident = self.identifier()

            if "A" <= ns <= "Z":
                self.write_string("::{")
                if ns == "C":
                    self.write_string("closure")
                elif ns == "S":
                    self.write_string("shim")
                else:
                    self.write_byte(ns)
                if ident:
                    self.write_byte(":")
                    self.write_string(ident)
                if not self.skip:
                    self.buf.append("#%d}" % dis)
                    self.last = "}"
            else:
                self.write_string("::")
                self.write_string(ident)
        elif c == "I":
            self.advance(1)
            self.path(needs_separator)
            if needs_separator:
                self.write_string("::")
            self.write_byte("<")
            self.generic_args()
            self.write_byte(">")
            self.check_char("E")
        elif c == "B":
            self.backref(lambda: self.path(needs_separator))
        else:
            self.fail("unrecognized letter in path")

    # implPath parses:
    #
    #	<impl-path> = [<disambiguator>] <path>
    def impl_path(self) -> None:
        # This path is not part of the demangled string.
        hold = self.skip
        self.skip = True
        try:
            self.disambiguator()
            self.path(False)
        finally:
            self.skip = hold

    # identifier parses:
    #
    #	<identifier> = [<disambiguator>] <undisambiguated-identifier>
    #
    # It returns the disambiguator and the identifier.
    def identifier(self) -> Tuple[int, str]:
        dis = self.disambiguator()
        ident, _ = self.undisambiguated_identifier()
        return dis, ident

    # disambiguator parses an optional:
    #
    #	<disambiguator> = "s" <base-62-number>
    def disambiguator(self) -> int:
        if self.peek() != "s":
            return 0
        self.advance(1)
        return _i64(self.base62_number() + 1)

    # undisambiguatedIdentifier parses:
    #
    #	<undisambiguated-identifier> = ["u"] <decimal-number> ["_"] <bytes>
    def undisambiguated_identifier(self) -> Tuple[str, bool]:
        is_punycode = False
        if self.peek() == "u":
            self.advance(1)
            is_punycode = True

        val = self.decimal_number()

        if self.peek() == "_":
            self.advance(1)

        if self.rem() < val:
            self.fail("not enough characters for identifier")
        id_ = self.orig[self.pos : self.pos + val]
        self.advance(val)

        for c in id_:
            if not ("0" <= c <= "9" or "A" <= c <= "Z" or "a" <= c <= "z" or c == "_"):
                self.fail("invalid character in identifier")

        if is_punycode:
            id_ = self.expand_punycode(id_)

        return id_, is_punycode

    def expand_punycode(self, s: str) -> str:
        """Decode the Rust version of punycode.  This algorithm is taken
        from RFC 3492 section 6.2."""
        base = 36
        tmin = 1
        tmax = 26
        skew = 38
        damp = 700
        initial_bias = 72
        initial_n = 128

        idx = s.rfind("_")
        if idx >= 0:
            output = [ord(ch) for ch in s[:idx]]
            encoding = s[idx + 1 :]
        else:
            output = []
            encoding = s

        i = 0
        n = initial_n
        bias = initial_bias

        pos = 0
        while pos < len(encoding):
            old_i = i
            w = 1
            k = base
            while True:
                if pos == len(encoding):
                    self.fail("unterminated punycode")

                d = encoding[pos]
                pos += 1
                if "0" <= d <= "9":
                    digit = ord(d) - 48 + 26
                elif "A" <= d <= "Z":
                    digit = ord(d) - 65
                elif "a" <= d <= "z":
                    digit = ord(d) - 97
                else:
                    self.fail("invalid punycode digit")

                i += digit * w
                if i < 0:
                    self.fail("punycode number overflow")

                if k <= bias:
                    t = tmin
                elif k > bias + tmax:
                    t = tmax
                else:
                    t = k - bias

                if digit < t:
                    break

                if w >= _MAX_INT32 // base:
                    self.fail("punycode number overflow")
                w *= base - t
                k += base

            delta = i - old_i
            num_points = len(output) + 1
            first_time = old_i == 0
            if first_time:
                delta //= damp
            else:
                delta //= 2
            delta += delta // num_points
            k = 0
            while delta > ((base - tmin) * tmax) // 2:
                delta //= base - tmin
                k += base
            bias = k + ((base - tmin + 1) * delta) // (delta + skew)

            n += i // (len(output) + 1)
            if n > _MAX_RUNE:
                self.fail("punycode rune overflow")
            elif 0xD800 <= n <= 0xDFFF or n < 0:
                self.fail("punycode invalid code point")
            i %= len(output) + 1
            output.insert(i, n)
            i += 1

        # string(output): UTF-8 encode, then back to one char per byte.
        return "".join(chr(r) for r in output).encode("utf-8").decode("latin-1")

    def generic_args(self) -> None:
        """Print a list of generic arguments, without angle brackets."""
        first = True
        while self.rem() > 0 and self.peek() != "E":
            if first:
                first = False
            else:
                self.write_string(", ")
            self.generic_arg()

    # genericArg parses:
    #
    #	<generic-arg> = <lifetime>
    #	              | <type>
    #	              | "K" <const> // forward-compat for const generics
    #	<lifetime> = "L" <base-62-number>
    def generic_arg(self) -> None:
        if self.rem() < 1:
            self.fail("expected generic-arg")
        c = self.peek()
        if c == "L":
            self.advance(1)
            self.write_lifetime(self.base62_number())
        elif c == "K":
            self.advance(1)
            self.demangle_const()
        else:
            self.demangle_type()

    # binder parses an optional:
    #
    #	<binder> = "G" <base-62-number>
    def binder(self) -> None:
        if self.rem() < 1 or self.peek() != "G":
            return
        self.advance(1)

        binder_lifetimes = _i64(self.base62_number() + 1)

        # Every bound lifetime should be referenced later.
        if binder_lifetimes >= _i64(self.rem() - self.lifetimes):
            self.fail("binder lifetimes overflow")

        self.write_string("for<")
        i = 0
        while i < binder_lifetimes:
            if i > 0:
                self.write_string(", ")
            self.lifetimes = _i64(self.lifetimes + 1)
            self.write_lifetime(1)
            i += 1
        self.write_string("> ")

    # demangleType parses:
    #
    #	<type> = <basic-type>
    #	       | <path>                      // named type
    #	       | "A" <type> <const>          // [T; N]
    #	       | "S" <type>                  // [T]
    #	       | "T" {<type>} "E"            // (T1, T2, T3, ...)
    #	       | "R" [<lifetime>] <type>     // &T
    #	       | "Q" [<lifetime>] <type>     // &mut T
    #	       | "P" <type>                  // *const T
    #	       | "O" <type>                  // *mut T
    #	       | "F" <fn-sig>                // fn(...) -> ...
    #	       | "D" <dyn-bounds> <lifetime> // dyn Trait<Assoc = X> + Send + 'a
    #	       | <backref>
    def demangle_type(self) -> None:
        if self.rem() < 1:
            self.fail("expected type")
        c = self.peek()
        if "a" <= c <= "z":
            self.basic_type()
            return
        if c in ("C", "M", "X", "Y", "N", "I"):
            self.path(False)
        elif c == "A" or c == "S":
            self.advance(1)
            self.write_byte("[")
            self.demangle_type()
            if c == "A":
                self.write_string("; ")
                self.demangle_const()
            self.write_byte("]")
        elif c == "T":
            self.advance(1)
            self.write_byte("(")
            cnt = 0
            while self.rem() > 0 and self.peek() != "E":
                if cnt > 0:
                    self.write_string(", ")
                cnt += 1
                self.demangle_type()
            if cnt == 1:
                self.write_byte(",")
            self.write_byte(")")
            self.check_char("E")
        elif c == "R" or c == "Q":
            self.advance(1)
            self.write_byte("&")
            if self.peek() == "L":
                self.advance(1)
                lifetime = self.base62_number()
                if lifetime > 0:
                    self.write_lifetime(lifetime)
                    self.write_byte(" ")
            if c == "Q":
                self.write_string("mut ")
            self.demangle_type()
        elif c == "P":
            self.advance(1)
            self.write_string("*const ")
            self.demangle_type()
        elif c == "O":
            self.advance(1)
            self.write_string("*mut ")
            self.demangle_type()
        elif c == "F":
            self.advance(1)
            hold = self.lifetimes
            self.fn_sig()
            self.lifetimes = hold
        elif c == "D":
            self.advance(1)
            hold = self.lifetimes
            self.dyn_bounds()
            self.lifetimes = hold
            if self.peek() != "L":
                self.fail("expected L")
            self.advance(1)
            lifetime = self.base62_number()
            if lifetime > 0:
                if self.last != " ":
                    self.write_byte(" ")
                self.write_string("+ ")
                self.write_lifetime(lifetime)
        elif c == "B":
            self.backref(self.demangle_type)
        else:
            self.fail("unrecognized character in type")

    def basic_type(self) -> None:
        """basicType parses <basic-type>."""
        if self.rem() < 1:
            self.fail("expected basic type")
        s = RUST_BASIC_TYPES.get(self.peek())
        if s is None:
            self.fail("unrecognized basic type character")
        self.advance(1)
        self.write_string(s)

    # fnSig parses:
    #
    #	<fn-sig> = [<binder>] ["U"] ["K" <abi>] {<type>} "E" <type>
    #	<abi> = "C"
    #	      | <undisambiguated-identifier>
    def fn_sig(self) -> None:
        self.binder()
        if self.peek() == "U":
            self.advance(1)
            self.write_string("unsafe ")
        if self.peek() == "K":
            self.advance(1)
            if self.peek() == "C":
                self.advance(1)
                self.write_string('extern "C" ')
            else:
                self.write_string('extern "')
                id_, is_punycode = self.undisambiguated_identifier()
                if is_punycode:
                    self.fail("punycode used in ABI string")
                id_ = id_.replace("_", "-")
                self.write_string(id_)
                self.write_string('" ')
        self.write_string("fn(")
        first = True
        while self.rem() > 0 and self.peek() != "E":
            if first:
                first = False
            else:
                self.write_string(", ")
            self.demangle_type()
        self.check_char("E")
        self.write_byte(")")
        if self.peek() == "u":
            self.advance(1)
        else:
            self.write_string(" -> ")
            self.demangle_type()

    # dynBounds parses:
    #
    #	<dyn-bounds> = [<binder>] {<dyn-trait>} "E"
    def dyn_bounds(self) -> None:
        self.write_string("dyn ")
        self.binder()
        first = True
        while self.rem() > 0 and self.peek() != "E":
            if first:
                first = False
            else:
                self.write_string(" + ")
            self.dyn_trait()
        self.check_char("E")

    # dynTrait parses:
    #
    #	<dyn-trait> = <path> {<dyn-trait-assoc-binding>}
    #	<dyn-trait-assoc-binding> = "p" <undisambiguated-identifier> <type>
    def dyn_trait(self) -> None:
        started = self.path_start_generics()
        while self.peek() == "p":
            self.advance(1)
            if started:
                self.write_string(", ")
            else:
                self.write_byte("<")
                started = True
            id_, _ = self.undisambiguated_identifier()
            self.write_string(id_)
            self.write_string(" = ")
            self.demangle_type()
        if started:
            self.write_byte(">")

    def path_start_generics(self) -> bool:
        """Like path but if it sees an I to start generic arguments it
        won't close them. It reports whether it started generics."""
        if self.rem() < 1:
            self.fail("expected path")
        c = self.peek()
        if c == "I":
            self.advance(1)
            self.path(False)
            self.write_byte("<")
            self.generic_args()
            self.check_char("E")
            return True
        elif c == "B":
            started = [False]

            def f():
                started[0] = self.path_start_generics()

            self.backref(f)
            return started[0]
        else:
            self.path(False)
            return False

    def write_lifetime(self, lifetime: int) -> None:
        """Write out a lifetime binding."""
        self.write_byte("'")
        if lifetime == 0:
            self.write_byte("_")
            return
        depth = _i64(self.lifetimes - lifetime)
        if depth < 0:
            self.fail("invalid lifetime")
        elif depth < 26:
            self.write_byte(chr(97 + depth))
        else:
            self.write_byte("z")
            if not self.skip:
                self.buf.append("%d" % (depth - 26 + 1))
                self.last = "0"

    # demangleConst parses:
    #
    #	<const> = <type> <const-data>
    #	        | "p" // placeholder, shown as _
    #	        | <backref>
    #	<const-data> = ["n"] {<hex-digit>} "_"
    def demangle_const(self) -> None:
        if self.rem() < 1:
            self.fail("expected constant")

        c = self.peek()
        if c == "B":
            self.backref(self.demangle_const)
            return

        if c == "p":
            self.advance(1)
            self.write_byte("_")
            return

        typ = c

        if typ in ("a", "s", "l", "x", "n", "i"):
            kind = "signed"
        elif typ in ("h", "t", "m", "y", "o", "j"):
            kind = "unsigned"
        elif typ == "b":
            kind = "boolean"
        elif typ == "c":
            kind = "character"
        else:
            self.fail("unrecognized constant type")

        self.advance(1)

        if kind == "signed" and self.peek() == "n":
            self.advance(1)
            self.write_byte("-")

        start = self.pos
        digits = 0
        val = 0
        while self.rem() > 0:
            c = self.peek()
            if "0" <= c <= "9":
                digit = ord(c) - 48
            elif "a" <= c <= "f":
                digit = ord(c) - 97 + 10
            elif c == "_":
                self.advance(1)
                break
            else:
                self.fail("expected hex digit or _")
            self.advance(1)
            if val == 0 and digit == 0 and self.peek() != "_":
                self.fail("invalid leading 0 in constant")
            val = (val * 16 + digit) & _MASK64
            digits += 1

        if digits == 0:
            self.fail("expected constant")

        if kind == "signed" or kind == "unsigned":
            if digits > 16:
                # Value too big, just write out the string.
                self.write_string("0x")
                self.write_string(self.orig[start : start + digits])
            else:
                if not self.skip:
                    self.buf.append("%d" % val)
                    self.last = "0"
        elif kind == "boolean":
            if digits > 1:
                self.fail("boolean value too large")
            elif val == 0:
                self.write_string("false")
            elif val == 1:
                self.write_string("true")
            else:
                self.fail("invalid boolean value")
        else:  # character
            if digits > 6:
                self.fail("character value too large")
            self.write_byte("'")
            if val == 9:
                self.write_string("\\t")
            elif val == 13:
                self.write_string("\\r")
            elif val == 10:
                self.write_string("\\n")
            elif val == 0x5C:
                self.write_string("\\\\")
            elif val == 0x27:
                self.write_string("\\'")
            elif 0x20 <= val <= 0x7E:
                # printable ASCII character
                self.write_byte(chr(val))
            else:
                if not self.skip:
                    self.buf.append("\\u{%x}" % val)
                    self.last = "}"
            self.write_byte("'")

    # base62Number parses:
    #
    #	<base-62-number> = {<0-9a-zA-Z>} "_"
    def base62_number(self) -> int:
        if self.peek() == "_":
            self.advance(1)
            return 0
        val = 0
        while self.rem() > 0:
            c = self.peek()
            self.advance(1)
            if c == "_":
                return _i64(val + 1)
            val = _i64(val * 62)
            if "0" <= c <= "9":
                val = _i64(val + ord(c) - 48)
            elif "a" <= c <= "z":
                val = _i64(val + ord(c) - 97 + 10)
            elif "A" <= c <= "Z":
                val = _i64(val + ord(c) - 65 + 36)
            else:
                self.fail("invalid digit in base 62 number")
        self.fail("expected _ after base 62 number")

    # backref parses:
    #
    #	<backref> = "B" <base-62-number>
    def backref(self, demangle) -> None:
        backoff = self.pos

        self.check_char("B")
        idx = self.base62_number()

        if self.skip:
            return

        if idx < 0 or idx >= backoff:
            self.fail("invalid backref index")

        hold_pos = self.pos
        hold_end = self.end
        self.pos = idx
        self.end = backoff
        try:
            demangle()
        finally:
            self.pos = hold_pos
            self.end = hold_end

    def decimal_number(self) -> int:
        if self.rem() == 0:
            self.fail("expected number")

        val = 0
        while self.rem() > 0 and _is_digit(self.peek()):
            add = ord(self.peek()) - 48
            if val >= _MAX_INT32 // 10 - add:
                self.fail("decimal number overflow")
            val *= 10
            val += add
            self.advance(1)
        return val


RUST_BASIC_TYPES = {
    "a": "i8",
    "b": "bool",
    "c": "char",
    "d": "f64",
    "e": "str",
    "f": "f32",
    "h": "u8",
    "i": "isize",
    "j": "usize",
    "l": "i32",
    "m": "u32",
    "n": "i128",
    "o": "u128",
    "p": "_",
    "s": "i16",
    "t": "u16",
    "u": "()",
    "v": "...",
    "x": "i64",
    "y": "u64",
    "z": "!",
}


def _hex_digit(c: str) -> Tuple[int, bool]:
    if "0" <= c <= "9":
        return ord(c) - 48, True
    if "a" <= c <= "f":
        return ord(c) - 97 + 10, True
    return 0, False


_OLD_RUST_CODES = {
    "SP": "@",
    "BP": "*",
    "RF": "&",
    "LT": "<",
    "GT": ">",
    "LP": "(",
    "RP": ")",
}


def old_rust_to_string(name: str) -> Tuple[str, bool]:
    """Demangle a Rust symbol using the old demangling.  The second
    result reports whether this is a valid Rust mangled name."""
    # We know that the string starts with _ZN.
    name = name[3:]

    # We know that the strings end with "17h" followed by 16 characters
    # followed by "E". We check that the 16 characters are all hex digits.
    # Also the hex digits must contain at least 5 distinct digits.
    seen = 0
    for i in range(len(name) - 17, len(name) - 1):
        digit, ok = _hex_digit(name[i])
        if not ok:
            return "", False
        seen |= 1 << digit
    if bin(seen).count("1") < 5:
        return "", False
    name = name[: len(name) - 20]

    # The name is a sequence of length-preceded identifiers.
    sb = []
    sb_len = 0
    while name:
        if not _is_digit(name[0]):
            return "", False

        val = 0
        while name and _is_digit(name[0]):
            add = ord(name[0]) - 48
            if val >= _MAX_INT32 // 10 - add:
                return "", False
            val *= 10
            val += add
            name = name[1:]

        # An optional trailing underscore can separate the
        # length from the identifier.
        if name and name[0] == "_":
            name = name[1:]
            val -= 1

        if len(name) < val:
            return "", False

        if val < 0:
            # name[:val] with a negative val panics in Go.
            raise InternalError("slice bounds out of range")
        id_ = name[:val]
        name = name[val:]

        if sb_len > 0:
            sb.append("::")
            sb_len += 2

        # Ignore leading underscores preceding escape sequences.
        if id_.startswith("_$"):
            id_ = id_[1:]

        # The identifier can have escape sequences.
        out = []
        while id_:
            c = id_[0]
            if c == "$":
                valid = True
                if len(id_) > 2 and id_[1] == "C" and id_[2] == "$":
                    out.append(",")
                    id_ = id_[3:]
                elif len(id_) > 4 and id_[1] == "u" and id_[4] == "$":
                    dig1, ok1 = _hex_digit(id_[2])
                    dig2, ok2 = _hex_digit(id_[3])
                    v = ((dig1 << 4) | dig2) & 0xFF
                    if not ok1 or not ok2 or dig1 > 7 or v < 0x20:
                        valid = False
                    else:
                        out.append(chr(v))
                        id_ = id_[5:]
                elif len(id_) > 3 and id_[3] == "$":
                    code = _OLD_RUST_CODES.get(id_[1:3])
                    if code is None:
                        valid = False
                    else:
                        out.append(code)
                        id_ = id_[4:]
                else:
                    valid = False
                if not valid:
                    out.append(id_)
                    break
            elif c == ".":
                if id_.startswith(".."):
                    out.append("::")
                    id_ = id_[2:]
                else:
                    out.append(c)
                    id_ = id_[1:]
            else:
                out.append(c)
                id_ = id_[1:]
        piece = "".join(out)
        sb.append(piece)
        sb_len += len(piece)

    return "".join(sb), True
