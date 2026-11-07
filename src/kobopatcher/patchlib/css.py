"""CSS detection heuristic (``IsCSS``) and the CSS Syntax Level 3 tokenizer it uses.

This module is a faithful port of two pieces of Go code:

* ``patchlib/css.go`` (``IsCSS``) from kobopatch
  (https://github.com/pgaskin/kobopatch, MIT licensed, Copyright Patrick Gaskin).
* The ``tokenizer`` package of github.com/riking/cssparse at
  v0.0.0-20180325025645-c37ded0aac89 (Copyright 2018 Kane York, Copyright 2012
  The Gorilla Authors; BSD licensed, see ``LICENSE.cssparse`` next to this file),
  including its input normaliser ``crlf.go`` (Copyright 2015 Andy Balholm,
  2-clause BSD), which runs as a golang.org/x/text ``transform.Reader``
  underneath a ``bufio.Reader``.

Only the tokenizer is ported, not the token renderer (``Token.WriteTo``,
``TokenRenderer``) or the older ``scanner`` package, which IsCSS does not use.

Behaviour that matters for parity and is reproduced here on purpose:

* The tokenizer works on bytes, like the Go original. Input is normalised
  first: CRLF and CR become LF, NUL becomes U+FFFD (EF BF BD). Form feed is
  *not* normalised (the Go code does not do it either) and is not whitespace,
  so it is tokenised as a delimiter.
* Comments are emitted as COMMENT tokens, and whitespace runs as S tokens;
  both take part in IsCSS's prev/cur token window.
* ``Tokenizer.err()`` becomes EOF as soon as any byte read hits the end of
  input, so the token being consumed at that moment is dropped by IsCSS,
  which checks the error before looking at the token. Peeks (used e.g. by
  whitespace runs) never set the error.
* ``bufio.Reader.UnreadByte`` only works directly after a read; a Peek or
  Discard in between makes it fail silently. This is emulated exactly.
* Invalid UTF-8 is never rejected: bytes >= 0x80 are name characters, and
  the only rune decode (an escaped non-hex character) uses Go's
  ``utf8.DecodeRune`` rules (U+FFFD, width 1 on error).

Token values are Go byte strings; here they are ``str`` decoded from those
bytes with ``errors="surrogateescape"``, so ``value.encode("utf-8",
"surrogateescape")`` recovers the exact Go bytes.
"""

from __future__ import annotations

import enum
import io
from dataclasses import dataclass
from typing import Optional, Union

__all__ = [
    "EOF",
    "ParseError",
    "Token",
    "TokenExtraError",
    "TokenExtraHash",
    "TokenExtraNumeric",
    "TokenExtraUnicodeRange",
    "TokenType",
    "Tokenizer",
    "is_css",
    "normalize",
]


class TokenType(enum.IntEnum):
    """Lexical token types, numbered exactly as the Go ``TokenType`` iota."""

    # Scanner flags.
    ERROR = 0
    EOF = 1
    # Tokens
    IDENT = 2
    FUNCTION = 3
    URI = 4
    DELIM = 5  # single character
    AT_KEYWORD = 6
    STRING = 7
    S = 8  # whitespace
    COMMENT = 9  # preserved, unlike CSS Syntax Level 3
    HASH = 10  # extra: TokenExtraHash
    NUMBER = 11  # extra: TokenExtraNumeric
    PERCENTAGE = 12
    DIMENSION = 13
    UNICODE_RANGE = 14  # extra: TokenExtraUnicodeRange
    # Error tokens
    BAD_STRING = 15
    BAD_URI = 16
    BAD_ESCAPE = 17  # a '\' right before a newline
    # Fixed-string tokens
    INCLUDES = 18
    DASH_MATCH = 19
    PREFIX_MATCH = 20
    SUFFIX_MATCH = 21
    SUBSTRING_MATCH = 22
    COLUMN = 23
    COLON = 24
    SEMICOLON = 25
    COMMA = 26
    OPEN_BRACKET = 27
    CLOSE_BRACKET = 28
    OPEN_PAREN = 29
    CLOSE_PAREN = 30
    OPEN_BRACE = 31
    CLOSE_BRACE = 32
    CDO = 33
    CDC = 34

    def __str__(self) -> str:  # Go TokenType.String()
        return _TOKEN_NAMES[self]

    def stop_token(self) -> bool:
        """Go ``TokenType.StopToken``."""
        return self in (
            TokenType.ERROR,
            TokenType.EOF,
            TokenType.BAD_ESCAPE,
            TokenType.BAD_STRING,
            TokenType.BAD_URI,
        )


