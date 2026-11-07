#!/usr/bin/env python3
"""Differential test of kobopatcher.patchlib.css against Go kobopatch.

Compares ``is_css`` (and, with ``--tokens``, the full token stream of the
cssparse tokenizer port) with a small Go helper built against kobopatch's
``patchlib.IsCSS`` and github.com/riking/cssparse/tokenizer.

The Go helper reads one base64-encoded input per line on stdin. Invoked as
``helper iscss`` it prints ``1``/``0`` per input; invoked as ``helper tokens``
it prints one line per input in the format produced by ``go_token_line``
below. (Its source lives outside this repo; see the helper's main.go.)

Inputs:
  * every non-empty, valid-UTF-8 zlib stream (78 9c) found in the given
    binaries, decompressed with zlib.decompressobj, keeping partial output if
    the stream is corrupt (binaries are only read, never executed);
  * generated snippets: structured CSS-ish documents, random token soups,
    mutations, every prefix of a set of seed documents, NUL/CR/CRLF/FF,
    unicode and invalid escapes.

Usage:
  css_diff.py GO_HELPER [--binary PATH ...] [--count N] [--seed S]
              [--tokens] [--write-fixture PATH]
"""

from __future__ import annotations

import argparse
import base64
import json
import random
import subprocess
import sys
import zlib
from pathlib import Path

from kobopatcher.patchlib.css import Token, TokenExtraError, TokenExtraHash, TokenExtraNumeric, TokenExtraUnicodeRange, Tokenizer, is_css

REPO = Path(__file__).resolve().parents[2]
DEFAULT_BINARIES = [
    REPO / "tests/testdata/nickel",
    REPO / "tests/testdata/libnickel.so.1.0.0",
    *sorted(Path("/home/jonathan/Documents/Github/kobo_aura_h2o/archive").glob("fw-*/x/rootfs/usr/local/Kobo/nickel")),
    *sorted(Path("/home/jonathan/Documents/Github/kobo_aura_h2o/archive").glob("fw-*/x/rootfs/usr/local/Kobo/libnickel.so.1.0.0")),
]


# --------------------------------------------------------------------------
# Token stream rendering (must match the Go helper's "tokens" mode)


def _gobytes(s: str) -> bytes:
    return s.encode("utf-8", "surrogateescape")


def _extra(t: Token) -> str:
    e = t.extra
    if isinstance(e, TokenExtraHash):
        return "h" + ("true" if e.is_identifier else "false")
    if isinstance(e, TokenExtraNumeric):
        return "n" + ("true" if e.non_integer else "false") + _gobytes(e.dimension).hex()
    if isinstance(e, TokenExtraUnicodeRange):
        return "u%x-%x" % (e.start, e.end)
    if isinstance(e, TokenExtraError):
        return "e"
    return ""


def go_token_line(data: bytes) -> str:
    """Tokens returned by Next() up to and including the one where Err() is set."""
    tk = Tokenizer(data)
    out = []
    while True:
        t = tk.next()
        out.append("%d:%s:%s " % (int(t.type), _gobytes(t.value).hex(), _extra(t)))
        if tk.err() is not None:
            out.append("|" + str(tk.err()))
            break
    return "".join(out)


# --------------------------------------------------------------------------
# Inputs from binaries


def zlib_streams(path: Path):
    buf = path.read_bytes()
    mv = memoryview(buf)
    i = buf.find(b"\x78\x9c")
    while i != -1:
        d = zlib.decompressobj()
        out = bytearray()
        pos = i
        try:
            while pos < len(buf) and not d.eof:
                out += d.decompress(mv[pos : pos + 4096])
                pos += 4096
        except zlib.error:
            pass
        if out:
            try:
                bytes(out).decode("utf-8")
            except UnicodeDecodeError:
                pass
            else:
                yield i, bytes(out)
        i = buf.find(b"\x78\x9c", i + 1)


# --------------------------------------------------------------------------
# Generated inputs

