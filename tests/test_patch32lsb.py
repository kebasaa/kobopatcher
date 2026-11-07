"""Ported from kobopatch patchfile/patch32lsb/patch32lsb_test.go, plus tests
for every instruction, the parse and validation errors, SetEnabled, the
kobopatcher fixes (in both modes) and a differential fixture generated
against Go by tools/parity/p32_diff.py."""

import base64
import contextlib
import importlib.util
import io
import json
import math
import struct
import zlib
from pathlib import Path

import pytest

from kobopatcher import patchfile
from kobopatcher.compat import go_compat_mode
from kobopatcher.errors import GoPanic, KobopatcherError
from kobopatcher.patchfile import patch32lsb as P
from kobopatcher.patchlib.patcher import Patcher, PatchError

REPO = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "testdata" / "p32_cases.json"


def unescape_b(s: bytes) -> bytes:
    return P._unescape(s)


# --- ported from patch32lsb_test.go ---------------------------------------------


@pytest.mark.parametrize(
    "inp,exp",
    [
        (b"test", b"test"),
        (b"test\\0", b"test\x00"),
        (b"test\\0\\xcc", b"test\x00\xcc"),
        (b"test\\0\\n\\t\\v\\r\\xcc", b"test\x00\n\t\v\r\xcc"),
        (b"test\\0\\n\\t\\v\\r\\xcc\\\"\\'\\`", b"test\x00\n\t\v\r\xcc\"'`"),
        ("ÉÀÇ".encode(), "ÉÀÇ".encode()),
    ],
)
def test_unescape(inp, exp):
    assert unescape_b(b"`" + inp + b"`") == exp
    # the str API (Go strings are bytes: surrogateescape)
    assert P.unescape("`" + inp.decode("utf-8", "surrogateescape") + "`").encode("utf-8", "surrogateescape") == exp


@pytest.mark.parametrize(
    "inp,exp",
    [
        (b"test", b"test"),
        (b"test\\0", b"test\x00"),
        (b'test\\0"\\xcc', b'test\x00"\xcc'),
        (b"test\\0\\n\\t\\v\\r\\xcc", b"test\x00\n\t\v\r\xcc"),
        (b"test\\0\\n\\t\\v\\r\\xcc\\\"\\'\\`", b"test\x00\n\t\v\r\xcc\"'`"),
    ],
)
def test_unescape_first(inp, exp):
    u, r = P._unescape_first(b"`" + inp + b"`" + b"dfgdfg dfgdfgd fgdf dfg `dfg`")
    assert u == exp
    assert r == b"dfgdfg dfgdfgd fgdf dfg `dfg`"
    us, rs = P.unescape_first("`" + inp.decode("utf-8", "surrogateescape") + "`dfgdfg dfgdfgd fgdf dfg `dfg`")
    assert us.encode("utf-8", "surrogateescape") == exp and rs == "dfgdfg dfgdfgd fgdf dfg `dfg`"


# --- unescape errors -------------------------------------------------------------


@pytest.mark.parametrize(
    "inp,err",
    [
        (b"", "string not wrapped in backticks"),
        (b"abc", "string not wrapped in backticks"),
        (b"`abc", "string not wrapped in backticks"),
        (b"abc`", "string not wrapped in backticks"),
        (b"`\\`", "string not wrapped in backticks"),
        (b"`abc\\`", "string not wrapped in backticks"),
        (b"`\\q`", "unknown escape q"),
        (b"`\\\xcc`", "unknown escape Ì"),  # Go: string(byte) is the rune U+00CC
        (b"`\\xzz`", "no hex data for %x string"),
        (b"`\\x0x`", "illegal hex digit"),
        (b"`\\x a`", "unexpected EOF"),
        (b"`\\x  `", "EOF"),
        (b"`\\x\xc2\xa0`", "EOF"),  # a no-break space is skipped like a space
        (b"`\\xa\xc3`", "illegal hex digit"),
    ],
)
def test_unescape_errors(both_modes, inp, err):
    with pytest.raises(P._UnescapeError) as e:
        unescape_b(inp)
    assert str(e.value) == err


def test_unescape_double_backslash_before_backtick():
    assert unescape_b(b"`a\\\\`") == b"a\\"
    assert unescape_b(b"``") == b""
    assert unescape_b(b"`\\``") == b"`"


@pytest.mark.parametrize(
    "inp,panic,err",
    [
        (b"`", "runtime error: index out of range [-1]", "string not wrapped in backticks"),
        (b"`\\\\\\`", "runtime error: index out of range [1] with length 1", "incomplete escape sequence at end of string"),
        (b"`\\x`", "runtime error: slice bounds out of range [:4] with length 2", "incomplete \\x escape sequence"),
        (b"`\\x4`", "runtime error: slice bounds out of range [:4] with length 3", "incomplete \\x escape sequence"),
    ],
)
def test_unescape_panics(both_modes, inp, panic, err):
    if both_modes:
        with pytest.raises(GoPanic) as e:
            unescape_b(inp)
        assert str(e.value) == panic
    else:
        with pytest.raises(P._UnescapeError) as e:
            unescape_b(inp)
        assert str(e.value) == err


