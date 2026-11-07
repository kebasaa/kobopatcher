"""zlib helpers for the CSS streams embedded in Kobo binaries.

Two properties matter:

* Recompression must reproduce classic zlib's compress2() at the default
  level byte for byte, because kobopatcher checks that recompressing a
  stream's content gives back exactly the bytes in the binary (kobopatch used
  go-libz, i.e. zlib 1.2.8). Python's zlib module is used when it passes
  `zlib_selftest()`'s checks; when it does not (Python linked against zlib-ng,
  e.g. the official Windows builds of Python 3.14, compresses differently),
  `compress()` falls back to `_zlibdeflate`, a pure-Python port of classic
  zlib's deflate. Setting KOBOPATCHER_PURE_DEFLATE=1 forces the fallback (for
  tests and CI). `compressor_name()` says which one is in use.
* Decompression must be as lenient as Go's compress/zlib, which kobopatch
  used: data after the end of a stream is ignored, and output decoded before
  a corrupt block or a bad checksum is kept. A stream that is cut off is an
  error. Inflate output does not depend on the zlib implementation, so this
  always uses Python's zlib module.
"""

import hashlib
import os
import zlib

from . import _zlibdeflate

# Expected output of classic zlib (1.2.x and 1.3.x) at the default level for
# a few inputs (an empty block, a static and a dynamic Huffman block);
# compress2() and Python's zlib.compress() agree. Each entry is
# (input, hex of the output or None, sha256 of the output or None).
_SELFTEST = [
    (b"", "789c030000000001", None),
    (b"a", "789c4b040000620062", None),
    (
        b"body { margin: 0; padding: 0; }\nQWidget { font-size: 12pt; }\n" * 8,
        "789c4bca4fa954a856c84d2c4acfccb35230b05628484c49c9cc4b07b36bb902c33353d2534b804ad2f2f34a748b33ab"
        "52ad140c8d0a4a409249a39a878a66007dfe9d09",
        None,
    ),
    (
        bytes((i * i * 7 + i // 3) % 61 + 32 for i in range(3000)),
        None,
        "aadbf6daa4577d2b08b25158d262c52d9813543637bb42c84e66354a1e553bec",
    ),
]

_native_result = None
_pure_result = None


def _check(name, fn):
    """None if fn compresses the self-test inputs like classic zlib, else a message."""
    for data, exp_hex, exp_sha in _SELFTEST:
        try:
            got = fn(data)
        except Exception as e:  # pragma: no cover - defensive
            return "%s fails: %s" % (name, e)
        if exp_hex is not None and got.hex() != exp_hex:
            return "%s compresses %r to %s, expected %s" % (name, data[:16], got.hex(), exp_hex)
        if exp_sha is not None and hashlib.sha256(got).hexdigest() != exp_sha:
            return "%s compresses %r... differently from classic zlib" % (name, data[:16])
        if zlib.decompress(got) != data:
            return "%s does not round-trip" % name
    return None


def _native_selftest():
    """None if Python's zlib module compresses like classic zlib, else a message."""
    global _native_result
    if _native_result is None:
        ng = getattr(zlib, "ZLIBNG_VERSION", None)
        if ng:
            msg = "Python's zlib module uses zlib-ng %s, which compresses differently from classic zlib" % ng
        else:
            msg = _check("zlib %s" % zlib.ZLIB_VERSION, zlib.compress)
        _native_result = msg or ""
    return _native_result or None


def _pure_selftest():
    """None if the pure-Python deflate compresses like classic zlib, else a message."""
    global _pure_result
    if _pure_result is None:
        _pure_result = _check("the pure-Python deflate", _zlibdeflate.compress) or ""
    return _pure_result or None


def _pure_forced():
    return os.environ.get("KOBOPATCHER_PURE_DEFLATE", "").strip() not in ("", "0")


def _use_native():
    return not _pure_forced() and _native_selftest() is None


def zlib_selftest():
    """Return None if compress() matches classic zlib, else a message.

    compress() uses Python's zlib module when it compresses like classic
    zlib, otherwise the pure-Python port, so this only fails if neither does.
    """
    if _use_native():
        return None
    msg = _pure_selftest()
    if msg is None:
        return None
    native = "pure-Python deflate forced by KOBOPATCHER_PURE_DEFLATE" if _pure_forced() else _native_selftest()
    return "%s; %s" % (native, msg)


def compressor_name() -> str:
    """Which implementation compress() uses, for messages and logs."""
    if _use_native():
        return "zlib %s (Python's zlib module)" % zlib.ZLIB_VERSION
    if _pure_forced():
        why = "KOBOPATCHER_PURE_DEFLATE is set"
    else:
        why = _native_selftest()
    return "pure-Python deflate (port of zlib 1.3.1; %s)" % why


def compress(src: bytes) -> bytes:
    """compress2(src, Z_DEFAULT_COMPRESSION) of classic zlib."""
    if _use_native():
        return zlib.compress(bytes(src))
    return _zlibdeflate.compress(bytes(src))


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