_TOKEN_NAMES = {
    TokenType.ERROR: "error",
    TokenType.EOF: "EOF",
    TokenType.IDENT: "IDENT",
    TokenType.AT_KEYWORD: "ATKEYWORD",
    TokenType.STRING: "STRING",
    TokenType.HASH: "HASH",
    TokenType.NUMBER: "NUMBER",
    TokenType.PERCENTAGE: "PERCENTAGE",
    TokenType.DIMENSION: "DIMENSION",
    TokenType.URI: "URI",
    TokenType.UNICODE_RANGE: "UNICODE-RANGE",
    TokenType.CDO: "CDO",
    TokenType.CDC: "CDC",
    TokenType.S: "S",
    TokenType.COMMENT: "COMMENT",
    TokenType.FUNCTION: "FUNCTION",
    TokenType.INCLUDES: "INCLUDES",
    TokenType.DASH_MATCH: "DASHMATCH",
    TokenType.PREFIX_MATCH: "PREFIXMATCH",
    TokenType.SUFFIX_MATCH: "SUFFIXMATCH",
    TokenType.SUBSTRING_MATCH: "SUBSTRINGMATCH",
    TokenType.DELIM: "DELIM",
    TokenType.BAD_STRING: "BAD-STRING",
    TokenType.BAD_URI: "BAD-URI",
    TokenType.BAD_ESCAPE: "BAD-ESCAPE",
    TokenType.COLUMN: "COLUMN",
    TokenType.COLON: "COLON",
    TokenType.SEMICOLON: "SEMICOLON",
    TokenType.COMMA: "COMMA",
    TokenType.OPEN_BRACKET: "LEFT-BRACKET",
    TokenType.CLOSE_BRACKET: "RIGHT-BRACKET",
    TokenType.OPEN_PAREN: "LEFT-PAREN",
    TokenType.CLOSE_PAREN: "RIGHT-PAREN",
    TokenType.OPEN_BRACE: "LEFT-BRACE",
    TokenType.CLOSE_BRACE: "RIGHT-BRACE",
}


class ParseError(Exception):
    """Go ``*ParseError``: a CSS syntax error attached to an error token."""

    def __init__(self, type: TokenType, message: str, loc: int = 0) -> None:
        super().__init__(message)
        self.type = type
        self.message = message
        self.loc = loc

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, ParseError)
            and (self.type, self.message, self.loc) == (other.type, other.message, other.loc)
        )

    def __hash__(self) -> int:
        return hash((self.type, self.message, self.loc))


@dataclass(frozen=True)
class TokenExtraHash:
    """Attached to HASH tokens."""

    is_identifier: bool = False

    def __str__(self) -> str:
        return "id" if self.is_identifier else "unrestricted"


@dataclass(frozen=True)
class TokenExtraNumeric:
    """Attached to NUMBER, PERCENTAGE and DIMENSION tokens."""

    non_integer: bool = False
    dimension: str = ""

    def __str__(self) -> str:
        return self.dimension


@dataclass(frozen=True)
class TokenExtraUnicodeRange:
    """Attached to UNICODE_RANGE tokens."""

    start: int = 0
    end: int = 0

    def __str__(self) -> str:
        if self.start == self.end:
            return "U+%04X" % self.start
        return "U+%04X-%04X" % (self.start, self.end)


@dataclass(frozen=True)
class TokenExtraError:
    """Attached to ERROR, BAD_ESCAPE, BAD_STRING and BAD_URI tokens."""

    err: Optional[BaseException] = None

    def __str__(self) -> str:
        return str(self.err)

    def parse_error(self) -> Optional[ParseError]:
        return self.err if isinstance(self.err, ParseError) else None


TokenExtra = Union[TokenExtraHash, TokenExtraNumeric, TokenExtraUnicodeRange, TokenExtraError]


@dataclass(frozen=True)
class Token:
    """A CSS token. ``value`` is the Go byte string, surrogateescape-decoded."""

    type: TokenType = TokenType.ERROR
    value: str = ""
    extra: Optional[TokenExtra] = None


#: The error ``Tokenizer.err()`` returns at end of input (Go ``io.EOF``).
EOF = EOFError("EOF")