def test_unescape_first_errors(both_modes):
    for s in (b"", b"`", b"`abc", b"abc`", b"`\\q`x`"):
        with pytest.raises(P._UnescapeError) as e:
            P._unescape_first(s)
        assert str(e.value) == "could not find valid string"
    # the shortest valid prefix wins
    assert P._unescape_first(b"`a`b`") == (b"a", b"b`")
    assert P._unescape_first(b"`a\\`b`c") == (b"a`b", b"c")


def test_unescape_first_panic(both_modes):
    # Go panics on the prefix "`\x`"; kobopatcher treats it as an invalid prefix
    if both_modes:
        with pytest.raises(GoPanic):
            P._unescape_first(b"`\\x`abc`")
    else:
        with pytest.raises(P._UnescapeError) as e:
            P._unescape_first(b"`\\x`abc`")
        assert str(e.value) == "could not find valid string"


# --- Sscanf ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "inp,fmt,kinds,exp",
    [
        (b"1A,00ff,AB", "%x,%x,%x", ["int32", "bytes", "bytes"], [0x1A, b"\x00\xff", b"\xab"]),
        (b"-10,00,01", "%x,%x,%x", ["int32", "bytes", "bytes"], [-16, b"\x00", b"\x01"]),
        (b"+10,00,\t01", "%x,%x,%x", ["int32", "bytes", "bytes"], [16, b"\x00", b"\x01"]),
        (b"10 junk", "%x", ["int32"], [16]),  # trailing input is ignored
        (b"  7fffffff", "%x", ["int32"], [0x7FFFFFFF]),
        (b"1,1.5,-2e3", "%x,%f,%f", ["int32", "float64", "float64"], [1, 1.5, -2000.0]),
        (b"1,0x1.8P1,1p4", "%x,%f,%f", ["int32", "float64", "float64"], [1, 3.0, 16.0]),
        (b"1,0x1.8p1,2", "%x,%f,%f", ["int32", "float64", "float64"], [1, 3.0, 2.0]),
        (b"1,1_0.5,inf", "%x,%f,%f", ["int32", "float64", "float64"], [1, 10.5, math.inf]),
        (b"0,0,255", "%x,%d,%d", ["int32", "uint8", "uint8"], [0, 0, 255]),
    ],
)
def test_sscanf_values(inp, fmt, kinds, exp):
    assert P._sscanf(inp, fmt, kinds) == exp


@pytest.mark.parametrize(
    "inp,fmt,kinds,err",
    [
        (b"", "%x", ["int32"], "EOF"),
        (b"   ", "%x", ["int32"], "EOF"),
        (b"-", "%x", ["int32"], "EOF"),
        (b"zz", "%x", ["int32"], "expected integer"),
        (b"FFFFFFF0", "%x", ["int32"], "integer overflow on token FFFFFFF0"),
        (b"-80000001", "%x", ["int32"], "integer overflow on token -80000001"),
        (b"123456789ABCDEF012", "%x", ["int32"], 'strconv.ParseInt: parsing "123456789ABCDEF012": value out of range'),
        (b"1", "%x,%x,%x", ["int32", "bytes", "bytes"], "unexpected EOF"),
        (b"1;2", "%x,%x,%x", ["int32", "bytes", "bytes"], "input does not match format"),
        (b"0x1,2,3", "%x,%x,%x", ["int32", "bytes", "bytes"], "input does not match format"),
        (b"1,012,34", "%x,%x,%x", ["int32", "bytes", "bytes"], "illegal hex digit"),
        (b"1,01,345", "%x,%x,%x", ["int32", "bytes", "bytes"], "unexpected EOF"),
        (b"1,,34", "%x,%x,%x", ["int32", "bytes", "bytes"], "no hex data for %x string"),
        (b"1,1.5,x", "%x,%f,%f", ["int32", "float64", "float64"], 'strconv.ParseFloat: parsing "": invalid syntax'),
        (b"1,1e400,2", "%x,%f,%f", ["int32", "float64", "float64"], 'strconv.ParseFloat: parsing "1e400": value out of range'),
        (b"1,1p,2", "%x,%f,%f", ["int32", "float64", "float64"], 'strconv.Atoi: parsing "1p": invalid syntax'),
        (b"1,1_,2", "%x,%f,%f", ["int32", "float64", "float64"], 'strconv.ParseFloat: parsing "1_": invalid syntax'),
        (b"1,Infinity,2", "%x,%f,%f", ["int32", "float64", "float64"], "input does not match format"),
        (b"1,256,1", "%x,%d,%d", ["int32", "uint8", "uint8"], "unsigned integer overflow on token 256"),
        (b"1,-1,1", "%x,%d,%d", ["int32", "uint8", "uint8"], "expected integer"),
        (b"1,99999999999999999999,1", "%x,%d,%d", ["int32", "uint8", "uint8"], 'strconv.ParseUint: parsing "99999999999999999999": value out of range'),
    ],
)
def test_sscanf_errors(inp, fmt, kinds, err):
    with pytest.raises(P._ScanError) as e:
        P._sscanf(inp, fmt, kinds)
    assert str(e.value) == err


