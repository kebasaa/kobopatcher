#!/usr/bin/env python3
"""Differential test of kobopatcher.patchfile.patch32lsb against Go kobopatch.

The Go side is a small helper built against kobopatch's patchfile/patch32lsb
(its source lives outside this repo). It is started as

    p32ref NAME=PATH ...

with the input buffers, reads JSON lines {"id", "patch" (base64), "bufs"} on
stdin and answers each with {"id", "out" (base64)}, where "out" is the
canonical description produced by ``describe`` below:

* the parse result (error text, or every patch with its decoded instructions);
* every result Validate can give (kobopatch validates in random map order, so
  the set of possible results is computed from single patches and pairs);
* SetEnabled on a missing patch;
* for each input buffer: each patch enabled on its own (all others disabled)
  and applied with ApplyTo to a fresh copy, and all patches enabled and
  applied one by one in name order to one copy. For each application: the
  result (ok / error text / panic text), the SHA1 of the result for small
  buffers, every change (the Patcher hook), the stdout and the debug log
  (without the lines of the skipped patches, whose order is random in Go).

The inputs are generated patch files: valid ones built from the instruction
grammar (using content of the input buffers, so most of them apply), real
world style ones, and mutations of them (missing equals signs, bad spacing,
bad escapes, unknown keys, odd hex, big and negative offsets, CRLF, nested or
unclosed blocks, duplicate names/groups/enables, tag and key case, Unicode
spaces, ...). The buffers are tests/testdata/libnickel.so.1.0.0 and small
synthetic buffers (with zlib CSS streams).

In go_compat mode (the default) the output must be identical; a difference
that goes away with the three Patcher fixes in proposed_patcher_fixes is
counted as "patcher.py", anything else as UNEXPECTED. In the default mode
(--fixed) each difference is classified by the smallest set of kobopatcher
fixes which, when reverted one by one, make it go away: the patch32lsb fixes 1,
2 and 4 (see kobopatcher.patchfile.patch32lsb) and patchlib.patcher's own
fixes. (Fix 3, the file order, cannot show up: the description is
order-independent.) The exit status is 1 if anything is UNEXPECTED.

Usage:
  p32_diff.py GO_HELPER [--count N] [--seed S] [--fixed] [--no-libnickel]
              [--write-fixture PATH] [--fixture-max N] [--show N]
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import io
import itertools
import json
import random
import re
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

from kobopatcher import patchfile
from kobopatcher.compat import go_compat_mode
from kobopatcher.errors import GoPanic, KobopatcherError
from kobopatcher.gofmt import quote
from kobopatcher.patchfile import patch32lsb as P
from kobopatcher.patchlib.patcher import Patcher
from kobopatcher.patchlib.zlibcss import compress

REPO = Path(__file__).resolve().parents[2]
LIBNICKEL = REPO / "tests/testdata/libnickel.so.1.0.0"
SMALL = 1 << 20


# --------------------------------------------------------------------------
# Buffers

CSS1 = (
    "QWidget {\n  font-size: 12px;\n  color: black;\n}\n"
    "#title { font-weight: bold; margin: 0 4px; }\n"
    "QLabel[selected=true] {\n    background-color: #ccc;\n    padding: 2px 3px;\n}\n"
)
CSS2 = "body { margin: 0; }\np.note { font-style: italic; text-indent: 1em; }\n"
STRINGS = [
    b"Hello World",
    b"font-size: 12px",
    b"abcabcabc",
    b"dicthtml%1/dicthtml%2",
    b"Kobo eReader",
    b"\xc3\xa9t\xc3\xa9",
    b"line\nbreak\ttab",
    b"quote\"back`slash\\",
    b"\x00\x01\x02\x03",
    b"ZZZZ",
]
FLOATS = [1.5, 2.0, -0.25, 1e10, 0.1]
NAN_BITS = 0x7FF8000000000001


def synth_buffers() -> dict[str, bytes]:
    """Small deterministic buffers (and what the generator knows about them)."""
    b = bytearray(b"SYNTH\x00")
    for s in STRINGS:
        b += s + b"\x00"
    b += bytes(range(256))
    for f in FLOATS:
        b += struct.pack("<d", f)
    b += struct.pack("<Q", NAN_BITS)
    b += bytes([0, 1, 2, 3, 0x7F, 0x80, 0xFE, 0xFF]) * 4
    b += b"\xee" * (1000 - len(b))
    z1 = compress(CSS1.encode())
    b += z1
    b += b"\x00" * 64
    z2 = compress(CSS2.encode())
    b += z2
    b += b"pad" * 40 + b"Hello World\x00end"
    tiny = b"0123456789ABCDEF"
    return {"synth": bytes(b), "tiny": tiny, "empty1": b"\x00"}


def load_buffers(libnickel: bool) -> dict[str, bytes]:
    bufs = synth_buffers()
    if libnickel and LIBNICKEL.exists():
        bufs["libnickel"] = LIBNICKEL.read_bytes()
    return bufs


# --------------------------------------------------------------------------
# Canonical description (must match the Go helper)


def _bkey(s: str) -> bytes:
    return s.encode("utf-8", "surrogateescape")


def _errs(e) -> str:
    return "ok" if e is None else "err " + quote(str(e))


def _sub(ps: P.PatchSet, names) -> P.PatchSet:
    s = P.PatchSet()
    s.patches = {n: ps.patches[n] for n in names}
    return s


def _sorted(ps: P.PatchSet):
    return sorted(ps.patches, key=_bkey)


def _validate(ps: P.PatchSet):
    try:
        ps.validate()
    except P.Patch32lsbError as e:
        return str(e)
    return None


def validate_set(ps: P.PatchSet) -> list[str]:
    errs = set()
    selfok = []
    for n in _sorted(ps):
        e = _validate(_sub(ps, [n]))
        if e is None:
            selfok.append(n)
        else:
            errs.add(e)
    for i, a in enumerate(selfok):
        for b in selfok[i + 1 :]:
            e = _validate(_sub(ps, [a, b]))
            if e is not None:
                errs.add(e)
    res = sorted((quote(e) for e in errs), key=_bkey)
    return res or ["ok"]


def _inst(i: P.Instruction) -> str:
    k, v = i.kind, i.value
    if k == "Enabled":
        return " Enabled %s\n" % ("true" if v else "false")
    if k in ("PatchGroup", "Comment", "FindBaseAddress", "FindZlib", "FindZlibHash"):
        return " %s %s\n" % (k, quote(v))
    if k == "BaseAddress":
        return " BaseAddress %d\n" % v
    if k == "ReplaceBytes":
        return " ReplaceBytes %d %s %s\n" % (v[0], v[1].hex(), v[2].hex())
    if k == "ReplaceFloat":
        bits = lambda f: "%016x" % struct.unpack("<Q", struct.pack("<d", f))[0]  # noqa: E731
        return " ReplaceFloat %d %s %s\n" % (v[0], bits(v[1]), bits(v[2]))
    if k == "ReplaceInt":
        return " ReplaceInt %d %d %d\n" % v
    if k in ("ReplaceString", "ReplaceZlib"):
        return " %s %d %s %s\n" % (k, v[0], quote(v[1]), quote(v[2]))
    if k == "FindReplaceString":
        return " FindReplaceString %s %s\n" % (quote(v[0]), quote(v[1]))
    return " ???\n"


def _apply(ps: P.PatchSet, buf: bytes, small: bool):
    logs = []
    hooks = []
    old_log = patchfile.log
    patchfile.set_log(logs.append)
    so = io.StringIO()
    pt = Patcher(bytearray(buf))
    pt.set_hook(lambda off, f, r: hooks.append("  hook %d %s %s\n" % (off, f.hex(), r.hex())))
    try:
        with contextlib.redirect_stdout(so):
            try:
                ps.apply_to(pt)
                res = "ok"
            except GoPanic as e:
                res = "panic " + quote(str(e))
            except KobopatcherError as e:
                res = _errs(e)
            except Exception as e:  # a kobopatcher bug: never matches Go
                res = "PYTHON EXCEPTION %r" % e
    finally:
        patchfile.set_log(old_log)
    if small:
        res += " sha1:%s" % hashlib.sha1(pt.get_bytes()).hexdigest()
    res += "\n" + "".join(hooks)
    return res, so.getvalue(), "".join(logs), bytes(pt.get_bytes())


def _splitnl(s: str):
    """Go's strings.SplitAfter(s, "\\n") without the empty last piece."""
    parts = s.split("\n")
    return [x + "\n" for x in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def _filter_one(stdout: str, log: str):
    """Drop the lines of the skipped patches (in Go, their order is random)."""
    so = []
    for line in _splitnl(stdout):
        if line.startswith("  [") and "] Skipping disabled patch `" in line:
            continue
        if line.startswith("  [") and "] Applying patch `" in line:
            line = "  [*/" + line[line.index("/") + 1 :]
        so.append(line)
    lo = []
    started = False
    for line in _splitnl(log):
        if line.startswith("  applying patch `"):
            started = True
        if not started or line in ("  ResetBaseAddress()\n", "  Enabled: false\n") or line.startswith("  skipping patch `"):
            continue
        lo.append(line)
    return "".join(so), "".join(lo)


def _indent(s: str) -> str:
    return "".join("    | " + line.removesuffix("\n") + "\n" for line in _splitnl(s))


def describe(patch: bytes, buf_names, bufs: dict[str, bytes]) -> str:
    """The canonical description of a patch file (see the module docstring)."""
    w = []
    try:
        try:
            ps = P.parse(patch)
        except P.Patch32lsbError as e:
            return "parse %s\n" % _errs(e)
        w.append("parse ok\n")
        names = _sorted(ps)
        for n in names:
            w.append("patch %s\n" % quote(n))
            w.extend(_inst(i) for i in ps.patches[n])
        vs = validate_set(ps)
        w.append("validate %s\n" % " ".join(vs))
        actual = _validate(ps)
        if (quote(actual) if actual is not None else "ok") not in vs:
            w.append("BUG: file-order validate result %s not possible in Go\n" % quote(str(actual)))

        def se(n, en):
            try:
                ps.set_enabled(n, en)
            except P.Patch32lsbError as e:
                return e
            return None

        w.append("setenabled-missing %s %s\n" % (_errs(se("\x00missing", False)), _errs(se("\x00missing", True))))
        for bn in buf_names:
            buf = bufs[bn]
            small = len(buf) < SMALL
            w.append("buf %s\n" % bn)
            for n in names:
                w.append(" one %s\n" % quote(n))
                for m in names:
                    e = se(m, m == n)
                    if e is not None:
                        w.append("  setenabled %s %s\n" % (quote(m), _errs(e)))
                vs = validate_set(ps)
                if vs != ["ok"]:
                    w.append("  invalid %s\n" % " ".join(vs))
                    continue
                res, so, lo, _ = _apply(ps, buf, small)
                so, lo = _filter_one(so, lo)
                w.append("  %s   stdout:\n%s   log:\n%s" % (res, _indent(so), _indent(lo)))
            w.append(" all\n")
            for m in names:
                e = se(m, True)
                if e is not None:
                    w.append("  setenabled %s %s\n" % (quote(m), _errs(e)))
            vs = validate_set(ps)
            if vs != ["ok"]:
                w.append("  invalid %s\n" % " ".join(vs))
                continue
            cur = buf
            for n in names:
                res, so, lo, nb = _apply(_sub(ps, [n]), cur, small)
                w.append("  %s %s   stdout:\n%s   log:\n%s" % (quote(n), res, _indent(so), _indent(lo)))
                if not res.startswith("ok"):
                    break
                cur = nb
    except GoPanic as e:
        w.append("panic %s\n" % quote(str(e)))
    return "".join(w)


def describe_bytes(patch: bytes, buf_names, bufs) -> bytes:
    return describe(patch, buf_names, bufs).encode("utf-8", "surrogateescape")


# --------------------------------------------------------------------------
# Generator


def _esc(r: random.Random, b: bytes) -> str:
    """Write bytes as a backtick string, with random (valid) escapes."""
    out = ["`"]
    named = {0x0A: "\\n", 0x0D: "\\r", 0x09: "\\t", 0x0B: "\\v", 0x00: "\\0", 0x5C: "\\\\", 0x60: "\\`", 0x22: '\\"', 0x27: "\\'"}
    s = b.decode("utf-8", "surrogateescape")
    for ch in s:
        c = ord(ch)
        if 0xDC80 <= c <= 0xDCFF:
            out.append("\\x%02x" % (c - 0xDC00) if r.random() < 0.5 else "\\x%02X" % (c - 0xDC00))
        elif c in named and (c in (0x0A, 0x0D, 0x5C, 0x60) or r.random() < 0.6):
            if c == 0x5C and r.random() < 0.3:
                out.append("\\x5c")
            else:
                out.append(named[c])
        elif c < 0x20 or c == 0x7F:
            out.append("\\x%02x" % c)
        elif c < 0x80 and r.random() < 0.05:
            out.append("\\x%02x" % c)
        else:
            out.append(ch)
    out.append("`")
    return "".join(out)


def _hex(r: random.Random, b: bytes) -> str:
    h = b.hex()
    if r.random() < 0.5:
        h = h.upper()
    if r.random() < 0.6:
        h = " ".join(h[i : i + 2] for i in range(0, len(h), 2))
    return h


def _off(r: random.Random, v: int) -> str:
    if v < 0:
        if r.random() < 0.5:
            return "-%X" % -v
        return "%08X" % (v & 0xFFFFFFFF)
    if r.random() < 0.3:
        return "%08X" % v
    return ("%X" if r.random() < 0.7 else "%x") % v


WEIRD_OFFSETS = [
    "0", "00000000", "1", "-1", "+1", "-0", "7FFFFFFF", "80000000", "-80000000", "-80000001", "FFFFFFFF", "FFFFFFF0",
    "ffffffF0", "FFFFFF00", "0FFFFFF0", "1FFFFFF0", "FFFFFF", "100000000", "FFFFFFFFFFFFFFFF", "123456789ABCDEF012",
    "0x10", "10h", "", "zz", "1 0", " 10", "\u00a010", "１", "-", "+", "8000000F", "fffffffe", "DEADBEEF",
]
WEIRD_FLOATS = [
    "1.5", "2", "-0", "0", "NaN", "nan", "inf", "-Inf", "+inf", "Infinity", "1e3", "1E3", "1e400", "-1e400", "1e-400",
    "0x1p1", "0X1.8P1", "0x1.8p1", "1p1", "1.5p-3", "1P1", "1_0.5", "1__0", "_1", "0x_1p1", ".5", "5.", ".", "e5",
    "1e", "1e+", "0x", "0x1", "1.2.3", "1,2", "", "abc", "2.5e-324", "1.7976931348623159e308", "4.9e-324", "1e5p3",
]
WEIRD_INTS = ["0", "255", "256", "-1", "+1", "010", "1_0", "", "x", "99999999999999999999", "18446744073709551616", "1.0", "0x1"]


class Gen:
    def __init__(self, seed: int, bufs: dict[str, bytes]):
        self.r = random.Random(seed)
        self.bufs = bufs
        self.lib_strings = []
        lib = bufs.get("libnickel")
        if lib:
            rr = random.Random(seed + 1)
            pat = re.compile(rb"[ -~]{8,40}")
            while len(self.lib_strings) < 300:
                p = rr.randrange(len(lib) - 4096)
                for m in pat.finditer(lib, p, p + 4096):
                    self.lib_strings.append((m.start(), m.group()))
                    break

    # -- instructions which (mostly) apply ---------------------------------

    def inst_lines(self, target: str):
        """Instruction lines for a patch, mostly applicable to target."""
        r = self.r
        buf = self.bufs.get(target, self.bufs["synth"])
        lines = []
        for _ in range(r.randint(1, 4)):
            k = r.choice(["rb", "rb", "rs", "frs", "fba", "ri", "rf", "ba", "zl", "zh", "rz", "rawrb"])
            if target == "libnickel" and self.lib_strings and k in ("rs", "frs", "fba", "rb"):
                pos, s = r.choice(self.lib_strings)
            else:
                s = r.choice(STRINGS)
                pos = buf.find(s)
            if k == "fba" or (k in ("rb", "rs") and r.random() < 0.6):
                if pos >= 0:
                    sub = s[: r.randint(1, len(s))]
                    lines.append("find_base_address = %s" % _esc(r, sub))
                    base = buf.find(sub)
                else:
                    base = 0
            elif k in ("rb", "rs") and r.random() < 0.5:
                base = max(0, pos - r.randint(0, 64))
                lines.append("base_address = %s" % _off(r, base))
            else:
                base = 0
            if k in ("rb", "rawrb"):
                p = pos if pos >= 0 and r.random() < 0.7 else r.randrange(len(buf))
                n = r.randint(1, 8)
                find = buf[p : p + n] or b"\x00"
                rep = bytes(r.getrandbits(8) for _ in range(len(find)))
                if r.random() < 0.1:
                    rep = rep[:-1] or b"\x01\x02"
                lines.append("replace_bytes = %s, %s, %s" % (_off(r, p - base), _hex(r, find), _hex(r, rep)))
            elif k == "rs":
                p = pos if pos >= 0 else 0
                find = s[: r.randint(1, len(s))]
                rep = bytes(r.choice(b"abcXYZ\x00\xc3\xa9") for _ in range(r.randint(0, len(find) + 2)))
                lines.append("replace_string = %s, %s, %s" % (_off(r, p - base + r.choice([0, 0, 0, 1, -1])), _esc(r, find), _esc(r, rep)))
            elif k == "frs":
                find = s[: r.randint(1, len(s))]
                rep = bytes(r.choice(b"abc\x00") for _ in range(r.randint(0, len(find) + 1)))
                lines.append("find_replace_string = %s, %s" % (_esc(r, find), _esc(r, rep)))
            elif k == "ri":
                p = r.randrange(len(buf))
                lines.append("base_address = %s" % _off(r, p))
                v = buf[p + 1] if p + 1 < len(buf) and r.random() < 0.8 else r.randint(0, 255)
                lines.append("replace_int = 1, %d, %d" % (v, r.randint(0, 255)))
            elif k == "rf":
                i = r.randrange(len(FLOATS) + 1)
                f = FLOATS[i] if i < len(FLOATS) else float("nan")
                fb = struct.pack("<d", f) if i < len(FLOATS) else struct.pack("<Q", NAN_BITS)
                p = buf.find(fb)
                fs = repr(f) if i < len(FLOATS) else "NaN"
                if r.random() < 0.2:
                    fs = r.choice(WEIRD_FLOATS)
                lines.append("replace_float = %s, %s, %s" % (_off(r, max(p, 0)), fs, r.choice(["3.25", "0", "-1e5", "1p4"])))
            elif k == "ba":
                lines.append("base_address = %s" % _off(r, r.randrange(len(buf) + 2)))
            elif k == "zl":
                css = r.choice([CSS1, CSS2])
                a = r.randrange(len(css))
                snip = css[a : a + r.randint(1, 30)]
                if r.random() < 0.3:
                    snip = snip.replace(": ", ":").replace("\n  ", "\n")
                lines.append("find_zlib = %s" % _esc(r, snip.encode()))
            elif k == "zh":
                css = r.choice([CSS1, CSS2, "nope"])
                h = hashlib.sha1(css.encode()).hexdigest()
                if r.random() < 0.2:
                    h = h[: r.randint(0, 45)]
                if r.random() < 0.1:
                    h = h.upper()
                lines.append("find_zlib_hash = `%s`" % h)
            elif k == "rz":
                css = r.choice([CSS1, CSS2])
                if r.random() < 0.8:
                    lines.append("find_zlib = %s" % _esc(r, css[: r.randint(5, 20)].encode()))
                a = r.randrange(len(css))
                find = css[a : a + r.randint(1, 20)]
                rep = r.choice(["", "x", "color: red;", find.upper(), "a" * r.randint(1, 200)])
                lines.append("replace_zlib = %s, %s, %s" % (_off(r, r.choice([0, 0, 0, 1, -1, 2])), _esc(r, find.encode()), _esc(r, rep.encode())))
        return lines

    def patch_block(self, name: str, target: str, enabled=None, group=None):
        r = self.r
        lines = ["<Patch>" if r.random() < 0.9 else r.choice(["<patch>", "<PATCH>", "<pAtCh>"])]
        lines.append("patch_name = %s" % _esc(r, name.encode("utf-8", "surrogateescape")))
        if enabled is None:
            enabled = r.random() < 0.5
        lines.append("patch_enable = `%s`" % ("yes" if enabled else "no"))
        if group is not None:
            lines.append("patch_group = %s" % _esc(r, group.encode()))
        if r.random() < 0.3:
            lines.append(r.choice(["# a comment", "#", "", "## another # comment", "#patch_name = `x`"]))
        lines += self.inst_lines(target)
        lines.append("</Patch>" if r.random() < 0.9 else r.choice(["</patch>", "</PATCH>"]))
        return lines

    def valid_file(self, target: str):
        r = self.r
        lines = []
        if r.random() < 0.3:
            lines += ["# Patches for %s" % target, ""]
        groups = [None, None, "Fonts", "Dictionary", "g"]
        names = set()
        for _ in range(r.randint(1, 4)):
            name = r.choice(["Patch", "Increase font size", "a", "Ünïcödé", "dup", "with `tick`", "x\\y", "tab\there", ""]) + str(r.randint(0, 9) if r.random() < 0.7 else "")
            if name == "" or (name in names and r.random() < 0.8):
                name += "_%d" % len(names)
            names.add(name)
            lines += self.patch_block(name, target, group=r.choice(groups))
            if r.random() < 0.3:
                lines.append("")
        return lines

    # -- mutations -----------------------------------------------------------

    TRICKY = [
        "patch_name = `x`", "patch_name = `dup`", "# patch_group = `g`", "#patch_group", "patch_group = `g`", "patch_group = ``",
        "patch_enable = `yes`", "patch_enable = `no`", "patch_enable = `YES`", "patch_enable = yes", "patch_enable = `no` x",
        "<Patch>", "</Patch>", "<Patch> x", "unknown_key = 1", "replace_bytes = 1, 2", "bogus", "=", " = ", "a = ", "a = b",
        "patch_name = `", "patch_name = ``", "patch_name = `\\`", "patch_name = `a\\`", "patch_name = `\\\\\\`", "patch_name = `\\x4`",
        "patch_name = `\\x`", "patch_name = `\\xg1`", "patch_name = `\\x 1`", "patch_name = `\\x\u00a0`", "patch_name = `\\x\xa0`",
        "find_base_address = `\\q`", "find_base_address = `\\\u00e9`", "find_base_address = ```", "find_base_address = `a`b`",
        "replace_string = 0, `a`", "replace_string = 0,`a`,`b`", "replace_string = 0, `a`, `b`, `c`", "replace_string = 0, `a``b`",
        "replace_string = 0, `\\x`, `b`", "replace_string = 0, `\\x`a`, `b`", "replace_string = 0, `a\\`, `b`", "replace_string = 0, ``, ``",
        "replace_string = FFFFFFF0, `a`, `b`", "replace_zlib = FFFFFF00, `a`, `b`", "replace_zlib = 0FFFFFF0, `a`, `b`",
        "find_replace_string = `a`, `b` junk", "find_replace_string = `a`", "find_replace_string = `a`,,,  , `b`",
        "replace_bytes = FFFFFFF0, 00, 01", "replace_bytes = 80000000, 00, 01", "base_address = FFFFFFF0", "base_address = 7FFFFFFF",
        "replace_int = FFFFFFFF, 1, 2", "replace_float = FFFFFFF8, 1.5, 2", "replace_bytes = 0, 0, 1", "replace_bytes = 0, 012, 345",
        "replace_bytes = 0, 0x41, 42", "replace_bytes = 0,\t41,\t42", "replace_bytes = 0\t, 41, 42", "replace_bytes = 0, , 42",
        "find_zlib_hash = `0123456789012345678901234567890123456789`", "find_zlib_hash = `\u00e9123456789012345678901234567890123456789`",
        "fİnd_base_address = `Hello`", "FIND_BASE_ADDRESS = `Hello`", "Patch_Name = `y`", "patch_name  =  `z`", "patch_name\t=\t`z`",
        "patch_name = `\u00a0`", "\u3000patch_name = `w`\u3000", "patch_name = `\\x41\\x42`", "patch_name = `\\xc3\\xa9`",
        "patch_name = `\\xff`", "patch_name = `\xff`", "find_zlib = ``", "replace_zlib = 0, `a`", "base_address = 1 junk",
        "replace_bytes = 7FFFFFFF, 00, 01", "replace_bytes = 7FFFFFF0, 00, 01", "replace_string = 7FFFFFF0, `a`, `b`",
        "replace_zlib = 7FFFFFFE, `a`, `b`", "replace_zlib = -10, `a`, `b`", "replace_zlib = -1, `a`, `b`", "replace_int = 7FFFFFFE, 1, 2",
        "replace_float = 7FFFFFF9, 1.5, 2", "replace_bytes = -7FFFFFFF, 00, 01", "find_base_address = `Hello`", "base_address = 10",
        "find_zlib_hash = `\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9`",
    ]

    def mutate(self, lines):
        r = self.r
        lines = list(lines)
        for _ in range(r.randint(1, 3)):
            if not lines:
                lines.append(r.choice(self.TRICKY))
                continue
            i = r.randrange(len(lines))
            op = r.randrange(14)
            line = lines[i]
            if op == 0 and " = " in line:
                lines[i] = line.replace(" = ", r.choice(["=", " =", "= ", "  =  ", "\t=\t", " =  ", "  = ", " = \t", "\u00a0=\u00a0", " == "]), 1)
            elif op == 1 and " = " in line:
                k, v = line.split(" = ", 1)
                k = r.choice([k.upper(), k.title(), k.replace("i", "\u0130", 1), k.replace("_", "-"), k + "x", k[:-1]])
                lines[i] = k + " = " + v
            elif op == 2:
                del lines[i]
            elif op == 3:
                lines.insert(i, line)
            elif op == 4 and len(lines) > 1:
                j = r.randrange(len(lines))
                lines[i], lines[j] = lines[j], lines[i]
            elif op == 5:
                lines.insert(i, r.choice(self.TRICKY))
            elif op == 6 and line:
                lines[i] = line[: r.randrange(len(line))]
            elif op == 7:
                lines[i] = line + r.choice([",", " x", "`", "\\", " ", "\t", "\u00a0", "\u3000", "\x0b", "\x0c", "\x1c", "\x85", "\r", "#"])
            elif op == 8:
                lines[i] = r.choice(["  ", "\t", "\u00a0", "\u2003", "\x1f", "\ufeff"]) + line
            elif op == 9 and ", " in line:
                a, b = line.split(", ", 1)
                if " = " in a:
                    k, _ = a.split(" = ", 1)
                    lines[i] = "%s = %s, %s" % (k, r.choice(WEIRD_OFFSETS), b)
            elif op == 10 and line.startswith(("replace_float", "replace_int")):
                k, v = line.split(" = ", 1)
                parts = v.split(", ")
                j = r.randrange(len(parts))
                parts[j] = r.choice(WEIRD_FLOATS if line.startswith("replace_float") else WEIRD_INTS)
                lines[i] = k + " = " + ", ".join(parts)
            elif op == 11 and line.startswith("base_address"):
                lines[i] = "base_address = " + r.choice(WEIRD_OFFSETS)
            elif op == 12 and "`" in line:
                # break a string
                j = line.index("`")
                ins = r.choice(["\\", "\\x", "\\x4", "\\q", "`", "\\\\", "\\`", "\\0", "\\xzz", "\\x\u00e9", "\xe9", "\udcff"])
                lines[i] = line[: j + 1] + ins + line[j + 1 :]
            elif op == 13:
                lines.insert(i, r.choice(["<Patch>", "</Patch>", "patch_name = `stray`", "patch_enable = `yes`", "replace_bytes = 0, 00, 00"]))
        return lines

    def serialize(self, lines) -> bytes:
        r = self.r
        nl = "\r\n" if r.random() < 0.15 else "\n"
        text = nl.join(lines)
        if r.random() < 0.7:
            text += nl
        if r.random() < 0.03:
            text = "\ufeff" + text
        if r.random() < 0.02:
            text = text.replace("\n", "\r")
        return text.encode("utf-8", "surrogateescape")

    def case(self):
        r = self.r
        target = r.choice(["synth"] * 6 + ["tiny"] + (["libnickel"] if "libnickel" in self.bufs else []))
        x = r.random()
        if x < 0.35:
            lines = self.valid_file(target)
        elif x < 0.9:
            lines = self.mutate(self.valid_file(target))
        else:
            lines = [r.choice(self.TRICKY) for _ in range(r.randint(0, 8))]
            if r.random() < 0.7:
                lines = ["<Patch>", "patch_name = `t`", "patch_enable = `yes`"] + lines + ["</Patch>"]
        bufs = [target]
        if target != "synth" and r.random() < 0.3:
            bufs.append("synth")
        if r.random() < 0.1:
            bufs.append("empty1")
        return self.serialize(lines), bufs


REAL_WORLD = [
    b"""# Kobo firmware patches
<Patch>
patch_name = `Change dicthtml strings to micthtml`
patch_enable = `no`
patch_group = `Dictionary`
replace_string = 0, `dicthtml%1/dicthtml%2`, `micthtml%1/micthtml%2`
</Patch>

<Patch>
patch_name = `Custom font sizes`
patch_enable = `yes`
find_base_address = `font-size: 12px`
replace_string = 0B, `12`, `14`
</Patch>
""",
    b"""<Patch>
patch_name = `Replace bytes`
patch_enable = `yes`
base_address = 00000006
replace_bytes = 0, 48 65 6C 6C 6F, 4A 65 6C 6C 79
</Patch>
<Patch>
patch_name = `Float`
patch_enable = `yes`
replace_float = 0, 1.5, 2.5
</Patch>
<Patch>
patch_name = `CSS`
patch_enable = `yes`
find_zlib = `font-size: 12px;`
replace_zlib = 0, `font-size: 12px;`, `font-size: 9px;`
</Patch>
""",
]


# --------------------------------------------------------------------------
# Go side and comparison


def run_go(helper: str, cases, bufs: dict[str, bytes]) -> list[bytes]:
    with tempfile.TemporaryDirectory() as td:
        args = [helper]
        for name, b in bufs.items():
            p = Path(td) / name
            p.write_bytes(b)
            args.append("%s=%s" % (name, p))
        inp = "".join(json.dumps({"id": i, "patch": base64.b64encode(p).decode(), "bufs": bn}) + "\n" for i, (p, bn) in enumerate(cases))
        res = subprocess.run(args, input=inp.encode(), capture_output=True, check=True)
    out = [None] * len(cases)
    for line in res.stdout.decode().splitlines():
        d = json.loads(line)
        out[d["id"]] = base64.b64decode(d["out"])
    return out


@contextlib.contextmanager
def proposed_patcher_fixes():
    """Temporarily give Patcher three fixes it lacks.

    * In go_compat mode, kobopatch's int32 arithmetic for cur+offset wraps
      around (and then panics on the negative slice index).
    * For ReplaceZlib's buf[pos:pos+2], Go checks the upper bound first, so
      the panic message names pos+2 when that is negative.
    * FindZlibHash checks that the hash is 40 bytes long, not 40 characters.

    Used to classify go_compat mismatches as coming from patchlib.patcher
    rather than from patch32lsb.
    """
    # These fixes are now part of patchlib.patcher; kept as a no-op so the
    # classification code below stays unchanged.
    yield


FIXES = (("1", "fix1-offsets"), ("2", "fix2-patch_name"), ("4", "fix4-panics"), ("patcher", "patcher-fixes"))


@contextlib.contextmanager
def reverted(fixes):
    """Revert some of kobopatcher's fixes: patch32lsb fix numbers, and
    "patcher" for the go_compat behaviour of patchlib.patcher (with the
    fixes it lacks, see proposed_patcher_fixes)."""
    from kobopatcher.patchlib import patcher as PM

    old_fixed, old_gc = P._fixed, PM.go_compat
    P._fixed = lambda n: str(n) not in fixes and old_fixed(n)
    if "patcher" in fixes:
        PM.go_compat = lambda: True
    try:
        with proposed_patcher_fixes() if "patcher" in fixes else contextlib.nullcontext():
            yield
    finally:
        P._fixed, PM.go_compat = old_fixed, old_gc


def classify_fixed(patch: bytes, bn, bufs, go_out: bytes) -> str:
    """The smallest set of reverted fixes which makes the default mode match Go."""
    keys = [k for k, _ in FIXES]
    for size in range(1, len(keys) + 1):
        for combo in itertools.combinations(keys, size):
            with reverted(set(combo)):
                if describe_bytes(patch, bn, bufs) == go_out:
                    return "+".join(dict(FIXES)[k] for k in combo)
    return "UNEXPECTED"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("go_helper", help="path to the built Go p32ref helper")
    ap.add_argument("--count", type=int, default=3000, help="number of generated patch files")
    ap.add_argument("--seed", type=int, default=32)
    ap.add_argument("--fixed", action="store_true", help="compare kobopatcher's default mode (classify differences)")
    ap.add_argument("--no-libnickel", action="store_true", help="only use the synthetic buffers")
    ap.add_argument("--write-fixture", type=Path, help="write a sample of the cases + Go's answers as JSON")
    ap.add_argument("--fixture-max", type=int, default=500)
    ap.add_argument("--show", type=int, default=5, help="mismatches to print in full")
    args = ap.parse_args()

    bufs = load_buffers(not args.no_libnickel)
    gen = Gen(args.seed, bufs)
    cases = [(p, ["synth", "tiny"]) for p in REAL_WORLD]
    cases += [gen.case() for _ in range(args.count)]
    print("cases: %d (buffers: %s)" % (len(cases), ", ".join("%s=%d" % (k, len(v)) for k, v in bufs.items())), file=sys.stderr)
    go = run_go(args.go_helper, cases, bufs)

    stats = {"parse_err": 0, "panic": 0, "applied_ok": 0, "apply_err": 0}
    mismatches = []
    classes: dict[str, int] = {}
    patcher_cases: set[int] = set()
    with go_compat_mode(not args.fixed):
        for idx, ((patch, bn), g) in enumerate(zip(cases, go)):
            stats["parse_err"] += g.startswith(b"parse err")
            stats["panic"] += b"panic " in g
            stats["applied_ok"] += g.count(b"  ok")
            stats["apply_err"] += g.count(b"  err ")
            p = describe_bytes(patch, bn, bufs)
            if p == g:
                continue
            mismatches.append(idx)
            if args.fixed:
                cls = classify_fixed(patch, bn, bufs, g)
            else:
                with proposed_patcher_fixes():
                    cls = "patcher.py" if describe_bytes(patch, bn, bufs) == g else "UNEXPECTED"
                if cls == "patcher.py":
                    patcher_cases.add(idx)
            classes[cls] = classes.get(cls, 0) + 1
            gl, pl = g.split(b"\n"), p.split(b"\n")
            k = next((j for j, (a, b) in enumerate(zip(gl, pl)) if a != b), min(len(gl), len(pl)))
            print("mismatch case %d %s: go %r / py %r" % (idx, cls, gl[k] if k < len(gl) else b"", pl[k] if k < len(pl) else b""))
            if cls == "UNEXPECTED" and args.show > 0:
                args.show -= 1
                print(
                    "=== MISMATCH case %d bufs=%s\n--- patch:\n%s\n--- go:\n%s\n--- py:\n%s"
                    % (idx, bn, patch.decode("utf-8", "replace"), g.decode("utf-8", "replace"), p.decode("utf-8", "replace"))
                )
    print("go results: %s" % stats, file=sys.stderr)
    print("mode: %s; compared %d cases; mismatches: %d" % ("fixed" if args.fixed else "go_compat", len(cases), len(mismatches)), file=sys.stderr)
    for k, v in sorted(classes.items()):
        print("  %-40s %d" % (k, v), file=sys.stderr)

    if args.write_fixture:
        if args.fixed:
            print("--write-fixture needs go_compat mode", file=sys.stderr)
            return 2
        r = random.Random(args.seed)
        ok = [i for i, (p, bn) in enumerate(cases) if len(p) < 1500 and len(go[i]) < 8000]
        panics = [i for i in ok if b"panic " in go[i]]
        lib = [i for i in ok if "libnickel" in cases[i][1]]
        pick = set(r.sample(panics, min(len(panics), args.fixture_max // 10)))
        pick |= set(r.sample(lib, min(len(lib), args.fixture_max // 10)))
        pick |= set(range(len(REAL_WORLD)))
        pick |= set(sorted(patcher_cases)[:10])
        rest = [i for i in ok if i not in pick]
        pick |= set(r.sample(rest, max(0, min(len(rest), args.fixture_max - len(pick)))))

        def enc(b: bytes, key: str):
            try:
                return {key: b.decode("utf-8")}
            except UnicodeDecodeError:
                return {key + "_b64": base64.b64encode(b).decode()}

        synth = synth_buffers()
        doc = {
            "description": "patch32lsb cases with kobopatch's answers, generated by tools/parity/p32_diff.py against a Go helper "
            "built on kobopatch's patchfile/patch32lsb. 'out' is the canonical description (p32_diff.describe, go_compat mode) "
            "of the patch file 'patch' applied to the buffers 'bufs' (synthetic ones from p32_diff.synth_buffers, and "
            "tests/testdata/libnickel.so.1.0.0); *_b64 fields are base64 (not valid UTF-8). 'patcher_issue' marks cases that "
            "only match with the patchlib.patcher go_compat fixes in p32_diff.proposed_patcher_fixes.",
            "buffers": {k: hashlib.sha1(v).hexdigest() for k, v in synth.items()},
            "cases": [
                {**enc(cases[i][0], "patch"), "bufs": cases[i][1], **enc(go[i], "out"), **({"patcher_issue": True} if i in patcher_cases else {})}
                for i in sorted(pick)
            ],
        }
        args.write_fixture.write_text(json.dumps(doc, indent=0, ensure_ascii=False) + "\n", encoding="utf-8")
        print("wrote %d cases to %s" % (len(pick), args.write_fixture), file=sys.stderr)
    return 1 if "UNEXPECTED" in classes else 0


if __name__ == "__main__":
    sys.exit(main())