_ZERO_TOKEN = Token()
_EOF_TOKEN = Token(TokenType.EOF)
_RUNE_ERROR = 0xFFFD
_MAX_RUNE = 0x10FFFF
_WS = (0x20, 0x09, 0x0A)  # ' ', '\t', '\n'

_PREMADE = {
    0x24: Token(TokenType.SUFFIX_MATCH, "$="),  # '$'
    0x2A: Token(TokenType.SUBSTRING_MATCH, "*="),  # '*'
    0x5E: Token(TokenType.PREFIX_MATCH, "^="),  # '^'
    0x7E: Token(TokenType.INCLUDES, "~="),  # '~'
    0x28: Token(TokenType.OPEN_PAREN, "("),
    0x29: Token(TokenType.CLOSE_PAREN, ")"),
    0x5B: Token(TokenType.OPEN_BRACKET, "["),
    0x5D: Token(TokenType.CLOSE_BRACKET, "]"),
    0x7B: Token(TokenType.OPEN_BRACE, "{"),
    0x7D: Token(TokenType.CLOSE_BRACE, "}"),
    0x3A: Token(TokenType.COLON, ":"),
    0x3B: Token(TokenType.SEMICOLON, ";"),
    0x2C: Token(TokenType.COMMA, ","),
}
_BAD_ESCAPE_TOKEN = Token(TokenType.BAD_ESCAPE, "\\")
_DASH_MATCH_TOKEN = Token(TokenType.DASH_MATCH, "|=")
_COLUMN_TOKEN = Token(TokenType.COLUMN, "||")
_CDC_TOKEN = Token(TokenType.CDC, "-->")
_CDO_TOKEN = Token(TokenType.CDO, "<!--")


def normalize(data: bytes) -> bytes:
    """cssparse ``crlf.go``: CRLF/CR -> LF and NUL -> U+FFFD (as UTF-8).

    The Go transformer keeps the previous source byte across Transform calls,
    and its ErrShortDst paths only re-run the same bytes, so the result does
    not depend on how transform.Reader chunks the input.
    """
    if b"\r" in data:
        data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    if b"\x00" in data:
        data = data.replace(b"\x00", b"\xef\xbf\xbd")
    return data


def _is_name_start(p: int) -> bool:
    return p >= 0x80 or p == 0x5F or 0x41 <= p <= 0x5A or 0x61 <= p <= 0x7A


def _is_name_code(p: int) -> bool:
    return (
        p >= 0x80
        or p == 0x5F
        or p == 0x2D
        or 0x41 <= p <= 0x5A
        or 0x61 <= p <= 0x7A
        or 0x30 <= p <= 0x39
    )


def _is_hex_digit(p: int) -> bool:
    return 0x41 <= p <= 0x46 or 0x61 <= p <= 0x66 or 0x30 <= p <= 0x39


def _is_valid_escape(p) -> bool:  # §4.3.8, up to 2 bytes
    if len(p) < 2:
        return False
    if p[0] != 0x5C:  # '\\'
        return False
    if p[1] == 0x0A:
        return False
    return True


def _is_start_identifier(p) -> bool:  # up to 3 bytes
    if p[0] == 0x2D:  # '-'
        p = p[1:]
    if _is_name_start(p[0]):
        return True
    return _is_valid_escape(p)


def _is_start_number(p) -> bool:  # §4.3.10, up to 3 bytes
    if p[0] == 0x2B or p[0] == 0x2D:  # '+' '-'
        p = p[1:]
    if p[0] == 0x2E:  # '.'
        p = p[1:]
    return 0x30 <= p[0] <= 0x39


def _is_non_printable(by: int) -> bool:
    return by <= 0x08 or by == 0x0B or 0x0E <= by <= 0x1F or by == 0x7F


def _encode_rune(cp: int) -> bytes:
    """Go ``utf8.EncodeRune``: invalid runes (surrogates, > MaxRune) -> U+FFFD."""
    if cp < 0 or cp > _MAX_RUNE or 0xD800 <= cp <= 0xDFFF:
        cp = _RUNE_ERROR
    return chr(cp).encode("utf-8")