def test_sscanf_hex_float_quirk():
    # convertFloat only handles a lowercase 'p' itself for non-hex numbers
    assert P._sscanf(b"0,0x1p-2,1", "%x,%f,%f", ["int32", "float64", "float64"]) == [0, 0.25, 1.0]


def test_sscanf_nan_is_go_nan():
    v = P._sscanf(b"0,NaN,nan", "%x,%f,%f", ["int32", "float64", "float64"])
    for f in v[1:]:
        assert struct.pack("<d", f) == struct.pack("<Q", 0x7FF8000000000001)


def test_sscanf_twos_complement():
    assert P._sscanf(b"FFFFFFF0", "%x", ["int32"], twos=True) == [-16]
    assert P._sscanf(b"80000000", "%x", ["int32"], twos=True) == [-(1 << 31)]
    assert P._sscanf(b"7FFFFFFF", "%x", ["int32"], twos=True) == [0x7FFFFFFF]
    for bad in (b"100000000", b"+FFFFFFF0", b"-FFFFFFF0"):
        with pytest.raises(P._ScanError) as e:
            P._sscanf(bad, "%x", ["int32"], twos=True)
        assert str(e.value).startswith("integer overflow on token")


@pytest.mark.parametrize(
    "f,s",
    [(1.0, "1"), (1.5, "1.5"), (1e20, "1e+20"), (123456789.0, "1.23456789e+08"), (100000.0, "100000"), (1e6, "1e+06"),
     (1e-5, "1e-05"), (0.0001, "0.0001"), (-0.0, "-0"), (math.inf, "+Inf"), (-math.inf, "-Inf"), (math.nan, "NaN"), (5e-324, "5e-324")],
)
def test_go_float(f, s):
    assert P.go_float(f) == s


# --- parsing ----------------------------------------------------------------------------

SYNTH = b"HEAD\x00Hello World\x00" + bytes(range(16)) + struct.pack("<d", 1.5) + b"\x2a" + b"tail\x00"


def pfile(*lines, nl="\n") -> bytes:
    return nl.join(lines).encode("utf-8", "surrogateescape") + nl.encode()


def block(name="a", enable="yes", *insts):
    return ["<Patch>", "patch_name = `%s`" % name, "patch_enable = `%s`" % enable, *insts, "</Patch>"]


def insts(ps, name):
    return [(i.kind, i.value) for i in ps.patches[name]]


def test_parse_every_instruction(both_modes):
    ps = P.parse(
        pfile(
            "# header comment",
            "<Patch>",
            "patch_name = `All`",
            "patch_enable = `no`",
            "patch_group = `Group`",
            "## a comment",
            "",
            "base_address = 1A",
            "find_base_address = `Hello\\0`",
            "replace_bytes = 4, 48 65, 4A 65",
            "replace_float = 10, 1.5, -2",
            "replace_int = 0, 42, 7",
            "replace_string = 2, `ll`, `LL`",
            "find_zlib = `body {`",
            "find_zlib_hash = `%s`" % ("a" * 40),
            "replace_zlib = 0, `a: b`, `a:c`",
            "find_replace_string = `World`, `Earth`",
            "</Patch>",
        )
    )
    assert ps.names() == ["All"]
    assert insts(ps, "All") == [
        ("Enabled", False),
        ("PatchGroup", "Group"),
        ("Comment", "a comment"),
        ("Comment", ""),
        ("BaseAddress", 0x1A),
        ("FindBaseAddress", "Hello\x00"),
        ("ReplaceBytes", (4, b"He", b"Je")),
        ("ReplaceFloat", (0x10, 1.5, -2.0)),
        ("ReplaceInt", (0, 42, 7)),
        ("ReplaceString", (2, "ll", "LL")),
        ("FindZlib", "body {"),
        ("FindZlibHash", "a" * 40),
        ("ReplaceZlib", (0, "a: b", "a:c")),
        ("FindReplaceString", ("World", "Earth")),
    ]