ATOMS = [
    "a", "asd", "div", "-webkit-x", "--var", "_u", "é", "╯︵┻━┻", "\U0001F600", "x1", "url", "URL", "uRl",
    "{", "}", "{}", ":", "::", ";", ",", "(", ")", "[", "]", " ", "  ", "\t", "\n", "\r\n", "\r", "\f", "\x00",
    "/*", "*/", "/**/", "/*}*/", "/* c */", "/***/", "*", "*=", ".", ".5", "5.", "1.5e3", "1e", "1E+2", "-1",
    "+2", "+.4", "-.4", "-", "+", "--", "-->", "<!--", "<", "!", "!important", "%", "5%", "42px", "1\\15",
    "#", "#id", "#1a", "##", "@", "@media", "@-x", "@1", "@\\41", "|", "|=", "||", "^", "^=", "$", "$=", "~", "~=",
    '"', "'", '"str"', "'s'", '"a\\"b"', '"x\n', "'y\\\n z'", "\\", "\\41", "\\41 ", "\\000041", "\\110000",
    "\\D800", "\\0", "\\\n", "\\\\", "\\{", "\\:", "\\é", "url(", "url(a)", "url( a )", "url('a')", 'url("b" )',
    "url(a b)", "url(a'b)", "url(\\))", "url(\x01)", "url(\\\n)", "url('a\n')", "url('a' x)", "url(", "fn(", "rgb(1,2,3)",
    "U+", "U+1", "u+12-34", "U+??", "U+1?", "U+FFFFFF", "U+1234567", "u+a-", "U+-", "=", "&", ">", "~", "?",
    "\x7f", "\x0b", "\u00a0", "\u2028", "\ufeff",
]

SELECTORS = ["a", "div p", "*", ".cls", "#id", ":hover", "[x]", "[x=y]", "a:hover", "@media print", "* .a",
             "a, b", "a > b", "html|body", "-x", "\\61 b", "é", "_x"]
PROPS = ["color", "a", "-webkit-box", "--v", "font-size", "x\\:y", "é"]
VALUES = ["red", "1px", "url(a.png)", "url( 'a' )", "'s'", '"d"', "rgb(1, 2, 3)", "1e3", "#fff", "!important",
          "a:b", "{", "}", "/*c*/", "\\", "U+0-7F", "attr(x)"]


def gen_structured(r: random.Random) -> str:
    out = []
    if r.random() < 0.3:
        out.append(r.choice([" ", "\n", "\r\n", "\t", "/*x*/", "\f", "\x00", "<!--"]))
    for _ in range(r.randint(1, 4)):
        out.append(r.choice(SELECTORS))
        out.append(r.choice(["", " ", "\n", "/**/"]))
        out.append("{")
        for _ in range(r.randint(0, 4)):
            out.append(r.choice(["", " ", "\n"]))
            out.append(r.choice(PROPS))
            out.append(r.choice([":", " :", "/*c*/:", ": ", ":\r\n"]))
            out.append(r.choice(VALUES))
            out.append(r.choice([";", "", " ;", ";\n"]))
        if r.random() < 0.9:
            out.append("}")
        out.append(r.choice(["", " ", "\n", "/*}*/", "\r"]))
    return "".join(out)


def gen_soup(r: random.Random) -> str:
    return "".join(r.choice(ATOMS) for _ in range(r.randint(1, 25)))


def mutate(r: random.Random, s: bytes) -> bytes:
    b = bytearray(s)
    for _ in range(r.randint(1, 4)):
        op = r.random()
        pos = r.randint(0, len(b))
        if op < 0.4:
            b[pos:pos] = r.choice(ATOMS).encode("utf-8")
        elif op < 0.7 and b:
            del b[pos : pos + r.randint(1, 3)]
        elif op < 0.85 and pos < len(b):
            b[pos] = r.choice(b"{}:;/*\\\"'\n\r\x00\f ()-+.#@uU")
        else:
            b[pos:pos] = bytes([r.choice([0x80, 0xC3, 0xE2, 0xF0, 0xFF, 0xED])])  # invalid UTF-8
    return bytes(b)


