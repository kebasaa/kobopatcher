"""Ported from kobopatch patchlib/patcher_test.go, plus tests for the fixes."""

import hashlib

import pytest

from kobopatcher.compat import go_compat_mode
from kobopatcher.errors import GoPanic
from kobopatcher.patchlib.patcher import Patcher, PatchError
from kobopatcher.patchlib.zlibcss import inflate_lenient, zlib_selftest


def err(fn, *a):
    with pytest.raises(PatchError):
        fn(*a)


def test_get_bytes():
    assert Patcher(b"this is a test").get_bytes() == b"this is a test"


def test_reset_base_address():
    p = Patcher(b"this is a test")
    p.cur = 5
    p.reset_base_address()
    assert p.cur == 0


def test_base_address():
    p = Patcher(b"this is a test")
    err(p.base_address, 14)
    err(p.base_address, -1)
    p.base_address(4)
    assert p.cur == 4


def test_find_base_address():
    p = Patcher(b"this is a test")
    err(p.find_base_address, b"thiss")
    err(p.find_base_address, b"this is a test sdfsdf")
    p.find_base_address(b"a test")
    assert p.cur == 8


def test_find_base_address_string():
    p = Patcher(b"this is a test")
    err(p.find_base_address_string, "thiss")
    err(p.find_base_address_string, "this is a test sdfsdf")
    p.find_base_address_string("a test")
    assert p.cur == 8


def test_replace_string():
    p = Patcher(b"this is a test")
    p.replace_string(10, "test", "n")
    p.replace_string(0, "this ", "that ")
    err(p.replace_string, 0, "this ", "that ")
    p.replace_string(0, "s", "5")
    assert p.get_bytes() == b"that i5 a n\x00st"


def test_replace_bytes():
    p = Patcher(bytes([0, 1, 2, 3, 4, 5]))
    err(p.replace_bytes, 0, b"\x00", b"\x00\x01")
    err(p.replace_bytes, 3, b"\x02\x03", b"\x03\x02")
    p.replace_bytes(2, b"\x02\x03", b"\x03\x02")
    err(p.replace_bytes, 0, b"\x02\x03", b"\x03\x02")
    assert p.get_bytes() == bytes([0, 1, 3, 2, 4, 5])


def test_replace_int():
    p = Patcher(bytes([0, 1, 2, 0xFF, 4, 5]))
    err(p.replace_int, 4, 255, 195)
    p.replace_int(3, 255, 195)
    err(p.replace_int, 2, 255, 195)
    assert p.get_bytes() == bytes([0, 1, 2, 0xC3, 4, 5])


def test_replace_float():
    p = Patcher(bytes([0x00, 0xCD, 0xCC, 0xCC, 0xCC, 0xCC, 0xCC, 0xF0, 0x3F, 0x05]))
    p.replace_float(1, 1.05, 3.25)
    err(p.replace_float, 0, 1.05, 3.25)
    assert p.get_bytes() == bytes([0x00, 0, 0, 0, 0, 0, 0, 0xA, 0x40, 0x05])


def test_all():
    inp = bytes([0x00, 0x01, 0xFF, 0xCD, 0xCC, 0xCC, 0xCC, 0xCC, 0xCC, 0xF0, 0x3F, 0x01, 0x02, 0x03, 0x04]) + b"this is a test"
    eout = bytes([0x33, 0x33, 0x00, 0x33, 0x33, 0x33, 0x33, 0x33, 0x33, 0x10, 0x40, 0x01, 0x02, 0x03, 0x04]) + b"taht\x00is z test"
    p = Patcher(inp)
    assert p.cur == 0
    err(p.replace_int, 0, 255, 0)
    p.replace_int(2, 255, 0)
    p.replace_string(0, "this ", "that")
    err(p.replace_string, 0, "this ", "that")
    p.replace_float(3, 1.05, 4.05)
    err(p.find_base_address, b"a testdfgdfg")
    assert p.cur == 0
    p.find_base_address(b"a test")
    assert p.cur == 23
    err(p.replace_string, 0, "is", "si")
    p.replace_string(0, "a", "t")
    p.reset_base_address()
    assert p.cur == 0
    p.replace_string(0, "that", "taht")
    p.find_base_address(b"taht")
    assert p.cur == 15
    p.replace_string(8, "t", "z")
    err(p.replace_bytes, 0, b"\x00\x01", b"\x33\x33")
    p.reset_base_address()
    err(p.replace_bytes, 3, b"\x00\x01", b"\x33\x33")
    p.replace_bytes(0, b"\x00\x01", b"\x33\x33")
    assert p.get_bytes() == eout