def test_parse_syntax_variants(both_modes):
    # tags in any case, CRLF, surrounding (Unicode) spaces, extra spaces around =,
    # keys in any case (Go's ToLower maps U+0130 to i), comments and lines outside
    # of blocks are dropped
    ps = P.parse(
        pfile(
            "# comment outside",
            "replace_bytes = 0, 00, 00",
            "  <PATCH>　",
            "PATCH_NAME = `a`",
            "patch_enable  = `yes`",
            " fİnd_base_address = `x`\t",
            "</patch>",
            nl="\r\n",
        )
    )
    assert insts(ps, "a") == [("Enabled", True), ("FindBaseAddress", "x")]
    # the = takes all spaces before it but only one after it
    ps = P.parse(pfile(*block("a", "yes", "patch_group   = `g`")))
    assert insts(ps, "a")[1] == ("PatchGroup", "g")
    with pytest.raises(P.Patch32lsbError) as e:
        P.parse(pfile(*block("a", "yes", "patch_group =  `g`")))
    assert str(e.value) == "line 4: error unescaping patch_group: string not wrapped in backticks"


def test_parse_unclosed_block_is_dropped(both_modes):
    ps = P.parse(pfile(*block("a"), "<Patch>", "patch_name = `b`", "patch_enable = `no`"))
    assert ps.names() == ["a"]


def test_parse_non_utf8(both_modes):
    ps = P.parse(b"<Patch>\npatch_name = `\xff\\xfe`\npatch_enable = `no`\nfind_base_address = `\\xc3\\xa9`\n</Patch>\n")
    (name,) = ps.names()
    assert name.encode("utf-8", "surrogateescape") == b"\xff\xfe"
    assert insts(ps, name)[1] == ("FindBaseAddress", "é")


@pytest.mark.parametrize(
    "lines,err",
    [
        (["# patch_group = `x`"], "line 1: patch_group should not be a comment"),
        (["#patch_group"], "line 1: patch_group should not be a comment"),
        (["<Patch>", "<Patch>"], "line 2: unexpected <Patch> (already in patch)"),
        (["</Patch>"], "line 1: unexpected </Patch> (not in patch)"),
        (["<Patch>", "</Patch>"], "line 2: no patch_name for patch"),
        (block("a") + block("a"), "line 8: duplicate patch with name 'a'"),
        (["patch_name=`a`"], "line 1: bad instruction: no equals sign"),
        (["patch_name =`a`"], "line 1: bad instruction: no equals sign"),
        (["patch_name\t=\t`a`"], "line 1: bad instruction: no equals sign"),
        (["patch_name = a"], "line 1: error unescaping patch_name: string not wrapped in backticks"),
        (["patch_group = `\\q`"], "line 1: error unescaping patch_group: unknown escape q"),
        (["patch_enable = `yes`"], "line 1: patch_enable set before patch_name"),
        (["patch_name = `a`", "patch_enable = `YES`"], "line 2: unexpected patch_enable value '`YES`' (should be yes or no)"),
        (["patch_name = `a`", "patch_enable = yes"], "line 2: unexpected patch_enable value 'yes' (should be yes or no)"),
        (["replace_bytes = 1, 2"], "line 1: replace_bytes malformed: unexpected EOF"),
        (["replace_bytes = 1, 012, 34"], "line 1: replace_bytes malformed: illegal hex digit"),
        (["replace_bytes = 0x1, 01, 02"], "line 1: replace_bytes malformed: input does not match format"),
        (["base_address = "], "line 1: bad instruction: no equals sign"),
        (["base_address = zz"], "line 1: base_address malformed: expected integer"),
        (["base_address = 1 000000000"], None),  # trailing junk is ignored by Sscanf
        (["replace_float = 0, 1.5"], "line 1: replace_float malformed: unexpected EOF"),
        (["replace_float = 0, x, 1"], 'line 1: replace_float malformed: strconv.ParseFloat: parsing "": invalid syntax'),
        (["replace_int = 0, 1, 256"], "line 1: replace_int malformed: unsigned integer overflow on token 256"),
        (["find_base_address = x"], "line 1: find_base_address malformed: string not wrapped in backticks"),
        (["replace_string = 0,`a`,`b`"], "line 1: replace_string malformed"),
        (["replace_string = zz, `a`, `b`"], "line 1: replace_string offset malformed: expected integer"),
        (["replace_string = 0, a, `b`"], "line 1: replace_string find malformed: could not find valid string"),
        (["replace_string = 0, `a`"], "line 1: replace_string replace malformed: could not find valid string"),
        (["replace_string = 0, `a`, `b`, `c`"], "line 1: replace_string malformed: extraneous characters after last argument"),
        (["find_zlib = x"], "line 1: find_zlib malformed: string not wrapped in backticks"),
        (["find_zlib_hash = x"], "line 1: find_zlib_hash malformed: string not wrapped in backticks"),
        (["replace_zlib = 0`a`"], "line 1: replace_zlib malformed"),
        (["replace_zlib = g, `a`, `b`"], "line 1: replace_zlib offset malformed: expected integer"),
        (["replace_zlib = 0, `a`, b"], "line 1: replace_zlib replace malformed: could not find valid string"),
        (["replace_zlib = 0, `a` `b` c"], "line 1: replace_zlib malformed: extraneous characters after last argument"),
        (["replace_zlib = 0, `a` b"], "line 1: replace_zlib replace malformed: could not find valid string"),
        (["replace_zlib = 0, `a`, `b`x"], "line 1: replace_zlib malformed: extraneous characters after last argument"),
        (["find_replace_string = x, `b`"], "line 1: find_replace_string find malformed: could not find valid string"),
        (["find_replace_string = `a`"], "line 1: find_replace_string replace malformed: could not find valid string"),
        (["find_replace_string = `a`, `b`, "], "line 1: find_replace_string malformed: extraneous characters after last argument"),
        (["find_replace_string = `a`,`b`"], None),
        (["find_replace_string = `a`,,,  `b` x"], "line 1: find_replace_string malformed: extraneous characters after last argument"),
        (["Find_Bass_Address = `x`"], "line 1: unexpected instruction: Find_Bass_Address"),
    ],
)
def test_parse_errors(both_modes, lines, err):
    if err is None:
        P.parse(pfile(*lines))
        return
    with pytest.raises(P.Patch32lsbError) as e:
        P.parse(pfile(*lines))
    assert str(e.value) == err


