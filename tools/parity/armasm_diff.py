#!/usr/bin/env python3
"""Differential test of kobopatcher.patchlib._armasm against Go's armasm.

Compares ``canonical()`` (src/kobopatcher/patchlib/_armasm/canon.py) with the
output of a small Go reference helper built against
rsc.io/arm@v0.0.0-20150420010332-9c32f2193064, which reads hex byte strings
(memory order, one per line) on stdin and prints, per line::

    <hex>\\tERR\\t<err>
    <hex>\\tOK\\t<Op>\\t<int(Op)>\\t<Len>\\t<%#x Enc>\\t<%T=%#v of each of the 4 Args, |-joined>\\t<Inst.String()>\\t<GNUSyntax>\\t<%+q of []Inst{inst}>

The helper source is printed by ``--print-helper`` (put it in a directory
with a go.mod requiring rsc.io/arm at that version and ``go build``).

Corpora (any combination)::

    --random N [--seed S]       N uniformly random 32-bit words
    --formats K                 K words per instFormat (bits outside the
                                format mask random), plus 1-bit near misses
    --decode-txt                inputs of armasm's testdata/decode.txt
    --elf PATH ...              .plt of ELF files (dirs are walked; symlinks
                                are skipped); every 2-byte offset
    --tar PATH ...              .plt of ELF members of (xz) tarballs, read
                                in memory; every 2-byte offset

ELF files are only parsed, never executed.  ``--write-fixture OUT`` writes
a gzip file of Go's lines for a deterministic sample of the inputs (used by
tests/test_armasm.py).
"""

from __future__ import annotations

import argparse
import gzip
import os
import random
import stat
import struct
import subprocess
import sys
import tarfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from kobopatcher.patchlib._armasm import canon  # noqa: E402
from kobopatcher.patchlib._armasm import tables  # noqa: E402

DECODE_TXT = REPO / "tests" / "testdata" / "armasm_cases_decode.txt"

GO_HELPER_MOD = """module armasmref

go 1.16

require rsc.io/arm v0.0.0-20150420010332-9c32f2193064
"""

GO_HELPER_SRC = r'''// armasm-ref: reads hex-encoded byte strings (one per line, memory order) on
// stdin and prints a canonical description of armasm.Decode(bytes, ModeARM).
package main

import (
	"bufio"
	"encoding/hex"
	"flag"
	"fmt"
	"os"
	"strings"

	"rsc.io/arm/armasm"
)

func main() {
	mode := flag.Int("mode", int(armasm.ModeARM), "decode mode")
	flag.Parse()
	sc := bufio.NewScanner(os.Stdin)
	sc.Buffer(make([]byte, 1<<20), 1<<20)
	w := bufio.NewWriterSize(os.Stdout, 1<<20)
	defer w.Flush()
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" {
			continue
		}
		b, err := hex.DecodeString(line)
		if err != nil {
			fmt.Fprintf(os.Stderr, "bad hex %q\n", line)
			os.Exit(2)
		}
		inst, err := armasm.Decode(b, armasm.Mode(*mode))
		if err != nil {
			fmt.Fprintf(w, "%s\tERR\t%s\n", line, err)
			continue
		}
		var args []string
		for _, a := range inst.Args {
			args = append(args, fmt.Sprintf("%T=%#v", a, a))
		}
		fmt.Fprintf(w, "%s\tOK\t%s\t%d\t%d\t%#x\t%s\t%s\t%s\t%+q\n", line, inst.Op, int(inst.Op), inst.Len, inst.Enc,
			strings.Join(args, "|"), inst, armasm.GNUSyntax(inst), []armasm.Inst{inst})
	}
	if err := sc.Err(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
}
'''


# ---------------------------------------------------------------------------
# ELF .plt extraction (ELF32 little-endian ARM only).