def _decode_rune(b: bytes) -> tuple[int, int]:
    """Go ``utf8.DecodeRune`` on a non-empty byte slice."""
    b0 = b[0]
    if b0 < 0x80:
        return b0, 1
    if 0xC2 <= b0 <= 0xDF:
        n = 2
    elif 0xE0 <= b0 <= 0xEF:
        n = 3
    elif 0xF0 <= b0 <= 0xF4:
        n = 4
    else:
        return _RUNE_ERROR, 1
    try:
        # Python's strict UTF-8 decoder rejects exactly what Go rejects
        # (overlongs, surrogates, > U+10FFFF, bad continuation bytes).
        s = bytes(b[:n]).decode("utf-8")
    except UnicodeDecodeError:
        return _RUNE_ERROR, 1
    if len(s) != 1:
        return _RUNE_ERROR, 1
    return ord(s), n


def _str(b) -> str:
    return bytes(b).decode("utf-8", "surrogateescape")


class _BufReader:
    """The parts of Go's ``bufio.Reader`` the tokenizer uses, over a flat buffer.

    Only the observable behaviour is modelled: the byte position and the
    ``lastByte`` bookkeeping that decides whether ``UnreadByte`` succeeds.
    (Buffer refills cannot be observed: Peek(n) with n <= 32 always sees the
    next n bytes or everything up to EOF, and after a successful read the read
    index is never 0, so UnreadByte fails only when lastByte < 0.)
    """

    __slots__ = ("data", "pos", "last_byte")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0
        self.last_byte = -1

    def read_byte(self) -> int:
        """Returns the byte, or -1 on EOF (lastByte is left untouched on EOF)."""
        pos = self.pos
        if pos >= len(self.data):
            return -1
        c = self.data[pos]
        self.pos = pos + 1
        self.last_byte = c
        return c

    def unread_byte(self) -> bool:
        if self.last_byte < 0:
            return False  # bufio.ErrInvalidUnreadByte, ignored by the tokenizer
        self.pos -= 1
        self.last_byte = -1
        return True

    def peek(self, n: int) -> bytes:
        self.last_byte = -1
        return self.data[self.pos : self.pos + n]

    def discard(self, n: int) -> int:
        self.last_byte = -1
        n = min(n, len(self.data) - self.pos)
        self.pos += n
        return n

    def read_rune(self) -> tuple[int, int]:
        """Returns (rune, size); size 0 means EOF."""
        pos = self.pos
        if pos >= len(self.data):
            return 0, 0
        r, size = _decode_rune(self.data[pos : pos + 4])
        self.pos = pos + size
        self.last_byte = self.data[self.pos - 1]
        return r, size


InputType = Union[str, bytes, bytearray, memoryview, io.RawIOBase, io.BufferedIOBase]


def _to_bytes(src) -> bytes:
    if isinstance(src, str):
        try:
            return src.encode("utf-8", "surrogateescape")
        except UnicodeEncodeError:
            return src.encode("utf-8", "surrogatepass")
    if isinstance(src, (bytes, bytearray, memoryview)):
        return bytes(src)
    if hasattr(src, "read"):
        data = src.read()
        return _to_bytes(data)
    raise TypeError("unsupported tokenizer input: %r" % type(src).__name__)