def test_parse_line_numbers_count_crlf_lines(both_modes):
    with pytest.raises(P.Patch32lsbError) as e:
        P.parse(pfile("", "#", "bogus", nl="\r\n"))
    assert str(e.value) == "line 3: bad instruction: no equals sign"


# --- validation -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "lines,err",
    [
        (block("a") + block("b", "no"), None),
        (["<Patch>", "patch_name = `a`", "</Patch>"], "you must have exactly 1 patch_enable option in each patch (a)"),
        (block("a", "yes", "patch_enable = `no`"), "you must have exactly 1 patch_enable option in each patch (a)"),
        (block("a", "yes", "patch_group = `g`", "patch_group = `h`"), "you must have at most 1 patch_group option in each patch (a)"),
        (block("a", "yes", "patch_group = `g`") + block("b", "yes", "patch_group = `g`"), "more than one patch enabled in patch_group 'g'"),
        (block("a", "yes", "patch_group = `g`") + block("b", "no", "patch_group = `g`"), None),
        (block("a", "yes", "patch_group = ``") + block("b", "yes", "patch_group = ``"), None),  # empty group = no group
        (block("a", "no", "find_zlib_hash = `abc`"), "hash must be 40 chars in FindZlibHash in `a`"),
        (block("a", "no", "find_zlib_hash = `%s`" % ("é" * 20)), None),  # 40 bytes
    ],
)
def test_validate(both_modes, lines, err):
    ps = P.parse(pfile(*lines))
    if err is None:
        ps.validate()
    else:
        with pytest.raises(P.Patch32lsbError) as e:
            ps.validate()
        assert str(e.value) == err


def test_validate_file_order(both_modes):
    # kobopatch reported a random one of these; kobopatcher the first
    ps = P.parse(pfile(*(["<Patch>", "patch_name = `b`", "</Patch>"] + ["<Patch>", "patch_name = `a`", "</Patch>"])))
    with pytest.raises(P.Patch32lsbError, match=r"\(b\)$"):
        ps.validate()


# --- set_enabled ------------------------------------------------------------------------


def test_set_enabled(both_modes):
    ps = P.parse(pfile(*block("a", "no"), "<Patch>", "patch_name = `b`", "</Patch>", *block("c", "no", "patch_enable = `no`")))
    ps.set_enabled("a", True)
    assert insts(ps, "a")[0] == ("Enabled", True)
    ps.set_enabled("a", False)
    assert insts(ps, "a")[0] == ("Enabled", False)
    # only the first patch_enable changes
    ps.set_enabled("c", True)
    assert [v for k, v in insts(ps, "c") if k == "Enabled"] == [True, False]
    with pytest.raises(P.Patch32lsbError) as e:
        ps.set_enabled("b", True)
    assert str(e.value) == "could not set enabled state of 'b' to true: no Enabled instruction in patch"
    with pytest.raises(P.Patch32lsbError) as e:
        ps.set_enabled("b", False)
    assert str(e.value) == "could not set enabled state of 'b' to false: no Enabled instruction in patch"
    with pytest.raises(P.Patch32lsbError) as e:
        ps.set_enabled("missing", True)
    assert str(e.value) == "could not set enabled state of 'missing' to true: no such patch"
    ps.set_enabled("missing", False)  # disabling a missing patch is fine


# --- applying ---------------------------------------------------------------------------


@contextlib.contextmanager
def captured():
    logs = []
    old = patchfile.log
    patchfile.set_log(logs.append)
    so = io.StringIO()
    try:
        with contextlib.redirect_stdout(so):
            yield so, logs
    finally:
        patchfile.set_log(old)


def apply(lines, buf=SYNTH):
    ps = P.parse(pfile(*lines))
    pt = Patcher(bytearray(buf))
    with captured() as (so, logs):
        err = None
        try:
            ps.apply_to(pt)
        except KobopatcherError as e:
            err = e
    return bytes(pt.get_bytes()), err, so.getvalue(), "".join(logs)


