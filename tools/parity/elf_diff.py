#!/usr/bin/env python3
"""Differential test of kobopatcher.patchlib.elfpatch against Go kobopatch.

Applies the structure-aware ELF patches (ElfLazyBinding, StubFunction,
NopCall, Tombstone, CheckELF, and the ElfClearDynFlag / ElfRemoveDynEntry /
ElfDisableProgramHeader building blocks) to firmware files with both
implementations and compares every result (return values, error messages,
hook calls) and the output bytes. Every case is run twice: on the original
file, and again on the output of the first run (which must fail the same way
in both implementations).

The Go helper (its source lives outside this repo) is invoked as
``helper IN OUT OPS_JSON``. OPS_JSON is a list of operations, each a list of
strings: ["lazy"], ["stub", sym, "void"|n], ["nop", sym, rel, target],
["tombstone", setpath, name], ["clearflag", tag, mask], ["rmdyn", tag],
["disphdr", type], ["check"] (CheckELF against IN). It prints one JSON object
per operation ({"op", "err", "changes", "off", "orig", "old", "found",
"hooks"}, see ``py_run``) and writes the final buffer to OUT.

The firmware files are only read, never executed.

Usage:
  elf_diff.py GO_HELPER [--rootfs DIR ...] [--keep DIR] [-v]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from kobopatcher.patchlib.patcher import Patcher, PatchError

ARCHIVE = Path.home() / "Documents/Github/kobo_aura_h2o/archive"
DEFAULT_ROOTFS = [ARCHIVE / v / "x/rootfs" for v in ("fw-4.30.18838", "fw-4.34.20097", "fw-4.38.23429", "fw-4.7.10413")]

LIBNICKEL = "usr/local/Kobo/libnickel.so.1.0.0"
QT = "usr/local/Trolltech/QtEmbedded-4.6.2-arm/lib/"
LAZY_LIBS = [
    "usr/local/Kobo/libiwnn.so.1.0.0",
    LIBNICKEL,
    "usr/local/Kobo/libQtSolutions_IOCompressor-2.3.so.1.0.0",
    "usr/local/Kobo/librmsdk.so.1.0.0",
    "usr/local/Kobo/libsunpinyin.so.3.0.0",
] + [
    QT + "lib%s.so.4.6.2" % n
    for n in (
        "QtConcurrent",
        "QtCore",
        "QtDBus",
        "QtGui",
        "QtMultimedia",
        "QtMultimediaWidgets",
        "QtNetwork",
        "QtPrintSupport",
        "QtScript",
        "QtSensors",
        "QtSql",
        "QtSvg",
        "QtWebKit",
        "QtWebKitWidgets",
        "QtWidgets",
        "QtXmlPatterns",
        "QtXml",
    )
]

SET_ATTRIBUTE = "_ZN7QWidget12setAttributeEN2Qt15WidgetAttributeEb"
DISABLE_TIMER = "_ZN10SketchNote16disableWithTimerEi"

# src/versions/4.38.23429/src/libnickel.yaml (the stub and the 4 NopCalls)
NICKEL_OPS = [
    ["stub", "_ZN18HomePageController22maybeShowKoboPlusPromoEv", "void"],
    ["nop", "_ZN11ReadingView8nextPageEv", "0xc", DISABLE_TIMER],
    ["nop", "_ZN11ReadingView8prevPageEv", "0xc", DISABLE_TIMER],
    ["nop", "_ZN11ReadingView8nextPageEv", "0x1e", SET_ATTRIBUTE],
    ["nop", "_ZN11ReadingView8prevPageEv", "0x1e", SET_ATTRIBUTE],
    ["check"],
]

# the error paths exercised by elf_test.go (and a few more)
NICKEL_ERR_OPS = [
    ["stub", "ReadingView::nextPage()", "0"],
    ["stub", "_ZN11ReadingView8prevPageEv", "256"],
    ["stub", "_ZN11ReadingView8doesNotExistEv", "void"],
    ["stub", "_ZTV11ReadingView", "void"],
    ["stub", SET_ATTRIBUTE, "void"],
    ["nop", "_ZN11ReadingView8nextPageEv", "0xc", SET_ATTRIBUTE],
    ["nop", "_ZN11ReadingView8nextPageEv", "0x0", DISABLE_TIMER],
    ["nop", "_ZN11ReadingView8nextPageEv", "0x1000", DISABLE_TIMER],
    ["nop", "_ZN11ReadingView8nextPageEv", "-2", DISABLE_TIMER],
    ["nop", "_ZN11ReadingView8nextPageEv", "0xc", "_ZN3Foo3barEv"],
    ["nop", "_ZTV11ReadingView", "0xc", DISABLE_TIMER],
    ["clearflag", "24", "1"],  # DT_BIND_NOW
    ["clearflag", str(0x6FFFFFFB), "0"],
    ["clearflag", "30", "8"],  # DT_FLAGS (not present)
    ["rmdyn", "1"],  # DT_NEEDED
    ["disphdr", str(0x6474E551)],  # PT_GNU_STACK
    ["check"],
    ["clearflag", str(0x6FFFFFFB), "1"],
    ["check"],  # partly lazy
    ["rmdyn", "24"],
    ["check"],  # lazy with RELRO
    ["disphdr", str(0x6474E552)],
    ["check"],
]

LAZY_OPS = [["lazy"], ["check"]]
TOMBSTONE_OPS = [
    ["tombstone", "", "usr/local/Kobo/libboggle.so"],
    ["tombstone", "./usr/local/Kobo/libboggle.so", "usr/local/Kobo/libsudoku.so"],
    ["tombstone", "./usr/local/Kobo/libboggle.so", "libboggle.so"],
    ["tombstone", "./usr/local/Kobo/libboggle.so", "usr/local/Kobo/libboggle.so"],
    ["tombstone", "./usr/local/Kobo/libboggle.so", "usr/local/Kobo/libboggle.so"],
    ["check"],
]


def py_run(inp: bytes, ops):
    """Apply ops like the Go helper; returns (results, output bytes)."""
    orig = bytes(inp)
    p = Patcher(bytearray(inp))
    hooks = []

    def hook(off, find, replace):
        hooks.append([off, len(find), -1 if replace is None else len(replace)])

    p.set_hook(hook)
    out = []
    for op in ops:
        hooks.clear()
        res = {"op": op[0], "err": None}
        try:
            if op[0] == "lazy":
                ch = None
                try:
                    ch = p.elf_lazy_binding()
                except PatchError as e:
                    ch = e.result
                    raise
                finally:
                    if ch:
                        res["changes"] = list(ch)
            elif op[0] == "stub":
                ret = None if op[2] == "void" else int(op[2], 0)
                off, ob = 0, None
                try:
                    off, ob = p.stub_function(op[1], ret)
                except PatchError as e:
                    off, ob = e.result
                    raise
                finally:
                    res["off"] = off
                    if ob is not None:
                        res["orig"] = ob.hex()
            elif op[0] == "nop":
                off = 0
                try:
                    off = p.nop_call(op[1], int(op[2], 0), op[3])
                except PatchError as e:
                    off = e.result
                    raise
                finally:
                    res["off"] = off
            elif op[0] == "tombstone":
                p.set_path(op[1])
                p.tombstone(op[2])
            elif op[0] == "clearflag":
                old, found = 0, False
                try:
                    old, found = p.elf_clear_dyn_flag(int(op[1], 0), int(op[2], 0))
                except PatchError as e:
                    old, found = e.result
                    raise
                finally:
                    res["old"], res["found"] = old, found
            elif op[0] == "rmdyn":
                found = False
                try:
                    found = p.elf_remove_dyn_entry(int(op[1], 0))
                except PatchError as e:
                    found = e.result
                    raise
                finally:
                    res["found"] = found
            elif op[0] == "disphdr":
                found = False
                try:
                    found = p.elf_disable_program_header(int(op[1], 0))
                except PatchError as e:
                    found = e.result
                    raise
                finally:
                    res["found"] = found
            elif op[0] == "check":
                p.check_elf(orig)
            else:
                raise ValueError(op[0])
        except PatchError as e:
            res["err"] = str(e)
        except Exception as e:  # noqa: BLE001 - reported as a mismatch
            res["err"] = "python exception: %r" % e
        res["hooks"] = [list(h) for h in hooks]
        out.append(res)
    return out, bytes(p.get_bytes())


def go_run(helper: str, path: Path, ops, tmp: Path):
    outp = tmp / "go.out"
    r = subprocess.run([helper, str(path), str(outp), json.dumps(ops)], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("go helper failed on %s: %s" % (path, r.stderr.strip()))
    return [json.loads(line) for line in r.stdout.splitlines() if line.strip()], outp.read_bytes()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("helper", help="path to the Go helper")
    ap.add_argument("--rootfs", action="append", type=Path, help="extracted KoboRoot.tgz (repeatable)")
    ap.add_argument("--keep", type=Path, help="write the outputs of the first runs here")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    roots = [r for r in (a.rootfs or DEFAULT_ROOTFS) if r.is_dir()]
    cases = []
    for root in roots:
        for lib in LAZY_LIBS:
            cases.append((root, lib, "lazy", LAZY_OPS))
        cases.append((root, LIBNICKEL, "nickel", NICKEL_OPS))
        cases.append((root, LIBNICKEL, "nickel-errors", NICKEL_ERR_OPS))
        cases.append((root, "usr/local/Kobo/libboggle.so", "tombstone", TOMBSTONE_OPS))

    nfiles = nmis = nops = 0
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for root, rel, name, ops in cases:
            path = root / rel
            if not path.is_file():
                if a.verbose:
                    print("skip (missing): %s" % path)
                continue
            data = path.read_bytes()
            for rnd in (1, 2):
                label = "%s %s [%s] round %d" % (root.parent.parent.name, rel, name, rnd)
                gres, gout = go_run(a.helper, path if rnd == 1 else tmp / "in2", ops, tmp)
                pres, pout = py_run(data, ops)
                nfiles += 1
                nops += len(ops)
                bad = False
                for g, q in zip(gres, pres):
                    if g != q:
                        bad = True
                        print("MISMATCH %s op %s:\n  go: %s\n  py: %s" % (label, g.get("op"), g, q))
                if len(gres) != len(pres):
                    bad = True
                    print("MISMATCH %s: %d go results, %d python results" % (label, len(gres), len(pres)))
                if gout != pout:
                    bad = True
                    print(
                        "MISMATCH %s: output differs (go sha256 %s, py %s)"
                        % (label, hashlib.sha256(gout).hexdigest()[:16], hashlib.sha256(pout).hexdigest()[:16])
                    )
                nmis += bad
                if a.verbose or bad:
                    print("%s %s: %s" % ("FAIL" if bad else "ok", label, "; ".join(str(r["err"]) for r in gres)))
                if rnd == 1:
                    if a.keep:
                        dst = a.keep / root.parent.parent.name / name / rel
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        dst.write_bytes(gout)
                    (tmp / "in2").write_bytes(gout)
                    data = gout
    print("%d runs (%d operations) compared, %d mismatches" % (nfiles, nops, nmis))
    return 1 if nmis else 0


if __name__ == "__main__":
    sys.exit(main())