class Tokenizer:
    """Port of cssparse ``tokenizer.Tokenizer``.

    The input may be ``bytes``-like, a binary file object, or a ``str``
    (encoded as UTF-8, with surrogateescape so that strings decoded from
    arbitrary bytes round-trip). It is normalised like ``NewTokenizer`` does.
    """

    def __init__(self, src: InputType) -> None:
        self._r = _BufReader(normalize(_to_bytes(src)))
        self._err: Optional[BaseException] = None
        self._peek = [0, 0, 0]
        self._tok = _ZERO_TOKEN

    # --- public API (mirrors Go) ---

    def scan(self) -> None:
        """Scan for the next token. If in an error state, no input is consumed."""
        if self._err is None:
            self._tok = self._consume()
        elif self._err is EOF:
            self._tok = _EOF_TOKEN
        else:  # pragma: no cover - no I/O errors are possible on a byte buffer
            self._tok = Token(TokenType.ERROR, str(self._err), TokenExtraError(self._err))

    def token(self) -> Token:
        """The most recently scanned token."""
        return self._tok

    def next(self) -> Token:
        """Scan for the next token and return it."""
        self.scan()
        return self._tok

    def err(self) -> Optional[BaseException]:
        """The last input reading error (``EOF`` at end of input), or None."""
        return self._err

    def __iter__(self):
        """Yields tokens until (excluding) the EOF token, like Fuzz's loop."""
        while True:
            t = self.next()
            if t.type == TokenType.EOF or t.type == TokenType.ERROR:
                return
            yield t

    # --- reader helpers ---

    def _repeek(self) -> None:
        by = self._r.peek(3)
        p = self._peek
        n = len(by)
        p[0] = by[0] if n > 0 else 0
        p[1] = by[1] if n > 1 else 0
        p[2] = by[2] if n > 2 else 0

    def _next_byte(self) -> int:
        """Next byte, 0 on EOF (setting err to EOF)."""
        if self._err is EOF:
            return 0
        by = self._r.read_byte()
        if by < 0:
            self._err = EOF
            return 0
        return by

    def _unread_byte(self) -> None:
        if self._err is EOF:
            return  # don't unread after EOF
        self._r.unread_byte()

    def _next_is_escape(self) -> bool:
        return _is_valid_escape(self._peek[:2])

    def _next_starts_identifier(self) -> bool:
        return _is_start_identifier(self._peek[:3])

    def _next_is_number(self) -> bool:
        return _is_start_number(self._peek[:3])

    def _next_compare(self, vs: bytes) -> bool:
        return bytes(self._peek[: len(vs)]) == vs

    # --- 4.3.1 ---

    def _consume(self) -> Token:
        ch = self._next_byte()

        if ch == 0:  # EOF
            return _EOF_TOKEN
        elif ch == 0x0A or ch == 0x09 or ch == 0x20:
            return self._consume_whitespace(ch)
        elif ch == 0x22 or ch == 0x27:  # '"' '\''
            return self._consume_string(ch)
        elif ch == 0x23:  # '#'
            self._repeek()
            if _is_name_code(self._peek[0]) or self._next_is_escape():
                e = TokenExtraHash(is_identifier=self._next_starts_identifier())
                return Token(TokenType.HASH, _str(self._consume_name()), e)
        elif ch in _PREMADE and ch not in (0x24, 0x2A, 0x5E, 0x7E):
            return _PREMADE[ch]
        elif ch in (0x24, 0x2A, 0x5E, 0x7E):  # '$' '*' '^' '~'
            self._repeek()
            if self._peek[0] == 0x3D:  # '='
                self._r.discard(1)
                return _PREMADE[ch]
        elif ch == 0x7C:  # '|'
            self._repeek()
            if self._peek[0] == 0x3D:
                self._r.discard(1)
                return _DASH_MATCH_TOKEN
            elif self._peek[0] == 0x7C:
                self._r.discard(1)
                return _COLUMN_TOKEN
        elif ch == 0x2B:  # '+'
            self._unread_byte()
            self._repeek()
            if self._next_is_number():
                return self._consume_numeric()
            self._next_byte()  # re-read, fall down to DELIM
        elif ch == 0x2D:  # '-'
            self._unread_byte()
            self._repeek()
            if self._next_is_number():
                return self._consume_numeric()
            if self._next_starts_identifier():
                return self._consume_identish()
            if self._next_compare(b"-->"):
                self._r.discard(3)
                return _CDC_TOKEN
            self._next_byte()  # re-read, fall down to DELIM
        elif ch == 0x2E:  # '.'
            self._unread_byte()
            self._repeek()
            if self._next_is_number():
                return self._consume_numeric()
            self._next_byte()  # re-read, fall down to DELIM
        elif ch == 0x2F:  # '/'
            self._repeek()
            if self._peek[0] == 0x2A:  # '*'
                self._r.discard(1)
                return self._consume_comment()
        elif ch == 0x3C:  # '<'
            self._repeek()
            if self._next_compare(b"!--"):
                self._r.discard(3)
                return _CDO_TOKEN
        elif ch == 0x40:  # '@'
            self._repeek()
            if self._next_starts_identifier():
                return Token(TokenType.AT_KEYWORD, _str(self._consume_name()))
        elif ch == 0x5C:  # '\\'
            self._unread_byte()
            self._repeek()
            if self._next_is_escape():
                # input stream has a valid escape
                return self._consume_identish()
            self._next_byte()
            return _BAD_ESCAPE_TOKEN
        elif ch == 0x55 or ch == 0x75:  # 'U' 'u'
            self._unread_byte()
            self._repeek()
            p = self._peek
            if p[1] == 0x2B and (_is_hex_digit(p[2]) or p[2] == 0x3F):
                self._r.discard(2)  # (!) only discard the U+
                return self._consume_unicode_range()

        if 0x30 <= ch <= 0x39:
            self._unread_byte()
            return self._consume_numeric()
        if _is_name_start(ch):
            # For 'U'/'u' this second unread fails silently (bufio lastByte is
            # -1 after the Peek), exactly as in Go; the first one already
            # pushed the byte back.
            self._unread_byte()
            return self._consume_identish()
        return Token(TokenType.DELIM, chr(ch))

    def _consume_whitespace(self, ch: int) -> Token:
        r = self._r
        data = r.data
        # Go peeks in chunks of 32 bytes and discards the leading whitespace;
        # Peek/Discard both reset bufio's lastByte.
        r.last_byte = -1
        start = pos = r.pos
        n = len(data)
        while pos < n and data[pos] in _WS:
            pos += 1
        r.pos = pos
        saw_newline = ch == 0x0A
        if ch != 0 and data.find(b"\n", start, pos) != -1:
            saw_newline = True
        return Token(TokenType.S, "\n" if saw_newline else " ")

    # §4.3.2
    def _consume_numeric(self) -> Token:
        repr_, not_integer = self._consume_numeric_inner()
        ttype = TokenType.NUMBER
        dimension = ""
        self._repeek()
        if self._next_starts_identifier():
            ttype = TokenType.DIMENSION
            dimension = _str(self._consume_name())
        elif self._peek[0] == 0x25:  # '%'
            self._r.discard(1)
            ttype = TokenType.PERCENTAGE
        return Token(ttype, _str(repr_), TokenExtraNumeric(not_integer, dimension))

    # §4.3.3
    def _consume_identish(self) -> Token:
        s = self._consume_name()
        self._repeek()
        if self._peek[0] == 0x28:  # '('
            self._r.discard(1)
            # strings.EqualFold(s, "url"): no non-ASCII rune case-folds to
            # u, r or l, so an ASCII-only comparison is equivalent.
            if s.lower() == b"url":
                return self._consume_url()
            return Token(TokenType.FUNCTION, _str(s))
        return Token(TokenType.IDENT, _str(s))

    # §4.3.4
    def _consume_string(self, delim: int) -> Token:
        bad, frag = self._consume_string_raw(delim)
        if bad:
            er = ParseError(TokenType.BAD_STRING, "unterminated string")
            return Token(TokenType.BAD_STRING, _str(frag), TokenExtraError(er))
        return Token(TokenType.STRING, _str(frag))

    def _consume_string_raw(self, delim: int) -> tuple[bool, bytearray]:
        """Returns (is_bad_string, value bytes)."""
        frag = bytearray()
        while True:
            by = self._next_byte()
            if by == delim or by == 0:
                # end of string, EOF
                return False, frag
            elif by == 0x0A:
                self._unread_byte()
                return True, frag
            elif by == 0x5C:
                self._unread_byte()
                self._repeek()
                if self._peek[1] == 0:
                    # escape @ EOF, ignore.
                    self._next_byte()  # '\'
                elif self._peek[1] == 0x0A:
                    # valid escaped newline, ignore.
                    self._next_byte()  # '\'
                    self._next_byte()  # newline
                else:
                    # stream will always contain a valid escape here
                    self._next_byte()  # '\'
                    frag += _encode_rune(self._consume_escaped_cp())
            else:
                frag.append(by)

    # §4.3.5, reader must be in the "url(" state
    def _consume_url(self) -> Token:
        self._consume_whitespace(0)
        self._repeek()
        p0 = self._peek[0]
        if p0 == 0:
            return Token(TokenType.URI, "")
        elif p0 == 0x27 or p0 == 0x22:
            delim = p0
            self._next_byte()
            bad, value = self._consume_string_raw(delim)
            if bad:
                value += self._consume_bad_url()
                pe = ParseError(TokenType.BAD_URI, "unterminated string in url()")
                return Token(TokenType.BAD_URI, _str(value), TokenExtraError(pe))
            self._consume_whitespace(0)
            self._repeek()
            if self._peek[0] == 0x29 or self._peek[0] == 0:
                self._next_byte()
                return Token(TokenType.URI, _str(value))
            value += self._consume_bad_url()
            pe = ParseError(TokenType.BAD_URI, "url() with string missing close parenthesis")
            return Token(TokenType.BAD_URI, _str(value), TokenExtraError(pe))

        frag = bytearray()
        while True:
            by = self._next_byte()
            if by == 0x29 or by == 0:
                return Token(TokenType.URI, _str(frag))
            elif by in _WS:
                self._consume_whitespace(0)
                self._repeek()
                if self._peek[0] == 0x29 or self._peek[0] == 0:
                    self._next_byte()  # ')'
                    return Token(TokenType.URI, _str(frag))
                pe = ParseError(TokenType.BAD_URI, "bare url() with internal whitespace")
                return Token(TokenType.BAD_URI, _str(frag + self._consume_bad_url()), TokenExtraError(pe))
            elif by == 0x27 or by == 0x22 or by == 0x28:
                pe = ParseError(TokenType.BAD_URI, "bare url() with illegal character '%c'" % by)
                return Token(TokenType.BAD_URI, _str(frag + self._consume_bad_url()), TokenExtraError(pe))
            elif _is_non_printable(by):
                pe = ParseError(TokenType.BAD_URI, "bare url() with unprintable character '%d'" % by)
                return Token(TokenType.BAD_URI, _str(frag + self._consume_bad_url()), TokenExtraError(pe))
            elif by == 0x5C:
                self._unread_byte()
                self._repeek()
                if self._next_is_escape():
                    self._next_byte()  # '\'
                    frag += _encode_rune(self._consume_escaped_cp())
                else:
                    pe = ParseError(TokenType.BAD_URI, "bare url() with invalid escape")
                    return Token(
                        TokenType.BAD_URI, _str(frag + self._consume_bad_url()), TokenExtraError(pe)
                    )
            else:
                frag.append(by)

    # §4.3.6
    def _consume_unicode_range(self) -> Token:
        sdigits = [0] * 6
        have_question_marks = False
        i = 0
        while True:
            by = self._next_byte()
            if i >= 6:
                break  # weird condition so that unreadByte() works
            if by == 0x3F:  # '?'
                sdigits[i] = by
                have_question_marks = True
                i += 1
            elif not have_question_marks and _is_hex_digit(by):
                sdigits[i] = by
                i += 1
            else:
                break
        self._unread_byte()
        slen = i
        edigits = [0] * 6
        self._repeek()

        if have_question_marks:
            edigits[:slen] = sdigits[:slen]
            elen = slen
            for idx in range(6):
                if sdigits[idx] == 0x3F:
                    sdigits[idx] = 0x30  # '0'
                    edigits[idx] = 0x46  # 'F'
        elif self._peek[0] == 0x2D and _is_hex_digit(self._peek[1]):
            self._next_byte()  # '-'
            i = 0
            while True:
                by = self._next_byte()
                if i < 6 and _is_hex_digit(by):
                    edigits[i] = by
                    i += 1
                else:
                    break
            self._unread_byte()
            elen = i
        else:
            edigits[:] = sdigits
            elen = slen

        start_cp = int(bytes(sdigits[:slen]), 16)
        end_cp = int(bytes(edigits[:elen]), 16)
        e = TokenExtraUnicodeRange(start_cp, end_cp)
        return Token(TokenType.UNICODE_RANGE, str(e), e)

    def _consume_comment(self) -> Token:
        frag = bytearray()
        while True:
            by = self._next_byte()
            if by == 0x2A:  # '*'
                self._repeek()
                if self._peek[0] == 0x2F:  # '/'
                    self._next_byte()  # '/'
                    return Token(TokenType.COMMENT, _str(frag))
            elif by == 0:
                return Token(TokenType.COMMENT, _str(frag))
            frag.append(by)

    # §4.3.7, after the "\"
    def _consume_escaped_cp(self) -> int:
        by = self._next_byte()
        if by == 0:
            return _RUNE_ERROR
        elif _is_hex_digit(by):
            digits = bytearray((by,))
            i = 1
            # (!) weird looping condition so that we UnreadByte() at the end
            while True:
                by = self._next_byte()
                if i < 6 and _is_hex_digit(by):
                    digits.append(by)
                    i += 1
                else:
                    break
            if by not in _WS and by != 0:
                self._unread_byte()
            cpi = int(bytes(digits), 16)
            if cpi == 0 or cpi > _MAX_RUNE:
                return _RUNE_ERROR
            return cpi
        else:
            self._unread_byte()
            ru, size = self._r.read_rune()
            if size == 0:  # pragma: no cover - a byte was just unread
                self._err = EOF
                return _RUNE_ERROR
            return ru

    # §4.3.11
    def _consume_name(self) -> bytes:
        frag = bytearray()
        while True:
            by = self._next_byte()
            if by == 0x5C:
                self._unread_byte()
                self._repeek()
                if self._next_is_escape():
                    self._next_byte()
                    frag += _encode_rune(self._consume_escaped_cp())
                    continue
                return bytes(frag)
            elif _is_name_code(by):
                frag.append(by)
                continue
            else:
                self._unread_byte()
                return bytes(frag)

    def _consume_digits(self, repr_: bytearray, by: int) -> int:
        while 0x30 <= by <= 0x39:
            repr_.append(by)
            by = self._next_byte()
        if by != 0:
            # don't attempt to unread EOF
            self._unread_byte()
        return by

    # §4.3.12
    def _consume_numeric_inner(self) -> tuple[bytearray, bool]:
        repr_ = bytearray()
        not_integer = False
        by = self._next_byte()
        if by == 0x2B or by == 0x2D:
            repr_.append(by)
            by = self._next_byte()

        by = self._consume_digits(repr_, by)
        self._repeek()
        p = self._peek
        if p[0] == 0x2E and 0x30 <= p[1] <= 0x39:
            not_integer = True
            by = self._next_byte()  # '.'
            repr_.append(by)
            by = self._next_byte()
            by = self._consume_digits(repr_, by)
            self._repeek()
        # [eE][+-]?[0-9]
        if p[0] == 0x65 or p[0] == 0x45:
            n = 0
            if (p[1] == 0x2B or p[1] == 0x2D) and 0x30 <= p[2] <= 0x39:
                n = 3
            elif 0x30 <= p[1] <= 0x39:
                n = 2
            if n != 0:
                not_integer = True
                repr_ += bytes(p[:n])
                self._r.discard(n)
                by = self._next_byte()
                by = self._consume_digits(repr_, by)
        return repr_, not_integer

    # §4.3.14
    def _consume_bad_url(self) -> bytes:
        frag = bytearray()
        while True:
            by = self._next_byte()
            if by == 0x29 or by == 0:
                return bytes(frag)
            elif by == 0x5C:
                self._unread_byte()
                self._repeek()
                if self._next_is_escape():
                    self._next_byte()  # '\'
                    # Allow for escaped right paren "\)"
                    frag += _encode_rune(self._consume_escaped_cp())
                    continue
                self._next_byte()  # '\'
            frag.append(by)