SEEDS = [
    "", "asd", " asd", "asd {", "asd {}", "asd {asd:fgh}", " asd {asd: fgh}", " asd {asd: fgh}/*}*/",
    "    * {asd: fgh}/*}*/", " asd {asd: url(:sdf{}dfg)}", " asd {asd: fgh; dfg: asd}",
    " @media print { asd {asd: fgh; dfg: asd} }", "[prop]{dfg: asd}", ".class{dfg: asd}", "#id{dfg: asd}",
    ":hover{dfg: asd}", ":hover{dfg: asd} fgh {content: 'dfg}'}", ":hover{dfg: asd; fgh: asd;} fgh {dfgdfgdfg}",
    "function asd() { fgh(); }", "<html><span style='asd: dfg'>dfgdfg</span></html>",
    "<html><span style='asd: dfg'>dfgdfg</span><style>dfg {asd: dfg} fgh {dfg: ert; fgh: asd}</style></html>",
    "a{b:c}", "a{b:c}\n", "a{b:c} ", "a{b:c}x", "a{b:c}/**/", "a{b:c}/*", "a {b /*c*/ : d}", "/*c*/a{b:c}",
    "\x00a{b:c}", "a{b:c}\x00", "a{\r\nb:c\r}", "a{b\f:c}", "\fa{b:c}", "u+1{b:c}", "U+1-2{b:c}", "a{b:url(x)}",
    "a{b:url(x}", "a{b:'x}", "a{b:\"x\n}", "*{b:c}", "*={b:c}", ".5{b:c}", ".a{b:c}", "@x{b:c}", "#1{b:c}",
    "[{b:c}]", "a{b:c;d:e}f{}", "a{b:c}f{}", "a{b:c}{}", "\\61{b:c}", "a\\{b:c}", "a{b\\:c}", "-a{b:c}", "--a{b:c}",
    "a { b : c }", "a{b:c}}", "a{{b:c}", "a{b:{c:d}}", "a:b{c:d}", "é{ü:ö}",
]


def generated_inputs(seed: int, count: int) -> list[bytes]:
    r = random.Random(seed)
    seen: set[bytes] = set()
    out: list[bytes] = []

    def add(b: bytes) -> None:
        if b not in seen:
            seen.add(b)
            out.append(b)

    for s in SEEDS:
        b = s.encode("utf-8")
        for k in range(len(b) + 1):
            add(b[:k])
    for _ in range(count):
        x = r.random()
        if x < 0.35:
            b = gen_structured(r).encode("utf-8")
        elif x < 0.65:
            b = gen_soup(r).encode("utf-8")
        elif x < 0.85:
            b = mutate(r, r.choice(SEEDS).encode("utf-8"))
        else:
            b = mutate(r, gen_structured(r).encode("utf-8"))
        add(b)
        if r.random() < 0.05:
            for k in range(len(b) + 1):  # every prefix
                add(b[:k])
    return out


# --------------------------------------------------------------------------


