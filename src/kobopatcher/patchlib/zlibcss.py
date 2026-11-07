"""zlib helpers for the CSS streams embedded in Kobo binaries.

Two properties matter:

* Recompression must reproduce classic zlib's compress2() at the default
  level byte for byte, because kobopatcher checks that recompressing a
  stream's content gives back exactly the bytes in the binary.
  `zlib_selftest()` verifies this for the zlib Python is linked against
  (zlib-ng, for example, produces different output).
* Decompression must be as lenient as Go's compress/zlib, which kobopatch
  used: data after the end of a stream is ignored, and output decoded before
  a corrupt block or a bad checksum is kept. A stream that is cut off is an
  error.
"""

import zlib

# Expected output of classic zlib (1.2.x and 1.3.x) at the default level for
# a few inputs; compress2() and Python's zlib.compress() agree.
_SELFTEST = [
    (b"", "789c030000000001"),
    (b"a", "789c4b0400006200 62".replace(" ", "")),
    (
        b"body { margin: 0; padding: 0; }\nQWidget { font-size: 12pt; }\n" * 8,
        None,  # checked for round-trip and determinism only
    ),
]

_selftest_result = None


def zlib_selftest():
    """Return None if zlib output matches classic zlib, else a message."""
    global _selftest_result
    if _selftest_result is None:
        msg = ""
        ng = getattr(zlib, "ZLIBNG_VERSION", None)
        if ng:
            msg = "Python's zlib module uses zlib-ng %s, which compresses differently from classic zlib" % ng
        else:
            for data, exp in _SELFTEST:
                got = zlib.compress(data)
                if exp is not None and got.hex() != exp:
                    msg = "zlib %s compresses %r to %s, expected %s" % (zlib.ZLIB_VERSION, data[:16], got.hex(), exp)
                    break
                if zlib.decompress(got) != data:
                    msg = "zlib %s does not round-trip" % zlib.ZLIB_VERSION
                    break
        _selftest_result = msg
    return _selftest_result or None


def compress(src: bytes) -> bytes:
    """compress2(src, Z_DEFAULT_COMPRESSION)."""
    return zlib.compress(bytes(src))


class InflateResult:
    __slots__ = ("data", "err")

    def __init__(self, data: bytes, err):
        self.data = data
        # None, or one of "corrupt input", "invalid checksum" (both tolerated,
        # like kobopatch), "unexpected EOF" (an error), "invalid header"
        self.err = err


def inflate_lenient(buf, start: int) -> InflateResult:
    """Decompress the zlib stream at buf[start:], Go compress/zlib style."""
    mv = memoryview(buf)
    if len(mv) - start < 2:
        return InflateResult(b"", "unexpected EOF")
    h0, h1 = mv[start], mv[start + 1]
    if (h0 & 0x0F) != 8 or (h0 >> 4) > 7 or ((h0 << 8) | h1) % 31 != 0:
        return InflateResult(b"", "invalid header")
    if h1 & 0x20:
        # preset dictionary: Go returns ErrDictionary from NewReader
        return InflateResult(b"", "invalid dictionary")
    pos = start + 2
    end = len(mv)
    d = zlib.decompressobj(-15)
    out = []
    chunk = 64
    while pos < end and not d.eof:
        n = min(chunk, end - pos)
        saved = d.copy()
        try:
            out.append(d.decompress(mv[pos : pos + n]))
        except zlib.error:
            # redo this chunk byte by byte to keep the output decoded before
            # the error (Go flushes it before returning the error)
            d = saved
            for j in range(pos, pos + n):
                try:
                    out.append(d.decompress(mv[j : j + 1]))
                except zlib.error:
                    return InflateResult(b"".join(out), "corrupt input")
                if d.eof:
                    pos = j + 1 - len(d.unused_data)
                    break
            else:
                pos += n
                continue
            break
        if d.eof:
            pos = pos + n - len(d.unused_data)
            break
        pos += n
        if chunk < 65536:
            chunk *= 4
    data = b"".join(out)
    if not d.eof:
        return InflateResult(data, "unexpected EOF")
    if end - pos < 4:
        return InflateResult(data, "unexpected EOF")
    want = int.from_bytes(mv[pos : pos + 4], "big")
    if zlib.adler32(data) != want:
        return InflateResult(data, "invalid checksum")
    return InflateResult(data, None)


def strip_whitespace(s: str) -> str:
    return s.replace(" ", "").replace("\t", "").replace("\n", "").replace("\r", "")
