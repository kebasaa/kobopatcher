"""The pure-Python deflate (_zlibdeflate) against classic zlib.

The expected outputs come from three places:

* tests/testdata/deflate_cases.json.gz: the SHA256 of classic zlib's
  compress2(..., Z_DEFAULT_COMPRESSION) output for ~200 generated inputs
  (regenerated from their names by `case_input`), so these tests also run on
  Pythons whose zlib module is zlib-ng;
* the zlib streams in tests/testdata/nickel, whose content recompresses to
  exactly the bytes in the binary;
* Python's zlib module, when it is classic zlib (tests skipped otherwise).

Regenerate the fixture (with a classic zlib) with
``python tests/test_zlibdeflate.py --write-fixture``.
"""

import gzip
import hashlib
import json
import os
import pathlib
import sys
import zlib

import pytest

from kobopatcher.patchlib import _zlibdeflate, zlibcss
from kobopatcher.patchlib._zlibdeflate import compress

TESTDATA = pathlib.Path(__file__).parent / "testdata"
FIXTURE = TESTDATA / "deflate_cases.json.gz"


def native_is_classic():
    return not getattr(zlib, "ZLIBNG_VERSION", None) and zlibcss._native_selftest() is None


needs_classic = pytest.mark.skipif(not native_is_classic(), reason="Python's zlib module is not classic zlib")


# --- deterministic inputs (independent of the random module) ---------------