def test_apply_replace_bytes(both_modes):
    out, err, so, log = apply(block("a", "yes", "replace_bytes = 5, 48 65, 4A 65"))
    assert err is None and out == SYNTH.replace(b"Hello", b"Jello")
    assert so == "  [1/1] Applying patch `a`\n"
    assert log == (
        "validating patch file\nlooping over patches\n  ResetBaseAddress()\n  Enabled: true\n  applying patch `a`\n"
        "looping over instructions\n  skipping non-instruction Enabled(), PatchGroup() or Comment()\n"
        "  ReplaceBytes(5, []byte{0x48, 0x65}, []byte{0x4a, 0x65})\n"
    )


def test_apply_base_address_and_find(both_modes):
    out, err, _, _ = apply(block("a", "yes", "base_address = 5", "replace_bytes = 6, 57, 77"))
    assert err is None and out == SYNTH.replace(b"World", b"world")
    out, err, _, log = apply(block("a", "yes", "find_base_address = `World`", "replace_bytes = 0, 57, 77"))
    assert err is None and out == SYNTH.replace(b"World", b"world")
    assert '  FindBaseAddressString("World") | hex:576f726c64\n' in log
    _, err, so, _ = apply(block("a", "yes", "base_address = 1000"))
    assert str(err) == "BaseAddress: offset greater than length of buf"
    assert so == "  [1/1] Applying patch `a`\n    Error: could not apply patch: BaseAddress: offset greater than length of buf\n"
    _, err, _, _ = apply(block("a", "yes", "find_base_address = `nope`"))
    assert str(err) == "FindBaseAddressString: FindBaseAddress: could not find bytes"


def test_apply_replace_string(both_modes):
    out, err, _, log = apply(block("a", "yes", "find_base_address = `Hello`", "replace_string = 0, `Hello`, `Hi`"))
    assert err is None and out == SYNTH.replace(b"Hello", b"Hi\x00lo")
    assert '  ReplaceString(0, "Hello", "Hi")\n' in log
    _, err, _, _ = apply(block("a", "yes", "replace_string = 0, `Hello`, `Hello!`"))
    assert str(err) == "ReplaceString: length mismatch in byte replacement"


def test_apply_find_replace_string(both_modes):
    out, err, _, log = apply(block("a", "yes", "find_replace_string = `World`, `Earth`"))
    assert err is None and out == SYNTH.replace(b"World", b"Earth")
    assert log.endswith('  FindReplaceString("World", "Earth")\n    FindBaseAddressString("World")\n    ReplaceString(0, "World", "Earth")\n')
    _, err, _, _ = apply(block("a", "yes", "find_replace_string = `Mars`, `Earth`"))
    assert str(err) == "FindReplaceString: FindBaseAddressString: FindBaseAddress: could not find bytes"
    _, err, _, _ = apply(block("a", "yes", "find_replace_string = `World`, `Earth!`"))
    assert str(err) == "FindReplaceString: ReplaceString: length mismatch in byte replacement"


def test_apply_replace_int_and_float(both_modes):
    i = SYNTH.index(b"\x2a")
    out, err, _, log = apply(block("a", "yes", "base_address = %X" % (i - 1), "replace_int = 1, 42, 7"))
    assert err is None and out[i] == 7
    assert "  ReplaceInt(1, 0x2a, 0x7)\n" in log
    j = SYNTH.index(struct.pack("<d", 1.5))
    out, err, _, log = apply(block("a", "yes", "replace_float = %X, 1.5, -2" % j))
    assert err is None and out[j : j + 8] == struct.pack("<d", -2.0)
    assert "  ReplaceFloat(%d, 1.5, -2)\n" % j in log
    _, err, _, _ = apply(block("a", "yes", "replace_float = 0, 1.5, -2"))
    assert str(err) == "ReplaceFloat: could not find specified bytes at offset"


CSS = "body {\n  margin: 0;\n  color: black;\n}\n"


def zbuf():
    # kobopatcher's compressor, i.e. classic zlib (Python's zlib may be zlib-ng)
    from kobopatcher.patchlib.zlibcss import compress

    return b"PAD" * 4 + compress(CSS.encode()) + b"\x00" * 8


def test_apply_zlib(both_modes):
    h = __import__("hashlib").sha1(CSS.encode()).hexdigest()
    out, err, _, log = apply(block("a", "yes", "find_zlib = `color: black;`", "replace_zlib = 0, `black`, `red`"), zbuf())
    assert err is None
    assert zlib.decompressobj().decompress(out[12:]) == CSS.replace("black", "red").encode()
    assert '  FindZlib("color: black;") | hex:' in log and '  ReplaceZlib(0, "black", "red")\n' in log
    out, err, _, _ = apply(block("a", "yes", "find_zlib_hash = `%s`" % h, "replace_zlib = 0, `margin: 0`, `margin: 1`"), zbuf())
    assert err is None and b"margin: 1" in zlib.decompressobj().decompress(out[12:])
    _, err, _, _ = apply(block("a", "yes", "find_zlib_hash = `%s`" % ("0" * 40)), zbuf())
    assert str(err) == "FindZlibHash: could not find hash"
    _, err, _, _ = apply(block("a", "yes", "replace_zlib = 0, `a`, `b`"), zbuf())
    assert str(err) == "ReplaceZlib: not a zlib stream"


