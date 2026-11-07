#!/usr/bin/env python3
"""Differential test of kobopatcher.patchlib._zlibdeflate against classic zlib.

The pure-Python deflate must produce exactly the bytes of classic zlib's
compress2(..., Z_DEFAULT_COMPRESSION), i.e. Python's zlib.compress(data, 6)
when Python is linked against classic zlib (not zlib-ng; checked at start).

Inputs:
  * the content of every zlib stream (78 9c, decoding completely) in the given
    ELF binaries (default: tests/testdata/nickel and the nickel binaries of the
    extracted firmware under ~/Documents/Github/kobo_aura_h2o/archive; they
    are only read, never executed). For these the recompressed stream is also
    compared with the bytes in the binary;
  * the cases of tests/testdata/deflate_cases.json.gz;
  * --count N seeded random and structured inputs (random bytes, small
    alphabets, text, synthetic CSS, copies at random distances, periodic
    data, sizes around the 16383-symbol and 32/64 KiB boundaries).

Usage:
  deflate_diff.py [--binary PATH ...] [--count N] [--seed S] [--no-fixture]

Exits 1 if any output differs.
"""

from __future__ import annotations

import argparse
import glob
import importlib.util
import os
import sys
import time
import zlib
from pathlib import Path

from kobopatcher.patchlib import zlibcss
from kobopatcher.patchlib._zlibdeflate import compress

REPO = Path(__file__).resolve().parents[2]
DEFAULT_BINARIES = [str(REPO / "tests/testdata/nickel")] + sorted(
    glob.glob(os.path.expanduser("~/Documents/Github/kobo_aura_h2o/archive/fw-*/x/rootfs/usr/local/Kobo/nickel"))
)


def _load_cases_module():
    """tests/test_zlibdeflate.py, for its deterministic input generators."""
    spec = importlib.util.spec_from_file_location("_deflate_cases", REPO / "tests/test_zlibdeflate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def streams(buf: bytes):
    """(offset, content, raw stream bytes) of each complete 78 9c zlib stream."""
    mv = memoryview(buf)
    i = buf.find(b"\x78\x9c")
    while i >= 0:
        d = zlib.decompressobj()
        chunk = mv[i : i + (1 << 22)]
        try:
            data = d.decompress(chunk)
        except zlib.error:
            data = None
        if data is not None and d.eof:
            end = i + len(chunk) - len(d.unused_data)
            yield i, data, bytes(mv[i:end])
        i = buf.find(b"\x78\x9c", i + 1)


def random_inputs(T, count, seed):
    r = T.Rng(seed)
    for k in range(count):
        kind = r.below(9)
        n = r.choice(
            [
                r.range(0, 40),
                r.range(40, 2000),
                r.range(2000, 40000),
                r.range(40000, 140000),
                16383 + r.range(-3, 4),
                32768 + r.range(-300, 300),
                65536 + r.range(-300, 300),
            ]
        )
        s = r.next()
        if kind == 0:
            data = T.random_bytes(s, n)
        elif kind == 1:
            data = T.small_alphabet(n, s % 10**9, r.range(1, 257))
        elif kind == 2:
            data = T.text(n, s % 10**9, r.range(2, 500))
        elif kind == 3:
            data = T.synthetic_css(n, s % 10**9)
        elif kind == 4:
            data = T.with_copies(n, s % 10**9, r.range(1, 40000), r.range(2, 400))
        elif kind == 5:
            p = r.range(1, 40000)
            unit = T.random_bytes(s, p)
            data = (unit * (n // p + 1))[:n]
        elif kind == 6:
            data = T.no_repeat(n, s & 0xFF)
        elif kind == 7:
            data = T.repeat_at(r.range(300, 40000), n, s % 10**9)
        else:
            # concatenation of different kinds
            parts = [T.random_bytes(s, n // 3), T.synthetic_css(n // 3, s % 1000), T.small_alphabet(n // 3, s % 1000, 3)]
            data = b"".join(parts[i] for i in (r.below(3), r.below(3), r.below(3)))
        yield "random#%d(kind %d, %d bytes)" % (k, kind, len(data)), data


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--binary", action="append", help="ELF binary to take zlib streams from (repeatable)")
    ap.add_argument("--count", type=int, default=300, help="number of random/structured inputs (default 300)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--no-fixture", action="store_true", help="skip tests/testdata/deflate_cases.json.gz")
    args = ap.parse_args()

    msg = zlibcss._native_selftest()
    if msg:
        sys.exit("needs Python linked against classic zlib: %s" % msg)
    print("oracle: zlib %s" % zlib.ZLIB_VERSION)
    T = _load_cases_module()

    total = {"inputs": 0, "bytes": 0, "mismatch": 0, "seconds": 0.0}

    def check(label, data, raw=None):
        """Compare with classic zlib; return the port's output."""
        t = time.process_time()
        got = compress(data)
        total["seconds"] += time.process_time() - t
        total["inputs"] += 1
        total["bytes"] += len(data)
        exp = zlib.compress(data, 6)
        ok = got == exp
        if raw is not None and exp != raw:
            print("  note: %s: classic zlib does not reproduce the stream in the binary" % label)
        if not ok:
            total["mismatch"] += 1
            pos = next((i for i in range(min(len(got), len(exp))) if got[i] != exp[i]), min(len(got), len(exp)))
            print("  MISMATCH %s: %d bytes, output %d vs %d bytes, first difference at %d" % (label, len(data), len(got), len(exp), pos))
        return got if ok else None

    for path in args.binary or DEFAULT_BINARIES:
        buf = Path(path).read_bytes()
        n = bad = same_as_binary = nbytes = 0
        for off, data, raw in streams(buf):
            n += 1
            nbytes += len(data)
            got = check("%s@%#x" % (path, off), data, raw)
            if got is None:
                bad += 1
            elif got == raw:
                same_as_binary += 1
        print("%s: %d streams (%d bytes), %d mismatches, %d recompress to the bytes in the binary" % (path, n, nbytes, bad, same_as_binary))

    if not args.no_fixture:
        fx = T.load_fixture()
        bad0 = total["mismatch"]
        for c in fx["cases"]:
            check(c["name"], T.case_input(c["name"]))
        print("fixture: %d cases, %d mismatches" % (len(fx["cases"]), total["mismatch"] - bad0))

    bad0 = total["mismatch"]
    for label, data in random_inputs(T, args.count, args.seed):
        check(label, data)
    print("random/structured: %d inputs (seed %d), %d mismatches" % (args.count, args.seed, total["mismatch"] - bad0))

    print(
        "total: %d inputs, %d bytes, %d mismatches; pure-Python deflate %.2f MB/s (CPU time)"
        % (total["inputs"], total["bytes"], total["mismatch"], total["bytes"] / max(total["seconds"], 1e-9) / 1e6)
    )
    sys.exit(1 if total["mismatch"] else 0)


if __name__ == "__main__":
    main()