def plt_section(data: bytes) -> bytes | None:
    if len(data) < 52 or data[:4] != b"\x7fELF" or data[4] != 1 or data[5] != 1:
        return None
    (e_machine,) = struct.unpack_from("<H", data, 18)
    if e_machine != 40:  # EM_ARM
        return None
    (e_shoff,) = struct.unpack_from("<I", data, 32)
    e_shentsize, e_shnum, e_shstrndx = struct.unpack_from("<HHH", data, 46)
    if e_shoff == 0 or e_shnum == 0 or e_shstrndx >= e_shnum:
        return None

    def sh(i):
        return struct.unpack_from("<IIIIIIIIII", data, e_shoff + i * e_shentsize)

    strtab = sh(e_shstrndx)
    stroff = strtab[4]
    for i in range(e_shnum):
        name, typ, _flags, _addr, off, size = sh(i)[:6]
        end = data.index(b"\0", stroff + name)
        if data[stroff + name:end] == b".plt" and typ != 8:  # not SHT_NOBITS
            return data[off:off + size]
    return None


def plt_inputs(plt: bytes) -> list[str]:
    """Every aligned and 2-byte-offset position (including the short tail)."""
    return [plt[off:off + 4].hex() for off in range(0, len(plt), 2)]


def walk_elf(paths):
    for p in paths:
        p = Path(p)
        if p.is_file() and not p.is_symlink():
            yield p
            continue
        for root, _dirs, files in os.walk(p):
            for f in files:
                fp = Path(root) / f
                try:
                    st = fp.lstat()
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode) or st.st_size < 52:
                    continue
                yield fp


def elf_corpus(paths):
    inputs = []
    nfiles = 0
    for fp in walk_elf(paths):
        try:
            with open(fp, "rb") as f:
                if f.read(4) != b"\x7fELF":
                    continue
                data = b"\x7fELF" + f.read()
        except OSError:
            continue
        plt = plt_section(data)
        if plt is None:
            continue
        nfiles += 1
        inputs.extend(plt_inputs(plt))
    return nfiles, inputs


def tar_corpus(paths):
    inputs = []
    nfiles = 0
    for p in paths:
        with tarfile.open(p, "r:*") as tf:
            for m in tf:
                if not m.isreg():
                    continue
                f = tf.extractfile(m)
                if f is None:
                    continue
                data = f.read()
                plt = plt_section(data)
                if plt is None:
                    continue
                nfiles += 1
                inputs.extend(plt_inputs(plt))
    return nfiles, inputs


def random_corpus(n, seed):
    rng = random.Random(seed)
    return [rng.getrandbits(32).to_bytes(4, "little").hex() for _ in range(n)]


def formats_corpus(k, seed):
    rng = random.Random(seed)
    out = []
    for mask, value, _prio, _op, _opbits, _args in tables.instFormats:
        m = mask | 0xF0000000
        for i in range(k):
            if value >> 28 == 0xF:
                top = 0xF
            else:
                top = rng.randrange(15)
            w = ((value | top << 28) & m) | (rng.getrandbits(32) & ~m & 0xFFFFFFFF)
            out.append(w.to_bytes(4, "little").hex())
            # near miss: flip one bit inside the mask
            bits = [b for b in range(32) if m >> b & 1]
            w2 = w ^ (1 << rng.choice(bits))
            out.append(w2.to_bytes(4, "little").hex())
    return out


def decode_txt_corpus():
    out = []
    for line in DECODE_TXT.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line.split("\t", 1)[0].replace("|", ""))
    return out


# ---------------------------------------------------------------------------

def run_go(helper: str, inputs: list[str], chunk: int = 200_000) -> list[str]:
    out = []
    for i in range(0, len(inputs), chunk):
        part = inputs[i:i + chunk]
        res = subprocess.run([helper], input="\n".join(part) + "\n", capture_output=True,
                             text=True, check=True)
        lines = res.stdout.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        if len(lines) != len(part):
            raise SystemExit(f"go helper returned {len(lines)} lines for {len(part)} inputs")
        out.extend(lines)
    return out