def test_apply_skips_disabled_and_resets_base(both_modes):
    out, err, so, log = apply(
        block("a", "yes", "base_address = 5", "replace_bytes = 0, 48, 4A")
        + block("b", "no", "replace_bytes = 0, 00, 01")
        + block("c", "yes", "replace_bytes = 0, 57, 77")
    )
    assert str(err) == "ReplaceBytes: could not find specified bytes at offset"  # base reset to 0 for c
    assert so == (
        "  [1/3] Applying patch `a`\n  [2/3] Skipping disabled patch `b`\n  [3/3] Applying patch `c`\n"
        "    Error: could not apply patch: ReplaceBytes: could not find specified bytes at offset\n"
    )
    assert "  Enabled: false\n  skipping patch `b`\n" in log
    assert out == SYNTH.replace(b"Hello", b"Jello")


def test_apply_invalid(both_modes):
    _, err, so, _ = apply(block("a", "yes", "patch_group = `g`") + block("b", "yes", "patch_group = `g`"))
    assert str(err) == "invalid patch file: more than one patch enabled in patch_group 'g'"
    assert so == "  Error: invalid patch file: more than one patch enabled in patch_group 'g'\n"


def test_formats_registry(tmp_path):
    assert patchfile.get_format("patch32lsb") is P.parse
    f = tmp_path / "x.patch"
    f.write_bytes(pfile(*block("a")))
    assert patchfile.format_for_file(str(f)) == "patch32lsb"
    assert patchfile.read_from_file("patch32lsb", str(f)).names() == ["a"]
    f.write_bytes(pfile("<Patch>", "bogus"))
    with pytest.raises(patchfile.PatchFileError) as e:
        patchfile.read_from_file("patch32lsb", str(f))
    assert str(e.value) == "could not parse patch file: line 2: bad instruction: no equals sign"


# --- the fixes ----------------------------------------------------------------------------


def offset_of(line, both_modes):
    ps = P.parse(pfile(*block("a", "no", line)))
    return insts(ps, "a")[1][1]


@pytest.mark.parametrize(
    "off,go,fixed",
    [
        ("FFFFFFF0", -0xF0, -16),
        ("FFFFFFFF", -0xFF, -1),
        ("FFFFFF00", 0, -256),
        ("0FFFFFF0", 0, 0x0FFFFFF0),  # "0-0"
        ("7FFFFFFF", 7, 0x7FFFFFFF),  # "7-"
        ("-10", -16, -16),
        ("10", 16, 16),
    ],
)
def test_fix1_string_offsets(both_modes, off, go, fixed):
    for key in ("replace_string", "replace_zlib"):
        v = offset_of("%s = %s, `a`, `b`" % (key, off), both_modes)
        assert v[0] == (go if both_modes else fixed)


def test_fix1_string_offsets_lowercase(both_modes):
    # the hack only matched uppercase FFFFFF
    if both_modes:
        with pytest.raises(P.Patch32lsbError) as e:
            offset_of("replace_string = ffffff00, `a`, `b`", both_modes)
        assert str(e.value) == "line 4: replace_string offset malformed: integer overflow on token ffffff00"
    else:
        assert offset_of("replace_string = ffffff00, `a`, `b`", both_modes)[0] == -256


@pytest.mark.parametrize(
    "line,what",
    [
        ("replace_bytes = FFFFFFF0, 00, 01", "replace_bytes"),
        ("base_address = FFFFFFF0", "base_address"),
        ("replace_int = FFFFFFF0, 0, 1", "replace_int"),
        ("replace_float = FFFFFFF0, 0, 1", "replace_float"),
    ],
)
def test_fix1_other_offsets(both_modes, line, what):
    if both_modes:
        with pytest.raises(P.Patch32lsbError) as e:
            offset_of(line, both_modes)
        assert str(e.value) == "line 4: %s malformed: integer overflow on token FFFFFFF0" % what
    else:
        v = offset_of(line, both_modes)
        assert (v if isinstance(v, int) else v[0]) == -16


def test_fix1_negative_offset_applies(both_modes):
    lines = block("a", "yes", "find_base_address = `World`", "replace_string = FFFFFFFA, `Hello`, `Jello`")
    out, err, _, _ = apply(lines)
    if both_modes:
        # -0xFA: before the start of the buffer, which made Go panic
        assert isinstance(err, GoPanic)
    else:
        assert err is None and out == SYNTH.replace(b"Hello", b"Jello")


