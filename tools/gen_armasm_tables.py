#!/usr/bin/env python3
"""Generate src/kobopatcher/patchlib/_armasm/tables.py from rsc.io/arm/armasm.

The instruction tables of the Python armasm port are converted mechanically
from the Go sources of rsc.io/arm/armasm at version
v0.0.0-20150420010332-9c32f2193064 (BSD-style license, see
src/kobopatcher/patchlib/_armasm/LICENSE):

* tables.go: the Op constants (an iota block with ``_`` placeholders), the
  ``opstr`` array and the ``instFormats`` array;
* decode.go: the ``instArg`` constants (another iota block), so that the
  argument codes used by ``instFormats`` have exactly the Go values.

Usage::

    tools/gen_armasm_tables.py [--armasm DIR] [--check] [-o OUT]

DIR defaults to $(go env GOMODCACHE)/rsc.io/arm@<version>/armasm (falling
back to ~/go/pkg/mod).  ``--check`` exits non-zero if OUT is not up to date
instead of writing it.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

VERSION = "v0.0.0-20150420010332-9c32f2193064"
REPO = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO / "src" / "kobopatcher" / "patchlib" / "_armasm" / "tables.py"


def default_armasm_dir() -> Path:
    modcache = None
    try:
        modcache = subprocess.run(
            ["go", "env", "GOMODCACHE"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        pass
    if not modcache:
        modcache = os.path.join(os.path.expanduser("~"), "go", "pkg", "mod")
    return Path(modcache) / f"rsc.io/arm@{VERSION}" / "armasm"


def parse_iota_block(src: str, start_re: str, typename: str) -> list[tuple[str, int]]:
    """Parse a Go ``const ( _ T = iota; A; _; B ... )`` block.

    Returns (name, value) pairs for the named (non-``_``) constants.
    """
    m = re.search(start_re, src, re.M)
    if not m:
        raise SystemExit(f"cannot find const block for {typename}")
    body = src[m.end():]
    end = body.index("\n)")
    lines = body[:end].split("\n")
    out = []
    idx = 0
    first = True
    for raw in lines:
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        if first:
            if not re.fullmatch(rf"_\s+{typename}\s*=\s*iota", line):
                raise SystemExit(f"unexpected first line in {typename} block: {raw!r}")
            first = False
            idx = 1
            continue
        if line == "_":
            idx += 1
            continue
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", line):
            raise SystemExit(f"unexpected line in {typename} block: {raw!r}")
        out.append((line, idx))
        idx += 1
    return out


def parse_opstr(src: str, ops: dict[str, int]) -> dict[int, str]:
    m = re.search(r"^var opstr = \[\.\.\.\]string\{\n", src, re.M)
    if not m:
        raise SystemExit("cannot find opstr")
    body = src[m.end():]
    body = body[: body.index("\n}")]
    out: dict[int, str] = {}
    for raw in body.split("\n"):
        if not raw.strip():
            continue
        mm = re.fullmatch(r'\s*([A-Za-z0-9_]+):\s*"([^"\\]*)",', raw)
        if not mm:
            raise SystemExit(f"unexpected opstr line: {raw!r}")
        name, s = mm.groups()
        out[ops[name]] = s
    return out


FMT_RE = re.compile(
    r"\s*\{(0x[0-9a-f]+), (0x[0-9a-f]+), (-?\d+), ([A-Za-z0-9_]+), (0x[0-9a-f]+), "
    r"instArgs\{([A-Za-z0-9_, ]*)\}\},\s*(?://\s?(.*))?"
)


def parse_formats(src: str, ops: dict[str, int], args: dict[str, int]):
    m = re.search(r"^var instFormats = \[\.\.\.\]instFormat\{\n", src, re.M)
    if not m:
        raise SystemExit("cannot find instFormats")
    body = src[m.end():]
    body = body[: body.index("\n}")]
    out = []
    for raw in body.split("\n"):
        if not raw.strip():
            continue
        mm = FMT_RE.fullmatch(raw)
        if not mm:
            raise SystemExit(f"unexpected instFormats line: {raw!r}")
        mask, value, prio, op, opbits, arglist, comment = mm.groups()
        if op not in ops:
            raise SystemExit(f"unknown op {op!r}")
        alist = [a.strip() for a in arglist.split(",") if a.strip()]
        if len(alist) > 4:
            raise SystemExit(f"too many args: {raw!r}")
        for a in alist:
            if a not in args:
                raise SystemExit(f"unknown instArg {a!r}")
        out.append((mask, value, int(prio), op, opbits, alist, (comment or "").strip()))
    return out


def generate(armasm: Path) -> str:
    tables_src = (armasm / "tables.go").read_text()
    decode_src = (armasm / "decode.go").read_text()

    op_consts = parse_iota_block(tables_src, r"^const \(\n", "Op")
    ops = dict(op_consts)
    if len(ops) != len(op_consts):
        raise SystemExit("duplicate Op names")
    arg_consts = parse_iota_block(decode_src, r"^const \(\n(?=\t_ instArg = iota)", "instArg")
    argmap = dict(arg_consts)
    opstr = parse_opstr(tables_src, ops)
    formats = parse_formats(tables_src, ops, argmap)

    h_tables = hashlib.sha256(tables_src.encode()).hexdigest()
    h_decode = hashlib.sha256(decode_src.encode()).hexdigest()

    w = []
    w.append("# Code generated by tools/gen_armasm_tables.py; DO NOT EDIT.")
    w.append("#")
    w.append(f"# Converted from rsc.io/arm/armasm@{VERSION}:")
    w.append(f"#   tables.go  sha256 {h_tables}")
    w.append(f"#   decode.go  sha256 {h_decode} (instArg constants)")
    w.append("#")
    w.append("# Copyright 2014 The Go Authors.  All rights reserved.")
    w.append("# Use of this source code is governed by a BSD-style")
    w.append("# license that can be found in the LICENSE file.")
    w.append("")
    w.append('"""Instruction tables for the ARM decoder (generated from tables.go)."""')
    w.append("")
    w.append("from .inst import Op")
    w.append("")
    w.append("# instArg constants (decode.go).")
    for name, val in arg_consts:
        w.append(f"{name} = {val}")
    w.append("")
    w.append("# Op constants (tables.go).")
    for name, val in op_consts:
        w.append(f"{name} = Op({val})")
    w.append("")
    w.append("OP_NAMES = (")
    for name, _ in op_consts:
        w.append(f'    "{name}",')
    w.append(")")
    w.append("")
    n = max(opstr) + 1
    w.append(f"# opstr[op] is the string form of op ({n} entries, '' for gaps).")
    w.append("_opstr = {")
    for val, s in sorted(opstr.items()):
        w.append(f'    {val}: "{s}",')
    w.append("}")
    w.append(f'opstr = tuple(_opstr.get(_i, "") for _i in range({n}))')
    w.append("del _opstr")
    w.append("")
    w.append("# instFormats: (mask, value, priority, op, opBits, args)")
    w.append("instFormats = (")
    for mask, value, prio, op, opbits, alist, comment in formats:
        targs = "(" + ", ".join(alist) + ("," if len(alist) == 1 else "") + ")"
        line = f"    ({mask}, {value}, {prio}, {op}, {opbits}, {targs}),"
        if comment:
            line += f"  # {comment}"
        w.append(line)
    w.append(")")
    w.append("")
    w.append("Op._opstr = opstr")
    w.append("for _name in OP_NAMES:")
    w.append("    setattr(Op, _name, globals()[_name])")
    w.append("del _name")
    w.append("")
    return "\n".join(w)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--armasm", type=Path, default=None, help="armasm source directory")
    ap.add_argument("-o", "--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--check", action="store_true", help="verify OUT is up to date")
    ns = ap.parse_args(argv)
    armasm = ns.armasm or default_armasm_dir()
    text = generate(armasm)
    if ns.check:
        cur = ns.out.read_text() if ns.out.exists() else None
        if cur != text:
            print(f"{ns.out} is out of date", file=sys.stderr)
            return 1
        return 0
    ns.out.parent.mkdir(parents=True, exist_ok=True)
    ns.out.write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
