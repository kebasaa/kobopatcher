#!/usr/bin/env python3
"""Differential test of kobopatcher's demangler against Go's demangle.

Usage::

    demangle_diff.py --emit-go-helper DIR    # write the Go helper's source
    demangle_diff.py --go-helper PATH [--names FILE ...] [--upstream DIR]
                     [--write-fixture OUT.txt.gz [--sample N]] [BINARY_OR_DIR ...]

The Go helper is built with (the module must be in the module cache)::

    cd DIR && GOFLAGS=-mod=mod go build -o demangle-ref .

* ``--go-helper`` is a small Go program built against
  github.com/ianlancetaylor/demangle v0.0.0-20250628045327-2d64ad6b7ec5
  that reads names line by line on stdin and prints ``OK\\t<result>``,
  ``ERR[\\t<message>]`` or ``PANIC\\t<message>`` per line, calling
  ``demangle.ToString(name)`` with no options.
* Positional arguments are ELF files or directories (searched
  recursively for ELF files).  The names of all their dynamic symbols
  (``.dynsym``, read directly from the file without any version
  decoration) are collected.  The files are only read, never executed.
* ``--names`` adds names from text files (one per line).
* ``--upstream`` adds the test inputs found in the upstream Go module
  directory (``*_test.go`` string literals starting with ``_`` and the
  inputs of the files in ``testdata/``).

All names are deduplicated and run through both implementations; the
script reports every difference (OK/ERR status, result text, and,
informationally, Go error messages vs. Python ones) and exits non-zero
if any OK/ERR status or result text differs.  A Go PANIC counts as
equal to a Python DemangleError.

With ``--write-fixture`` the Go results are written as a gzip-compressed
fixture with lines ``name\\tOK\\tresult`` or ``name\\tERR``.  With
``--sample N`` only a representative sample is written: every name whose
Go error message (with the offset stripped) is the first of its kind,
every Go panic, and a deterministic spread of the rest up to N names.
"""

from __future__ import annotations

import argparse
import gzip
import os
import re
import struct
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[2] / "src"))

from kobopatcher.patchlib._demangle import DemangleError, to_string  # noqa: E402

GO_MOD = """module demangleref

go 1.21

require github.com/ianlancetaylor/demangle v0.0.0-20250628045327-2d64ad6b7ec5
"""

GO_MAIN = r"""// Reference helper: reads names line by line on stdin and prints
// "OK\t<result>", "ERR\t<message>" or "PANIC\t<message>" (if
// demangle.ToString panics) per line, using demangle.ToString(name) with
// no options.
package main

import (
	"bufio"
	"fmt"
	"os"

	"github.com/ianlancetaylor/demangle"
)

func one(name string) (res string, err error, panicked interface{}) {
	defer func() {
		if r := recover(); r != nil {
			panicked = r
		}
	}()
	res, err = demangle.ToString(name)
	return
}

func main() {
	in := bufio.NewReaderSize(os.Stdin, 1<<20)
	out := bufio.NewWriterSize(os.Stdout, 1<<20)
	defer out.Flush()
	for {
		line, rerr := in.ReadString('\n')
		if len(line) == 0 && rerr != nil {
			break
		}
		if len(line) > 0 && line[len(line)-1] == '\n' {
			line = line[:len(line)-1]
		}
		res, err, p := one(line)
		if p != nil {
			fmt.Fprintf(out, "PANIC\t%v\n", p)
		} else if err != nil {
			fmt.Fprintf(out, "ERR\t%v\n", err)
		} else {
			fmt.Fprintf(out, "OK\t%s\n", res)
		}
		if rerr != nil {
			break
		}
	}
}
"""

# ---------------------------------------------------------------- ELF