def run_go(helper: str, mode: str, inputs: list[bytes]) -> list[str]:
    stdin = b"".join(base64.b64encode(b) + b"\n" for b in inputs)
    res = subprocess.run([helper, mode], input=stdin, stdout=subprocess.PIPE, check=True)
    lines = res.stdout.decode("utf-8").split("\n")
    assert lines[-1] == ""
    lines = lines[:-1]
    assert len(lines) == len(inputs), (len(lines), len(inputs))
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("go_helper", help="path to the built Go css-ref helper")
    ap.add_argument("--binary", action="append", type=Path, help="binary to scan for zlib streams (repeatable)")
    ap.add_argument("--no-binaries", action="store_true", help="skip zlib streams from binaries")
    ap.add_argument("--count", type=int, default=5000, help="number of random generated snippets")
    ap.add_argument("--seed", type=int, default=20180325)
    ap.add_argument("--mutations", type=int, default=3, help="mutated variants per real stream")
    ap.add_argument("--prefix-streams", type=int, default=6, help="real streams to cut at every position")
    ap.add_argument("--tokens", action="store_true", help="also compare full token streams")
    ap.add_argument("--write-fixture", type=Path, help="write generated cases + Go answers as JSON")
    ap.add_argument("--fixture-max", type=int, default=4000, help="max cases in the fixture")
    ap.add_argument("--fixture-max-len", type=int, default=160, help="max input length in the fixture")
    args = ap.parse_args()

    binary_inputs: list[tuple[str, bytes]] = []
    if not args.no_binaries:
        for p in args.binary or DEFAULT_BINARIES:
            if not p.exists():
                print(f"skip missing {p}", file=sys.stderr)
                continue
            n = 0
            for off, data in zlib_streams(p):
                binary_inputs.append((f"{p}@0x{off:x}", data))
                n += 1
            print(f"{p}: {n} valid-UTF-8 zlib streams", file=sys.stderr)
    # Deduplicate binary streams (the same CSS recurs across firmware versions).
    uniq: dict[bytes, str] = {}
    for name, data in binary_inputs:
        uniq.setdefault(data, name)
    # Variants of the real streams: a few mutations of each, and every prefix
    # (up to 4 KiB) of a handful of them.
    r = random.Random(args.seed)
    derived: dict[bytes, str] = {}
    for data, name in uniq.items():
        for k in range(args.mutations):
            derived.setdefault(mutate(r, data), f"{name}~mut{k}")
    for data, name in r.sample(sorted(uniq.items()), min(args.prefix_streams, len(uniq))):
        for k in range(min(len(data), 4096) + 1):
            derived.setdefault(data[:k], f"{name}[:{k}]")
    for d in uniq:
        derived.pop(d, None)
    gen = generated_inputs(args.seed, args.count)
    names = list(uniq.values()) + list(derived.values()) + [f"gen#{i}" for i in range(len(gen))]
    inputs = list(uniq.keys()) + list(derived.keys()) + gen
    nreal = len(uniq) + len(derived)
    print(
        f"inputs: {len(uniq)} unique binary streams ({len(binary_inputs)} total), "
        f"{len(derived)} derived from them, {len(gen)} generated",
        file=sys.stderr,
    )

    mismatches = 0
    go_iscss = run_go(args.go_helper, "iscss", inputs)
    n_true = n_real_true = 0
    for idx, (name, data, g) in enumerate(zip(names, inputs, go_iscss)):
        p = "1" if is_css(data) else "0"
        n_true += p == "1"
        n_real_true += p == "1" and idx < len(uniq)
        if p != g:
            mismatches += 1
            print(f"ISCSS MISMATCH {name}: go={g} py={p} input={data[:200]!r}")
    print(
        f"is_css: {len(inputs)} inputs, {n_true} CSS ({n_real_true} of {len(uniq)} real streams), "
        f"{mismatches} mismatches",
        file=sys.stderr,
    )

    if args.tokens:
        tmis = 0
        go_tok = run_go(args.go_helper, "tokens", inputs)
        for name, data, g in zip(names, inputs, go_tok):
            p = go_token_line(data)
            if p != g:
                tmis += 1
                if tmis <= 20:
                    print(f"TOKEN MISMATCH {name}: input={data[:200]!r}\n  go={g[:400]}\n  py={p[:400]}")
        print(f"tokens: {len(inputs)} inputs, {tmis} mismatches", file=sys.stderr)
        mismatches += tmis

    if args.write_fixture:
        r = random.Random(args.seed)
        cand = [(d, g) for d, g in zip(gen, go_iscss[nreal:]) if len(d) <= args.fixture_max_len]
        trues = [c for c in cand if c[1] == "1"]
        falses = [c for c in cand if c[1] == "0"]
        half = args.fixture_max // 2
        pick = r.sample(trues, min(half, len(trues)))
        pick += r.sample(falses, min(args.fixture_max - len(pick), len(falses)))
        # Keep the seed documents and all their prefixes deterministic and complete.
        seedset = {s.encode("utf-8")[:k] for s in SEEDS for k in range(len(s.encode("utf-8")) + 1)}
        have = {d for d, _ in pick}
        pick += [c for c in cand if c[0] in seedset and c[0] not in have]
        pick.sort(key=lambda c: (len(c[0]), c[0]))
        tok = run_go(args.go_helper, "tokens", [d for d, _ in pick])
        cases = [
            {"in": base64.b64encode(d).decode("ascii"), "is_css": g == "1", "tokens": t}
            for (d, g), t in zip(pick, tok)
        ]
        doc = {
            "description": "Generated IsCSS/tokenizer cases with answers from Go kobopatch patchlib.IsCSS and "
            "riking/cssparse tokenizer (tools/parity/css_diff.py). 'in' is base64; 'tokens' is the "
            "token stream in css_diff.go_token_line format.",
            "seed": args.seed,
            "cases": cases,
        }
        args.write_fixture.write_text(json.dumps(doc, indent=0, ensure_ascii=True) + "\n")
        print(f"wrote {len(cases)} cases to {args.write_fixture}", file=sys.stderr)

    print("OK" if mismatches == 0 else f"FAIL: {mismatches} mismatches")
    return 0 if mismatches == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