def is_css(s: InputType) -> bool:
    """Port of kobopatch ``patchlib.IsCSS``.

    Uses a heuristic to check if the input is likely to be CSS. There may be
    false negatives for invalid CSS or for CSS documents with more rules than
    properties, and false positives for CSS-like languages. (The Go version
    also returns an error if the io.Reader fails; byte input cannot fail.)
    """
    tk = Tokenizer(s)
    seen_non_whitespace = False
    open_brace_count = close_brace_count = prop_count = 0
    prev = cur = _ZERO_TOKEN
    T = TokenType
    while True:
        t = tk.next()
        err = tk.err()
        if err is not None:
            if err is EOF:
                break
            raise err  # pragma: no cover
        prev, cur = cur, t
        if not seen_non_whitespace:
            if cur.type != T.S:
                if (
                    cur.type != T.IDENT
                    and cur.type != T.COLON
                    and cur.type != T.AT_KEYWORD
                    and not (cur.type == T.DELIM and (cur.value == "*" or cur.value == "."))
                    and cur.type != T.HASH
                    and cur.type != T.OPEN_BRACKET
                ):
                    return False  # doesn't start with an identifier (for a selector)
                seen_non_whitespace = True
            continue
        ct = cur.type
        if ct == T.OPEN_BRACE:
            open_brace_count += 1
        elif ct == T.CLOSE_BRACE:
            close_brace_count += 1
        elif ct == T.COLON:
            if prev.type == T.IDENT and open_brace_count > close_brace_count:
                # is likely a property if it has an identifier before and is
                # inside an unclosed block (i.e. not a selector)
                prop_count += 1
    if open_brace_count != close_brace_count:
        return False  # different number of open/close braces
    if open_brace_count == 0:
        return False  # no rules
    if open_brace_count > prop_count:
        return False  # more blocks than properties
    return True