def test_zlib(testdata):
    assert zlib_selftest() is None
    p = Patcher(testdata("nickel"))
    err(p.find_zlib, "height")
    z = p.extract_zlib()
    for zi in z:
        if "#boggleContainer[qApp_deviceIsPika=true]" in zi.css:
            assert hashlib.sha1(zi.css.encode()).hexdigest() == "1b80e45ffa47d77642b053205452a528d7b37c76"
    p.find_zlib("#boggleContainer[qApp_deviceIsPika=true]")
    assert p.cur == 4563746
    p.replace_zlib(0, "qproperty-visible: false;", "qproperty-visible:true;")
    err(p.replace_zlib, 0, "dfgdfgdfg", "dfgdfgdfg")
    err(
        p.replace_zlib,
        0,
        "false",
        "a really long string which is way too long to possibly fit asndkjas aksjndkajsnd kajsnjkwjnw akjnwr kejnskr fjsdndf ksjdndkf jsd",
    )
    z = p.extract_zlib()
    for zi in z:
        if "#boggleContainer[qApp_deviceIsPika=true]" in zi.css:
            assert hashlib.sha1(zi.css.encode()).hexdigest() == "cc9d7ce57f8746517ea692b7c65e9ed74c1d765b"
    p.reset_base_address()
    p.find_zlib_hash("cc9d7ce57f8746517ea692b7c65e9ed74c1d765b")
    assert p.cur == 4563746
    p.replace_zlib(0, "qproperty-visible: true;", "qproperty-visible: ALongString;")
    p.replace_zlib(0, "qproperty-visible: ALongString;", "qproperty-visible: true;")
    long = (
        "qproperty-visible:true; a really long string which is way too long to possibly fit asndkjas "
        "aksjndkajsnd kajsnjkwjnw akjnwr kejnskr fjsdndf ksjdndkf jsd"
    )
    err(p.replace_zlib_group, 0, [("qproperty-visible: true;", long)])
    p.replace_zlib_group(0, [("qproperty-visible: true;", long), (long, "qproperty-visible: true;")])
    p.replace_zlib_group(0, [("qproperty-visible: true;", long), (long, "qproperty-visible: true;")])
    p.reset_base_address()
    err(p.replace_zlib, 0, " ", " ")