def _is_elf(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


def dynsym_names(path: Path) -> list[bytes]:
    """Return the raw names of all .dynsym entries of an ELF file."""
    data = path.read_bytes()
    if data[:4] != b"\x7fELF":
        return []
    ei_class = data[4]
    end = "<" if data[5] == 1 else ">"
    if ei_class == 1:
        shoff = struct.unpack_from(end + "I", data, 0x20)[0]
        shentsize, shnum = struct.unpack_from(end + "HH", data, 0x2E)
        shfmt = end + "IIIIIIIIII"
        symsize = 16
    elif ei_class == 2:
        shoff = struct.unpack_from(end + "Q", data, 0x28)[0]
        shentsize, shnum = struct.unpack_from(end + "HH", data, 0x3A)
        shfmt = end + "IIQQQQIIQQ"
        symsize = 24
    else:
        return []
    sections = []
    for i in range(shnum):
        sections.append(struct.unpack_from(shfmt, data, shoff + i * shentsize))
    names = []
    for sh in sections:
        sh_type = sh[1]
        if sh_type != 11:  # SHT_DYNSYM
            continue
        off, size, link = sh[4], sh[5], sh[6]
        str_off = sections[link][4]
        entsize = sh[9] or symsize
        for j in range(size // entsize):
            st_name = struct.unpack_from(end + "I", data, off + j * entsize)[0]
            if st_name == 0:
                continue
            k = data.index(b"\0", str_off + st_name)
            names.append(data[str_off + st_name : k])
    return names


def collect_elf_names(paths: list[str]) -> tuple[set[bytes], int]:
    names: set[bytes] = set()
    nfiles = 0
    for p in paths:
        root = Path(p)
        if root.is_dir():
            files = sorted(x for x in root.rglob("*") if x.is_file() and not x.is_symlink())
        else:
            files = [root]
        for f in files:
            if not _is_elf(f):
                continue
            nfiles += 1
            names.update(dynsym_names(f))
    return names, nfiles


# ---------------------------------------------------------- upstream


_GO_STRING = re.compile(r'"((?:[^"\\\n]|\\.)*)"|`([^`]*)`')


def upstream_inputs(moddir: Path) -> set[bytes]:
    out: set[bytes] = set()
    for f in sorted(moddir.glob("*_test.go")):
        text = f.read_text(encoding="utf-8")
        for m in _GO_STRING.finditer(text):
            s = m.group(1) if m.group(1) is not None else m.group(2)
            if s.startswith("_") and "\\" not in s:
                out.add(s.encode())
    td = moddir / "testdata"
    for name in ("demangle-expected", "rust-demangle-expected"):
        p = td / name
        if p.exists():
            for line in p.read_bytes().split(b"\n"):
                if line and not line.startswith(b"#") and not line.startswith(b"--"):
                    out.add(line)
    p = td / "rust.test"
    if p.exists():
        for line in p.read_bytes().split(b"\n"):
            line = line.strip()
            if line.startswith(b"_"):
                out.add(line)
    p = td / "DemangleTestCases.inc"
    if p.exists():
        for m in re.finditer(rb'\{"((?:[^"\\]|\\.)*)"', p.read_bytes()):
            s = m.group(1)
            if b"\\" not in s:
                out.add(s)
                # cases_test.go strips one leading underscore of __Z/____Z.
                if s.startswith(b"__Z") or s.startswith(b"____Z"):
                    out.add(s[1:])
    return out


# --------------------------------------------------------------- run


def run_go(helper: str, names: list[bytes]) -> list[tuple[str, bytes]]:
    inp = b"".join(n + b"\n" for n in names)
    proc = subprocess.run([helper], input=inp, stdout=subprocess.PIPE, check=True)
    lines = proc.stdout.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    if len(lines) != len(names):
        raise SystemExit("go helper returned %d lines for %d names" % (len(lines), len(names)))
    out = []
    for line in lines:
        status, _, rest = line.partition(b"\t")
        out.append((status.decode(), rest))
    return out


def run_py(name: bytes) -> tuple[str, bytes]:
    try:
        r = to_string(name.decode("utf-8", "surrogateescape"))
    except DemangleError as e:
        return "ERR", str(e).encode("utf-8", "surrogateescape")
    except Exception as e:  # must never happen
        return "EXC", ("%s: %s" % (type(e).__name__, e)).encode("utf-8", "surrogateescape")
    return "OK", r.encode("utf-8", "surrogateescape")


_OFF = re.compile(rb" at -?\d+$")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--go-helper")
    ap.add_argument("--emit-go-helper", metavar="DIR", help="write go.mod and main.go of the Go helper to DIR")
    ap.add_argument("--names", action="append", default=[])
    ap.add_argument("--upstream")
    ap.add_argument("--write-fixture")
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--write-names", help="write the deduplicated name list here")
    ap.add_argument("paths", nargs="*")
    args = ap.parse_args()

    if args.emit_go_helper:
        d = Path(args.emit_go_helper)
        d.mkdir(parents=True, exist_ok=True)
        (d / "go.mod").write_text(GO_MOD)
        (d / "main.go").write_text(GO_MAIN)
        print("wrote %s and %s" % (d / "go.mod", d / "main.go"))
        return 0
    if not args.go_helper:
        ap.error("--go-helper is required")

    names, nfiles = collect_elf_names(args.paths)
    print("ELF files: %d, distinct dynsym names: %d" % (nfiles, len(names)))
    for nf in args.names:
        for line in Path(nf).read_bytes().split(b"\n"):
            if line:
                names.add(line)
    if args.upstream:
        up = upstream_inputs(Path(args.upstream))
        print("upstream test inputs: %d" % len(up))
        names |= up
    names.discard(b"")
    names = {n for n in names if b"\n" not in n and b"\r" not in n}
    name_list = sorted(names)
    print("total distinct names: %d" % len(name_list))
    if args.write_names:
        Path(args.write_names).write_bytes(b"".join(n + b"\n" for n in name_list))

    t0 = time.perf_counter()
    go = run_go(args.go_helper, name_list)
    t1 = time.perf_counter()
    py = [run_py(n) for n in name_list]
    t2 = time.perf_counter()
    print("go: %.1fs, python: %.1fs" % (t1 - t0, t2 - t1))

    mismatches = 0
    msg_diffs = 0
    counts: dict[str, int] = {}
    for n, (gs, gr), (ps, pr) in zip(name_list, go, py):
        counts[gs] = counts.get(gs, 0) + 1
        if gs == "OK":
            ok = ps == "OK" and pr == gr
        else:
            ok = ps == "ERR"
            if ok and gs == "ERR" and gr and gr != pr:
                msg_diffs += 1
                if msg_diffs <= 20:
                    print("ERRMSG %r\n  go: %r\n  py: %r" % (n, gr, pr))
        if not ok:
            mismatches += 1
            if mismatches <= 200:
                print("MISMATCH %r\n  go: %s %r\n  py: %s %r" % (n, gs, gr, ps, pr))
    print("results:", ", ".join("%s=%d" % kv for kv in sorted(counts.items())))
    print("compared %d names: %d mismatches, %d differing error messages" % (len(name_list), mismatches, msg_diffs))

    if args.write_fixture:
        idx = list(range(len(name_list)))
        if args.sample:
            chosen = set()
            seen_kinds: set[bytes] = set()
            for i, (gs, gr) in enumerate(go):
                if gs == "PANIC":
                    chosen.add(i)
                elif gs == "ERR":
                    kind = _OFF.sub(b"", gr)
                    if kind not in seen_kinds:
                        seen_kinds.add(kind)
                        chosen.add(i)
            rest = [i for i in idx if i not in chosen]
            want = max(0, args.sample - len(chosen))
            if want and rest:
                step = len(rest) / want
                chosen.update(rest[int(k * step)] for k in range(min(want, len(rest))))
            idx = sorted(chosen)
        with gzip.open(args.write_fixture, "wb", compresslevel=9) as f:
            for i in idx:
                gs, gr = go[i]
                if gs == "OK":
                    f.write(name_list[i] + b"\tOK\t" + gr + b"\n")
                else:
                    f.write(name_list[i] + b"\tERR\n")
        print("wrote %d entries to %s (%d bytes)" % (len(idx), args.write_fixture, os.path.getsize(args.write_fixture)))

    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