class Rng:
    """splitmix64"""

    def __init__(self, seed):
        self.s = (seed * 0x9E3779B97F4A7C15 + 0x632BE59BD9B4E019) & 0xFFFFFFFFFFFFFFFF

    def next(self):
        self.s = (self.s + 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
        z = self.s
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
        return z ^ (z >> 31)

    def below(self, n):
        return self.next() % n

    def range(self, a, b):
        return a + self.below(b - a)

    def choice(self, seq):
        return seq[self.below(len(seq))]

    def chance(self, p):
        return self.next() < p * 2**64

    def bytes(self, n):
        return random_bytes(self.next(), n)


def random_bytes(seed, n):
    out = bytearray()
    i = 0
    key = b"%d:" % seed
    while len(out) < n:
        out += hashlib.sha256(key + b"%d" % i).digest()
        i += 1
    return bytes(out[:n])


def no_repeat(n, seed=0):
    """Bytes in which (almost) no 3-byte string repeats: only literals."""
    out = bytearray()
    a = seed & 0xFF
    while len(out) < n:
        for b in range(256):
            out += bytes((a, b))
        a = (a + 1) & 0xFF
    return bytes(out[:n])


_PROPS = [
    "margin",
    "padding",
    "font-size",
    "color",
    "qproperty-visible",
    "border",
    "min-height",
    "background-color",
    "qproperty-alignment",
    "font-family",
]
_WIDGETS = ["QWidget", "QLabel", "N3ButtonLabel", "TouchLabel", "QScrollArea", "#boggleContainer", "*[qApp_deviceIsPika=true]"]


def synthetic_css(n, seed):
    r = Rng(seed)
    parts = []
    size = 0
    while size < n:
        sel = r.choice(_WIDGETS)
        if r.chance(0.5):
            sel += "#w%d" % r.below(300)
        if r.chance(0.3):
            sel += "[qApp_deviceIs%s=true]" % r.choice(["Pika", "Alyssa", "Phoenix", "Dragon", "Storm"])
        block = [sel + " {\n"]
        for _ in range(r.range(1, 7)):
            prop = r.choice(_PROPS)
            if r.chance(0.2):
                val = '"%s"' % r.choice(["Avenir Next", "Georgia", "Amasis"])
            elif r.chance(0.3):
                val = "#%06x" % r.below(1 << 24)
            elif r.chance(0.2):
                val = r.choice(["true", "false", "none"])
            else:
                val = "%d%s" % (r.below(200), r.choice(["px", "pt", "%", ""]))
            block.append("  %s: %s;\n" % (prop, val))
        block.append("}\n")
        s = "".join(block)
        parts.append(s)
        size += len(s)
    return "".join(parts).encode()[:n]


def text(n, seed, nwords=200):
    r = Rng(seed)
    words = [random_bytes(seed * 1000 + i, r.range(1, 12)).hex()[: r.range(1, 12)].encode() for i in range(nwords)]
    out = bytearray()
    while len(out) < n:
        out += r.choice(words)
        out += r.choice([b" ", b" ", b" ", b"\n", b", ", b". "])
    return bytes(out[:n])


def small_alphabet(n, seed, k):
    r = Rng(seed)
    return bytes((r.below(k) + 97) & 0xFF for _ in range(n))


def with_copies(n, seed, maxdist=40000, maxlen=300):
    """Random literals interleaved with copies from up to maxdist back."""
    r = Rng(seed)
    out = bytearray()
    while len(out) < n:
        if out and r.chance(0.7):
            dist = r.range(1, min(len(out), maxdist) + 1)
            for _ in range(r.range(1, maxlen)):
                out.append(out[-dist])
        else:
            out += r.bytes(r.range(1, 20))
    return bytes(out[:n])


def repeat_at(dist, n, seed, mlen=300):
    """A random block repeated at exactly `dist` bytes distance."""
    head = random_bytes(seed, mlen)
    gap = random_bytes(seed + 1, dist - mlen)
    return (head + gap + head + random_bytes(seed + 2, n))[: dist + mlen + n]


def case_input(name):
    """The input of the fixture case `name` (kind:args)."""
    kind, _, arg = name.partition(":")
    a = [int(x) for x in arg.split(",")] if arg else []
    if kind == "bytes":
        return bytes(a)
    if kind == "all":
        return bytes(range(256)) * a[0]
    if kind == "zeros":
        return bytes(a[0])
    if kind == "const":
        return bytes([a[1]]) * a[0]
    if kind == "period":  # n, period, seed
        unit = random_bytes(a[2], a[1])
        return (unit * (a[0] // a[1] + 1))[: a[0]]
    if kind == "random":
        return random_bytes(a[1], a[0])
    if kind == "norepeat":
        return no_repeat(a[0], a[1])
    if kind == "css":
        return synthetic_css(a[0], a[1])
    if kind == "text":
        return text(a[0], a[1], a[2])
    if kind == "alpha":
        return small_alphabet(a[0], a[1], a[2])
    if kind == "copies":
        return with_copies(a[0], a[1], a[2], a[3])
    if kind == "repeatat":
        return repeat_at(a[0], a[1], a[2])
    if kind == "mixed":  # n, seed: alternating random and text runs
        r = Rng(a[1])
        out = bytearray()
        while len(out) < a[0]:
            m = r.range(1, 40000)
            out += r.bytes(m) if r.chance(0.5) else synthetic_css(m, r.below(1000))
        return bytes(out[: a[0]])
    if kind == "lazy":  # inputs where lazy evaluation matters: abcd...bcde...
        r = Rng(a[1])
        alpha = b"abcdefghij"
        out = bytearray()
        while len(out) < a[0]:
            i = r.below(len(alpha) - 4)
            out += alpha[i : i + r.range(3, 6)]
        return bytes(out[: a[0]])
    raise ValueError(name)


def case_names():
    names = []
    add = names.append
    add("bytes:")
    for v in (0, 1, 97, 255):
        add("bytes:%d" % v)
    add("bytes:0,0")
    add("bytes:0,0,0")
    add("bytes:97,98,99")
    add("bytes:97,97,97,97")
    add("bytes:1,2,1,2,1,2,1")
    add("all:1")
    add("all:3")
    add("all:200")
    for n in (3, 4, 5, 257, 258, 259, 260, 261, 262, 263, 515, 516, 517, 1000, 4096, 32767, 32768, 32769, 65536, 100000):
        add("zeros:%d" % n)
    add("const:300000,120")
    for n in (2, 3, 7, 100, 4097, 70000):
        add("period:%d,2,%d" % (n, n))
    for p in (3, 5, 31, 257, 258, 259, 1000, 32506, 32507, 32768):
        add("period:%d,%d,%d" % (max(3 * p, 70000), p, p))
    for n in (1, 2, 100, 1000, 16383, 16384, 16385, 32767, 32768, 32769, 65535, 65536, 65537, 100000):
        add("random:%d,%d" % (n, n))
    for n in (16381, 16382, 16383, 16384, 16385, 16386, 32766, 32767, 32768, 49149, 49150, 65536, 70000):
        add("norepeat:%d,%d" % (n, n & 7))
    for d in (4095, 4096, 4097, 32505, 32506, 32507, 32768, 32769):
        add("repeatat:%d,1000,%d" % (d, d))
    for i, n in enumerate((10, 100, 500, 1000, 3000, 8000, 16000, 30000, 40000, 60000, 65274, 65275, 70000, 85000, 130000)):
        add("css:%d,%d" % (n, i))
    add("css:1000000,99")
    for i, n in enumerate((50, 700, 5000, 20000, 66000, 70000)):
        add("text:%d,%d,%d" % (n, i, (5, 50, 400)[i % 3]))
    for i, n in enumerate((20, 300, 2000, 20000, 40000)):
        for k in (1, 2, 4, 16):
            add("alpha:%d,%d,%d" % (n, i * 10 + k, k))
    for i, n in enumerate((500, 5000, 33000, 66000, 100000, 150000)):
        add("copies:%d,%d,40000,300" % (n, i))
        add("copies:%d,%d,300,20" % (n, i + 100))
        add("copies:%d,%d,32768,260" % (n, i + 200))
    for i, n in enumerate((80000, 120000)):
        add("mixed:%d,%d" % (n, i))
    for i, n in enumerate((100, 5000, 50000)):
        add("lazy:%d,%d" % (n, i))
    # seeded variety of everything at assorted sizes
    r = Rng(12345)
    while len(names) < 210:
        n = r.choice([r.range(1, 64), r.range(64, 3000), r.range(3000, 70000)])
        k = r.below(5)
        s = r.below(10**6)
        add(
            ["random:%d,%d" % (n, s), "css:%d,%d" % (n, s), "text:%d,%d,%d" % (n, s, r.range(2, 500)),
             "alpha:%d,%d,%d" % (n, s, r.range(1, 256)), "copies:%d,%d,%d,%d" % (n, s, r.range(1, 40000), r.range(2, 300))][k]
        )
    return names


def load_fixture():
    with gzip.open(FIXTURE, "rt") as f:
        return json.load(f)


def write_fixture():
    assert native_is_classic(), "needs Python linked against classic zlib"
    cases = []
    for name in case_names():
        data = case_input(name)
        cases.append(
            {
                "name": name,
                "len": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "zlib_sha256": hashlib.sha256(zlib.compress(data, 6)).hexdigest(),
            }
        )
    with open(FIXTURE, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as f:
        f.write(json.dumps({"zlib": zlib.ZLIB_VERSION, "cases": cases}, indent=0).encode())
    print("wrote %d cases to %s" % (len(cases), FIXTURE))


# --- tests --------------------------------------------------------------------


def test_fixture_cases():
    """Output equals classic zlib's (as recorded in the fixture) for every case."""
    fx = load_fixture()
    assert len(fx["cases"]) >= 200
    bad = []
    for c in fx["cases"]:
        data = case_input(c["name"])
        assert len(data) == c["len"] and hashlib.sha256(data).hexdigest() == c["sha256"], "input of %s changed" % c["name"]
        out = compress(data)
        if hashlib.sha256(out).hexdigest() != c["zlib_sha256"]:
            bad.append(c["name"])
        assert zlib.decompress(out) == data
    assert not bad


@needs_classic
def test_fixture_is_classic_zlib():
    """The fixture records what this (classic) zlib produces."""
    for c in load_fixture()["cases"]:
        data = case_input(c["name"])
        assert hashlib.sha256(zlib.compress(data, 6)).hexdigest() == c["zlib_sha256"], c["name"]


@needs_classic
@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"x",
        bytes(range(256)),
        b"\0" * 258 * 3,
        b"ab" * 40000,
        random_bytes(7, 70000),
        no_repeat(16384),
        repeat_at(32506, 500, 3),
        repeat_at(32507, 500, 3),
    ],
    ids=lambda d: "len%d" % len(d),
)
def test_against_native(data):
    assert compress(data) == zlib.compress(data, 6)


def nickel_streams(maxlen=None):
    buf = (TESTDATA / "nickel").read_bytes()
    mv = memoryview(buf)
    out = []
    i = buf.find(b"\x78\x9c")
    while i >= 0:
        d = zlib.decompressobj()
        chunk = mv[i : i + (1 << 21)]
        try:
            data = d.decompress(chunk)
        except zlib.error:
            data = None
        if data is not None and d.eof and (maxlen is None or len(data) <= maxlen):
            end = i + len(chunk) - len(d.unused_data)
            out.append((i, data, bytes(mv[i:end])))
        i = buf.find(b"\x78\x9c", i + 1)
    return out


def test_nickel_streams():
    """Every zlib stream in nickel (up to 100 KB; all with KOBOPATCHER_SLOW_TESTS=1)
    recompresses to the bytes in the binary."""
    maxlen = None if os.environ.get("KOBOPATCHER_SLOW_TESTS") else 100_000
    streams = nickel_streams(maxlen)
    assert len(streams) >= 100
    bad = [off for off, data, raw in streams if compress(data) != raw]
    assert not bad


def test_zlibcss_backends(monkeypatch):
    data = synthetic_css(20000, 1)
    expect = _zlibdeflate.compress(data)
    monkeypatch.setenv("KOBOPATCHER_PURE_DEFLATE", "1")
    assert zlibcss.compressor_name().startswith("pure-Python")
    assert zlibcss.zlib_selftest() is None
    assert zlibcss.compress(data) == expect
    monkeypatch.delenv("KOBOPATCHER_PURE_DEFLATE")
    # a zlib module that compresses differently (zlib-ng) -> fallback
    monkeypatch.setattr(zlibcss, "_native_result", "zlib-ng, simulated")
    assert zlibcss.compressor_name().startswith("pure-Python")
    assert "zlib-ng, simulated" in zlibcss.compressor_name()
    assert zlibcss.zlib_selftest() is None
    assert zlibcss.compress(data) == expect
    # neither works -> the self-test fails with both reasons
    monkeypatch.setattr(zlibcss, "_pure_result", "pure broken, simulated")
    msg = zlibcss.zlib_selftest()
    assert msg and "zlib-ng, simulated" in msg and "pure broken, simulated" in msg


@needs_classic
def test_zlibcss_native():
    assert zlibcss.compress(b"abc") == zlib.compress(b"abc")
    if not os.environ.get("KOBOPATCHER_PURE_DEFLATE"):
        assert zlibcss.compressor_name().startswith("zlib ")


if __name__ == "__main__":
    if "--write-fixture" in sys.argv:
        write_fixture()