def test_real(testdata):
    p = Patcher(testdata("libnickel.so.1.0.0"))

    def cs(exp):
        assert hashlib.sha256(p.get_bytes()).hexdigest() == exp

    def ign(fn, *a):
        try:
            fn(*a)
        except PatchError:
            pass

    cs("6603e718eb01947c7face497dd766e3447dce95dbcbabb7d31f46e9d09fbb1e5")
    p.reset_base_address()
    ign(p.replace_int, 0x95DD02, 1, 2)
    cs("7a5c27729c5a2ac20246ad1c44c789410a4ac344a344dab2432b04d0093f186e")
    p.reset_base_address()
    ign(p.find_base_address_string, "*.otf")
    ign(p.replace_string, 0, "*", "_")
    cs("ed3de9e305b883642d4ecc1e5ace77c15e2d4f6ceacafa1816727724429e34cb")
    p.reset_base_address()
    ign(p.replace_bytes, 0x9F6252, bytes([0x40, 0xF6, 0xB8, 0x31]), bytes([0x03, 0x21, 0x89, 0x02]))
    ign(p.replace_int, 0x9F6252, 3, 5)
    cs("61c9d966ef53018c9de9c674dc2946cbb42701fa5a458704bc115023e193c55f")
    p.reset_base_address()
    ign(p.find_base_address_string, "\x00Extra:\x20")
    ign(p.replace_string, 0o007, "\x20", "_")
    cs("8f92a0c8b7041b89d331d5dd0e4579e30caa757272bbc0f47578765f49fe4076")
    p.reset_base_address()
    ign(p.replace_string, 0xC9717C, "%1/dicthtml%2", "%1/micthtml%2")
    ign(p.replace_string, 0xC9718C, "dicthtml", "micthtml")
    ign(p.replace_string, 0xC971D4, "/mnt/onboard/.kobo/dict/dicthtml%1", "/mnt/onboard/.kobo/dict/micthtml%1")
    cs("8a36efb87ccfd3fe0bad3a9913be12273fec089cbb7537e6a4cff69edcae1520")
    p.reset_base_address()
    ign(p.find_base_address_string, "a:link, a:visited, a:hover, a:active {")
    ign(p.replace_string, 0x0027, "b", "_")
    ign(p.replace_string, 0x0053, "c", "_")
    cs("a64e265813aaf58d5fb227d681a9049ebd2daf1270a1cb3f9d21564d1f260842")
    p.reset_base_address()
    ign(p.find_base_address_string, ".KBHighlighting, .KBSearchResult {")
    ign(
        p.replace_string,
        0x0000,
        ".KBHighlighting, .KBSearchResult { background-color: #C6C6C6 !important; } \t",
        ".KBHighlighting,.KBSearchResult{background-color:#C6C6C6!important}.KBSearch",
    )
    ign(
        p.replace_string,
        0x004C,
        ".KBSearchResult, .KBAnnotation, .KBHighlighting { font-size: 100% !important; -webkit-text-combine: inherit !important; } \t",
        "Result,.KBAnnotation,.KBHighlighting{font-size:100%!important;-webkit-text-combine:inherit!important}.KBAnnotation[writingM",
    )
    ign(
        p.replace_string,
        0x00C7,
        '.KBAnnotation[writingMode="horizontal-tb"], .KBAnnotationContinued[writingMode="horizontal-tb"] { border-bottom: 2px solid black !important; } \t',
        'ode="horizontal-tb"],.KBAnnotationContinued[writingMode="horizontal-tb"]{border-bottom:2px solid black!important}.KBAnnotation[writingMode="vert',
    )
    ign(
        p.replace_string,
        0x0157,
        '.KBAnnotation[writingMode="vertical-rl"], .KBAnnotationContinued[writingMode="vertical-rl"] { border-right: 2px solid black !important; } \t',
        'ical-rl"],.KBAnnotationContinued[writingMode="vertical-rl"]{border-right:2px solid black!important}.KBAnnotation[writingMode="vertical-lr"]',
    )
    ign(
        p.replace_string,
        0x01E2,
        '.KBAnnotation[writingMode="vertical-lr"], .KBAnnotationContinued[writingMode="vertical-lr"] { border-left: 2px solid black !important; }',
        ',.KBAnnotationContinued[writingMode="vertical-lr"]{border-left:2px solid black!important}/*********************************************/',
    )
    cs("07509cc3ed09e60558b37f4f71245af8893b52f221b84810260be53c0f163f6f")
    p.reset_base_address()
    for off, f in [
        (0x659DA4, [0xBE, 0xF5, 0xAE, 0xE9]),
        (0x659DFA, [0xBE, 0xF5, 0x84, 0xE9]),
        (0x659E24, [0xBE, 0xF5, 0x6E, 0xE9]),
        (0x659E60, [0xBE, 0xF5, 0x50, 0xE9]),
        (0x659EC6, [0xBE, 0xF5, 0x1E, 0xE9]),
    ]:
        ign(p.replace_bytes, off, bytes(f), bytes([0x00, 0x46, 0x00, 0x46]))
    ign(p.base_address, 0x659F60)
    for i, (f, r) in enumerate(
        [(1.05, 0.8), (1.07, 0.85), (1.1, 0.875), (1.35, 0.9), (1.7, 0.925), (1.8, 0.95), (2.2, 0.975), (2.4, 1.0), (2.6, 1.05), (2.8, 1.1)]
    ):
        ign(p.replace_float, i * 8, f, r)
    cs("d07f0d59517bee75043505da790adfe4875b18eea2f7d65cfd6bb7e61068ecc9")


# --- behaviour that kobopatcher changes (see README "Differences from kobopatch")


def test_negative_offset_is_an_error(both_modes):
    p = Patcher(b"0123456789")
    p.cur = 2
    exc = GoPanic if both_modes else PatchError
    with pytest.raises(exc):
        p.replace_bytes(-5, b"0", b"1")


def test_replace_bytes_nop_relative_to_base(both_modes):
    p = Patcher(b"\x11\x22\x33\x44\x11\x22")
    p.base_address(4)
    if both_modes:
        # kobopatch ignored the base address
        p.replace_bytes_nop(0, b"\x11\x22")
        assert p.get_bytes() == b"\x00\x46\x33\x44\x11\x22"
    else:
        p.replace_bytes_nop(0, b"\x11\x22")
        assert p.get_bytes() == b"\x11\x22\x33\x44\x00\x46"


def test_inflate_lenient_trailing_garbage_and_truncation():
    import zlib

    data = b"QWidget { color: black; }\n" * 20
    c = zlib.compress(data)
    r = inflate_lenient(c + b"garbage\x00\xff", 0)
    assert r.err is None and r.data == data
    bad = bytearray(c)
    bad[-1] ^= 0xFF
    r = inflate_lenient(bytes(bad), 0)
    assert r.err == "invalid checksum" and r.data == data
    r = inflate_lenient(c[: len(c) // 2], 0)
    assert r.err == "unexpected EOF"
    r = inflate_lenient(c[:-2], 0)
    assert r.err == "unexpected EOF" and r.data == data