def test_fix2_patch_name_reset(both_modes):
    nameless = ["<Patch>", "patch_enable = `no`", "</Patch>"]
    if both_modes:
        with pytest.raises(P.Patch32lsbError) as e:
            P.parse(pfile(*block("a"), *nameless))
        assert str(e.value) == "line 7: duplicate patch with name 'a'"
        # a name between blocks is used by the next block
        ps = P.parse(pfile("patch_name = `b`", *nameless))
        assert ps.names() == ["b"]
        # patch_enable before patch_name passes with the previous name
        ps = P.parse(pfile(*block("a"), "<Patch>", "patch_enable = `no`", "patch_name = `b`", "</Patch>"))
        assert ps.names() == ["a", "b"]
    else:
        with pytest.raises(P.Patch32lsbError) as e:
            P.parse(pfile(*block("a"), *nameless))
        assert str(e.value) == "line 6: patch_enable set before patch_name"
        with pytest.raises(P.Patch32lsbError) as e:
            P.parse(pfile("patch_name = `b`", "<Patch>", "</Patch>"))
        assert str(e.value) == "line 3: no patch_name for patch"
        with pytest.raises(P.Patch32lsbError) as e:
            P.parse(pfile(*block("a"), "<Patch>", "patch_enable = `no`", "patch_name = `b`", "</Patch>"))
        assert str(e.value) == "line 6: patch_enable set before patch_name"


def test_fix3_file_order(both_modes):
    lines = block("z", "yes") + block("a", "no") + block("m", "yes")
    ps = P.parse(pfile(*lines))
    assert ps.names() == ["z", "a", "m"]
    _, err, so, _ = apply(lines)
    assert err is None
    assert so == "  [1/3] Applying patch `z`\n  [2/3] Skipping disabled patch `a`\n  [3/3] Applying patch `m`\n"


@pytest.mark.parametrize(
    "line,go_panic,err",
    [
        ("patch_name = `", "runtime error: index out of range [-1]", "line 1: error unescaping patch_name: string not wrapped in backticks"),
        ("patch_group = `\\\\\\`", "runtime error: index out of range [1] with length 1", "line 1: error unescaping patch_group: incomplete escape sequence at end of string"),
        ("find_base_address = `\\x`", "runtime error: slice bounds out of range [:4] with length 2", "line 1: find_base_address malformed: incomplete \\x escape sequence"),
        ("find_zlib = `ab\\x4`", "runtime error: slice bounds out of range [:4] with length 3", "line 1: find_zlib malformed: incomplete \\x escape sequence"),
        ("find_replace_string = `\\x`, `b`", "runtime error: slice bounds out of range [:4] with length 2", "line 1: find_replace_string find malformed: could not find valid string"),
        ("replace_string = 0, `a`, `\\x`", "runtime error: slice bounds out of range [:4] with length 2", "line 1: replace_string replace malformed: could not find valid string"),
    ],
)
def test_fix4_panics(both_modes, line, go_panic, err):
    if both_modes:
        with pytest.raises(GoPanic) as e:
            P.parse(pfile(line))
        assert str(e.value) == go_panic
    else:
        with pytest.raises(P.Patch32lsbError) as e:
            P.parse(pfile(line))
        assert str(e.value) == err


# --- differential fixture (tools/parity/p32_diff.py) -------------------------------------


def _load_p32_diff():
    spec = importlib.util.spec_from_file_location("p32_diff", REPO / "tools" / "parity" / "p32_diff.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fixture_cases():
    if not FIXTURE.exists():
        return []
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return doc["cases"]


def _field(c, key) -> bytes:
    if key in c:
        return c[key].encode("utf-8")
    return base64.b64decode(c[key + "_b64"])


@pytest.fixture(scope="module")
def p32_diff():
    return _load_p32_diff()


@pytest.fixture(scope="module")
def p32_bufs(p32_diff):
    bufs = p32_diff.synth_buffers()
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for k, h in doc["buffers"].items():
        assert __import__("hashlib").sha1(bufs[k]).hexdigest() == h, "synthetic buffer %s changed; regenerate the fixture" % k
    lib = REPO / "tests" / "testdata" / "libnickel.so.1.0.0"
    if lib.exists():
        bufs["libnickel"] = lib.read_bytes()
    return bufs


_CASES = _fixture_cases()


def test_go_fixture_present():
    assert len(_CASES) >= 400


@pytest.mark.parametrize("n", range(len(_CASES)))
def test_go_fixture(p32_diff, p32_bufs, n):
    case = _CASES[n]
    if "libnickel" in case["bufs"] and "libnickel" not in p32_bufs:
        pytest.skip("libnickel.so.1.0.0 not in tests/testdata")
    with go_compat_mode(True):
        got = p32_diff.describe_bytes(_field(case, "patch"), case["bufs"], p32_bufs)
    assert got.decode("utf-8", "replace") == _field(case, "out").decode("utf-8", "replace")
    assert got == _field(case, "out")