def compare(name: str, helper: str, inputs: list[str], max_show: int = 20):
    uniq = list(dict.fromkeys(inputs))
    t0 = time.perf_counter()
    go = run_go(helper, uniq)
    t1 = time.perf_counter()
    py = [canon.canonical(h) for h in uniq]
    t2 = time.perf_counter()
    bad = [(g, p) for g, p in zip(go, py) if g != p]
    nok = sum(1 for g in go if "\tOK\t" in g)
    print(f"[{name}] inputs={len(inputs)} unique={len(uniq)} decoded={nok} "
          f"errors={len(uniq) - nok} mismatches={len(bad)} "
          f"(go {t1 - t0:.1f}s, python {t2 - t1:.1f}s)")
    for g, p in bad[:max_show]:
        print(f"  go: {g}\n  py: {p}")
    return uniq, go, len(bad)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--go-helper", help="path to the built Go reference helper")
    ap.add_argument("--print-helper", action="store_true", help="print the Go helper go.mod and main.go")
    ap.add_argument("--random", type=int, default=0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--formats", type=int, default=0)
    ap.add_argument("--decode-txt", action="store_true")
    ap.add_argument("--elf", nargs="*", default=[])
    ap.add_argument("--tar", nargs="*", default=[])
    ap.add_argument("--write-fixture", type=Path)
    ap.add_argument("--fixture-size", type=int, default=20000)
    ns = ap.parse_args(argv)

    if ns.print_helper:
        print("// go.mod\n" + GO_HELPER_MOD + "\n// main.go\n" + GO_HELPER_SRC)
        return 0
    if not ns.go_helper:
        ap.error("--go-helper is required")

    total_bad = 0
    pools: dict[str, tuple[list[str], list[str]]] = {}

    def run(name, inputs):
        nonlocal total_bad
        uniq, go, nbad = compare(name, ns.go_helper, inputs)
        total_bad += nbad
        pools[name] = (uniq, go)

    if ns.decode_txt:
        run("decode.txt", decode_txt_corpus())
    if ns.formats:
        run(f"formats x{ns.formats}", formats_corpus(ns.formats, ns.seed))
    if ns.elf:
        nfiles, inputs = elf_corpus(ns.elf)
        run(f"elf ({nfiles} files with .plt)", inputs)
    if ns.tar:
        nfiles, inputs = tar_corpus(ns.tar)
        run(f"tar ({nfiles} files with .plt)", inputs)
    if ns.random:
        run(f"random {ns.random} seed={ns.seed}", random_corpus(ns.random, ns.seed))

    # Error-path checks: other modes.
    for mode in (0, 2):
        res = subprocess.run([ns.go_helper, f"-mode={mode}"], input="04402de5\n", capture_output=True,
                             text=True, check=True)
        py = canon.canonical("04402de5", mode)
        ok = res.stdout.rstrip("\n") == py
        total_bad += 0 if ok else 1
        print(f"[mode {mode}] {'ok' if ok else 'MISMATCH go=%r py=%r' % (res.stdout, py)}")

    if ns.write_fixture:
        rng = random.Random(ns.seed)
        lines: dict[str, str] = {}
        budget = ns.fixture_size
        # Take everything from small pools, sample the large ones evenly.
        names = sorted(pools, key=lambda n: len(pools[n][0]))
        for idx, name in enumerate(names):
            uniq, go = pools[name]
            share = (budget - len(lines)) // (len(names) - idx)
            pairs = [(h, g) for h, g in zip(uniq, go) if h not in lines]
            if len(pairs) > share:
                pairs = rng.sample(pairs, share)
            for h, g in pairs:
                lines[h] = g
        with gzip.open(ns.write_fixture, "wt", compresslevel=9) as f:
            f.write("# Go rsc.io/arm/armasm@v0.0.0-20150420010332-9c32f2193064 canonical decode output\n")
            f.write("# generated by tools/parity/armasm_diff.py; see canon.py for the format\n")
            for g in sorted(lines.values()):
                f.write(g + "\n")
        print(f"wrote {len(lines)} cases to {ns.write_fixture}")

    print(f"TOTAL mismatches: {total_bad}")
    return 1 if total_bad else 0


if __name__ == "__main__":
    sys.exit(main())
