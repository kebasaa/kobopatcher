"""Go-style formatting helpers.

kobopatch's error messages use Go's %#v and %q verbs. kobopatcher keeps the
messages identical, so these helpers reproduce Go's quoting rules.
"""

import unicodedata

_NAMED = {
    0x07: "\\a",
    0x08: "\\b",
    0x0C: "\\f",
    0x0A: "\\n",
    0x0D: "\\r",
    0x09: "\\t",
    0x0B: "\\v",
    0x5C: "\\\\",
    0x22: '\\"',
}


def _is_print(ch: str) -> bool:
    # unicode.IsPrint: letters, marks, numbers, punctuation, symbols and the
    # ASCII space.
    if ch == " ":
        return True
    cat = unicodedata.category(ch)
    return cat[0] not in "CZ"


def quote(s) -> str:
    """strconv.Quote (also what %#v and %q print for a string).

    Accepts str (treated as its UTF-8 encoding) or bytes (which may contain
    invalid UTF-8, printed as \\xNN like Go).
    """
    b = s.encode("utf-8", "surrogateescape") if isinstance(s, str) else bytes(s)
    out = ['"']
    i = 0
    n = len(b)
    while i < n:
        c = b[i]
        if c < 0x80:
            if c in _NAMED:
                out.append(_NAMED[c])
            elif c < 0x20 or c == 0x7F:
                out.append("\\x%02x" % c)
            else:
                out.append(chr(c))
            i += 1
            continue
        # decode one UTF-8 sequence, Go style (invalid bytes become \xNN)
        if c >= 0xF0:
            ln = 4
        elif c >= 0xE0:
            ln = 3
        elif c >= 0xC0:
            ln = 2
        else:
            ln = 1
        try:
            ch = b[i : i + ln].decode("utf-8") if ln > 1 else None
        except UnicodeDecodeError:
            ch = None
        if ch is None or len(ch) != 1:
            out.append("\\x%02x" % c)
            i += 1
            continue
        cp = ord(ch)
        if _is_print(ch):
            out.append(ch)
        elif cp < 0x10000:
            out.append("\\u%04x" % cp)
        else:
            out.append("\\U%08x" % cp)
        i += ln
    out.append('"')
    return "".join(out)


def hexbytes_spaced(b: bytes) -> str:
    """fmt.Sprintf("% X", b)."""
    return " ".join("%02X" % x for x in b)
