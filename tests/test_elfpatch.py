"""Tests for kobopatcher.patchlib.elfpatch (port of kobopatch's patchlib/elf_test.go).

They run against the real binaries from Kobo firmware 4.38.23429 (not
redistributable, so not in testdata); see the rootfs fixture in conftest.py.
The expected offsets and bytes come from kobo_aura_h2o's
docs/kobopatch-extensions.md section 4. (TestDecodeThumbBL is covered by
test_asm.py.)
"""

import os
import struct

import pytest

from kobopatcher.patchlib.elffile import (
    DF_1_NOW,
    DT_BIND_NOW,
    DT_FLAGS,
    DT_FLAGS_1,
    DT_NEEDED,
    PT_GNU_RELRO,
    PT_GNU_STACK,
    ElfFile,
)
from kobopatcher.patchlib.elfpatch import parse_elf
from kobopatcher.patchlib.patcher import Patcher, PatchError

EM_386 = 3
LIBNICKEL = "usr/local/Kobo/libnickel.so.1.0.0"

# section 4.1 table: file, DT_FLAGS_1 value offset, PT_GNU_RELRO phdr offset.
LAZY_LIBS = [
    ("usr/local/Kobo/libiwnn.so.1.0.0", 0x5CB20, 0xF4),
    ("usr/local/Kobo/libnickel.so.1.0.0", 0x1684AB4, 0xF4),
    ("usr/local/Kobo/libQtSolutions_IOCompressor-2.3.so.1.0.0", 0x3EBC, 0xF4),
    ("usr/local/Kobo/librmsdk.so.1.0.0", 0x82C554, 0xF4),
    ("usr/local/Kobo/libsunpinyin.so.3.0.0", 0x3E8A4, 0xF4),
    ("usr/local/Trolltech/QtEmbedded-4.6.2-arm/lib/libQtConcurrent.so.4.6.2", 0x3EEC, 0xF4),
    ("usr/local/Trolltech/QtEmbedded-4.6.2-arm/lib/libQtCore.so.4.6.2", 0x3B063C, 0x114),
    ("usr/local/Trolltech/QtEmbedded-4.6.2-arm/lib/libQtDBus.so.4.6.2", 0x5FA24, 0xF4),
    ("usr/local/Trolltech/QtEmbedded-4.6.2-arm/lib/libQtGui.so.4.6.2", 0x317C4C, 0xF4),
    ("usr/local/Trolltech/QtEmbedded-4.6.2-arm/lib/libQtMultimedia.so.4.6.2", 0xA52E4, 0xF4),
    ("usr/local/Trolltech/QtEmbedded-4.6.2-arm/lib/libQtMultimediaWidgets.so.4.6.2", 0xFCF4, 0xF4),
    ("usr/local/Trolltech/QtEmbedded-4.6.2-arm/lib/libQtNetwork.so.4.6.2", 0x1033E4, 0xF4),
    ("usr/local/Trolltech/QtEmbedded-4.6.2-arm/lib/libQtPrintSupport.so.4.6.2", 0x3D784, 0xF4),
    ("usr/local/Trolltech/QtEmbedded-4.6.2-arm/lib/libQtScript.so.4.6.2", 0x150734, 0xF4),
    ("usr/local/Trolltech/QtEmbedded-4.6.2-arm/lib/libQtSensors.so.4.6.2", 0x28C3C, 0xF4),
    ("usr/local/Trolltech/QtEmbedded-4.6.2-arm/lib/libQtSql.so.4.6.2", 0x2ECC4, 0xF4),
    ("usr/local/Trolltech/QtEmbedded-4.6.2-arm/lib/libQtSvg.so.4.6.2", 0x358C4, 0xF4),
    ("usr/local/Trolltech/QtEmbedded-4.6.2-arm/lib/libQtWebKit.so.4.6.2", 0x12F3EDC, 0xF4),
    ("usr/local/Trolltech/QtEmbedded-4.6.2-arm/lib/libQtWebKitWidgets.so.4.6.2", 0x30608, 0xF4),
    ("usr/local/Trolltech/QtEmbedded-4.6.2-arm/lib/libQtWidgets.so.4.6.2", 0x44537C, 0xF4),
    ("usr/local/Trolltech/QtEmbedded-4.6.2-arm/lib/libQtXmlPatterns.so.4.6.2", 0x2A13C4, 0xF4),
    ("usr/local/Trolltech/QtEmbedded-4.6.2-arm/lib/libQtXml.so.4.6.2", 0x27DB4, 0xF4),
]

B01000000 = b"\x01\x00\x00\x00"
B00000000 = b"\x00\x00\x00\x00"
BRELRO = b"\x52\xe5\x74\x64"
BFLAGS1 = b"\xfb\xff\xff\x6f"


def must_err(fn, contains):
    with pytest.raises(PatchError) as ei:
        fn()
    assert contains in str(ei.value), "expected an error containing %r, got %s" % (contains, ei.value)


def bytes_at(buf, off, exp, msg):
    got = bytes(buf[off : off + len(exp)])
    assert got == exp, "%s: at 0x%X: got %s, expected %s" % (msg, off, got.hex(" "), exp.hex(" "))


@pytest.mark.parametrize("path,flags1,relro_hdr", LAZY_LIBS, ids=[os.path.basename(x[0]) for x in LAZY_LIBS])
def test_elf_lazy_binding_oracle(fwfile, path, flags1, relro_hdr):
    orig = fwfile(path)
    buf = bytearray(orig)

    # the doc's oracle for the unpatched file
    bytes_at(buf, flags1 - 4, BFLAGS1, "DT_FLAGS_1 tag")
    bytes_at(buf, flags1, B01000000, "DT_FLAGS_1 value")
    bytes_at(buf, relro_hdr, BRELRO, "PT_GNU_RELRO p_type")

    hooked = []
    p = Patcher(buf)
    p.set_hook(lambda off, find, replace: hooked.append(off))
    changes = p.elf_lazy_binding()
    print("changes:", changes)
    assert len(hooked) == 3, "expected 3 hooked writes (FLAGS_1, BIND_NOW, RELRO), got %d" % len(hooked)

    # PT_GNU_RELRO -> PT_NULL in place
    bytes_at(buf, relro_hdr, B00000000, "PT_GNU_RELRO p_type after")

    # DT_BIND_NOW (which precedes DT_FLAGS_1 in all of these) was removed, so
    # DT_FLAGS_1 moved up by one entry
    ei = parse_elf(buf)
    f1 = ei.dynamic(DT_FLAGS_1)
    assert len(f1) == 1
    assert f1[0].off + 4 == flags1 - 8
    bytes_at(buf, flags1 - 8, B00000000, "DT_FLAGS_1 value after")
    assert not ei.dynamic(DT_BIND_NOW), "DT_BIND_NOW still present"
    assert not ei.eager()
    assert ei.prog(PT_GNU_RELRO)[1] is None, "PT_GNU_RELRO still present"

    # only the dynamic section and the phdr changed
    ei0 = parse_elf(orig)
    dstart, dend = ei0.dyn.off, ei0.dyn.off + ei0.dyn.filesz
    for i, (a, b) in enumerate(zip(buf, orig)):
        if a != b and not dstart <= i < dend and not relro_hdr <= i < relro_hdr + 4:
            pytest.fail("unexpected change at 0x%X" % i)

    p.check_elf(orig)

    # not again
    must_err(lambda: Patcher(buf).elf_lazy_binding(), "not eagerly bound")


def test_elf_check_raw_lazy_patch(fwfile):
    """The raw 4.1 ReplaceBytes patch (clear DF_1_NOW, drop PT_GNU_RELRO) leaves
    DT_BIND_NOW behind, which glibc also honours, so check_elf must reject it."""
    orig = fwfile(LIBNICKEL)
    p = Patcher(bytearray(orig))
    p.replace_bytes(0x1684AB4, B01000000, B00000000)
    p.replace_bytes(0xF4, BRELRO, B00000000)
    must_err(lambda: p.check_elf(orig), "eager binding only partly removed")


def test_elf_check_relro_only(fwfile):
    orig = fwfile(LIBNICKEL)
    p = Patcher(bytearray(orig))
    assert p.elf_disable_program_header(PT_GNU_RELRO), "expected PT_GNU_RELRO to be found"
    must_err(lambda: p.check_elf(orig), "PT_GNU_RELRO removed, but still eagerly bound")


def test_elf_check_lazy_with_relro(fwfile):
    orig = fwfile(LIBNICKEL)
    p = Patcher(bytearray(orig))
    p.elf_clear_dyn_flag(DT_FLAGS_1, DF_1_NOW)
    p.elf_remove_dyn_entry(DT_BIND_NOW)
    must_err(lambda: p.check_elf(orig), "PT_GNU_RELRO covers the PLT GOT")


def test_elf_clear_dyn_flag(fwfile):
    buf = bytearray(fwfile(LIBNICKEL))
    p = Patcher(buf)

    old, found = p.elf_clear_dyn_flag(DT_FLAGS_1, 0x1)
    assert found and old == 1, "expected found DT_FLAGS_1=1, got found=%s old=0x%X" % (found, old)
    bytes_at(buf, 0x1684AB4, B00000000, "DT_FLAGS_1 value")

    must_err(lambda: p.elf_clear_dyn_flag(DT_FLAGS_1, 0x1), "already patched")

    _, found = p.elf_clear_dyn_flag(DT_FLAGS, 0x8)
    assert not found, "DT_FLAGS should not be present"

    must_err(lambda: p.elf_clear_dyn_flag(DT_BIND_NOW, 0x1), "unsupported tag")
    must_err(lambda: p.elf_clear_dyn_flag(DT_FLAGS_1, 0), "mask is 0")


def test_elf_disable_program_header(fwfile):
    buf = bytearray(fwfile(LIBNICKEL))
    p = Patcher(buf)

    assert p.elf_disable_program_header(PT_GNU_RELRO), "expected PT_GNU_RELRO"
    bytes_at(buf, 0xF4, B00000000, "phdr 6 p_type")

    assert not p.elf_disable_program_header(PT_GNU_RELRO), "PT_GNU_RELRO should be gone"

    must_err(lambda: p.elf_disable_program_header(PT_GNU_STACK), "unsupported type")


def test_elf_remove_dyn_entry(fwfile):
    orig = fwfile(LIBNICKEL)
    buf = bytearray(orig)
    p = Patcher(buf)

    oi = parse_elf(orig)
    assert p.elf_remove_dyn_entry(DT_BIND_NOW), "expected DT_BIND_NOW"
    ni = parse_elf(buf)
    assert len(ni.dents) == len(oi.dents) - 1
    # the slot after the new DT_NULL is also DT_NULL
    last = ni.dents[-1].off
    bytes_at(buf, last + 8, bytes(8), "padding DT_NULL")

    assert not p.elf_remove_dyn_entry(DT_BIND_NOW), "DT_BIND_NOW should be gone"

    must_err(lambda: p.elf_remove_dyn_entry(DT_NEEDED), "unsupported tag")


def test_elf_stub_function(fwfile):
    orig = fwfile(LIBNICKEL)
    buf = bytearray(orig)
    p = Patcher(buf)

    # 4.3: Kobo Plus promo check, void
    off, ob = p.stub_function("_ZN18HomePageController22maybeShowKoboPlusPromoEv", None)
    assert off == 0xD5F2C0, "expected offset 0xd5f2c0, got 0x%X" % off
    assert ob == b"\x2d\xe9\xf0\x4f", "unexpected original bytes %s" % ob.hex(" ")
    bytes_at(buf, 0xD5F2C0, b"\x70\x47\x00\xbf", "void stub")

    must_err(lambda: p.stub_function("_ZN18HomePageController22maybeShowKoboPlusPromoEv", None), "already stubbed")

    # int return (demangled name lookup)
    off, _ = p.stub_function("ReadingView::nextPage()", 0)
    bytes_at(buf, off, b"\x00\x20\x70\x47", "movs r0, #0; bx lr")

    must_err(lambda: p.stub_function("_ZN11ReadingView8prevPageEv", 256), "does not fit")
    must_err(lambda: p.stub_function("_ZN11ReadingView8doesNotExistEv", None), "no such dynamic symbol")
    must_err(lambda: p.stub_function("_ZTV11ReadingView", None), "not STT_FUNC")
    must_err(lambda: p.stub_function("_ZN7QWidget12setAttributeEN2Qt15WidgetAttributeEb", None), "not defined in this file")

    p.check_elf(orig)
    buf[0xD5F2C0] = 0x2D  # overwrite the stub
    must_err(lambda: p.check_elf(orig), "was overwritten")


def test_elf_stub_function_arm(fwfile):
    # all exported functions in these libraries are Thumb, so test the ARM
    # encoding by clearing the Thumb bit of a (4-byte aligned) one in dynsym
    buf = bytearray(fwfile(LIBNICKEL))
    f = ElfFile(buf)
    ds = f.section(".dynsym")
    addr = None
    for i, s in enumerate(f.dynamic_symbols()):
        if s.name == "_ZN11ReadingView8nextPageEv":
            addr = s.value & ~1
            assert addr % 4 == 0, "expected a 4-byte aligned function"
            struct.pack_into("<I", buf, ds.offset + (i + 1) * 16 + 4, addr)  # st_value (+1 for the null symbol)
    p = Patcher(buf)
    off, _ = p.stub_function("_ZN11ReadingView8nextPageEv", 1)
    assert off == addr, "expected 0x%X, got 0x%X" % (addr, off)
    bytes_at(buf, addr, b"\x01\x00\xa0\xe3\x1e\xff\x2f\xe1", "mov r0, #1; bx lr")


def test_elf_nop_call(fwfile):
    orig = fwfile(LIBNICKEL)
    buf = bytearray(orig)
    p = Patcher(buf)

    nop2 = b"\x00\xbf\x00\xbf"
    for sym, rel, target, exp_off, find in [
        # 4.3: sketch overlay timer per page turn
        ("_ZN11ReadingView8nextPageEv", 0xC, "_ZN10SketchNote16disableWithTimerEi", 0xF5BB28, b"\x33\xf7\x00\xc8"),
        ("_ZN11ReadingView8prevPageEv", 0xC, "SketchNote::disableWithTimer(int)", 0xF5BAE4, b"\x33\xf7\x22\xc8"),
        # 4.3: forced full refresh per PDF page turn
        ("_ZN11ReadingView8nextPageEv", 0x1E, "_ZN7QWidget12setAttributeEN2Qt15WidgetAttributeEb", 0xF5BB3A, b"\x4c\xf7\x7e\xcd"),
        ("_ZN11ReadingView8prevPageEv", 0x1E, "_ZN7QWidget12setAttributeEN2Qt15WidgetAttributeEb", 0xF5BAF6, b"\x4c\xf7\xa0\xcd"),
    ]:
        bytes_at(buf, exp_off, find, sym + " original")
        off = p.nop_call(sym, rel, target)
        assert off == exp_off, "%s+0x%X: expected 0x%X, got 0x%X" % (sym, rel, exp_off, off)
        bytes_at(buf, exp_off, nop2, sym + " patched")
    p.check_elf(orig)

    q = Patcher(bytearray(orig))
    must_err(
        lambda: q.nop_call("_ZN11ReadingView8nextPageEv", 0xC, "_ZN7QWidget12setAttributeEN2Qt15WidgetAttributeEb"),
        "calls 0x68EB2C, not",
    )
    must_err(lambda: q.nop_call("_ZN11ReadingView8nextPageEv", 0x0, "_ZN10SketchNote16disableWithTimerEi"), "is not a BL/BLX")
    must_err(
        lambda: q.nop_call("_ZN11ReadingView8nextPageEv", 0x1000, "_ZN10SketchNote16disableWithTimerEi"),
        "not a halfword inside",
    )
    must_err(lambda: q.nop_call("_ZN11ReadingView8nextPageEv", 0xC, "_ZN3Foo3barEv"), "no definition or PLT entry")


def test_tombstone(fwfile):
    buf = bytearray(fwfile("usr/local/Kobo/libboggle.so"))
    orig = bytes(buf)

    p = Patcher(buf)
    must_err(lambda: p.tombstone("usr/local/Kobo/libboggle.so"), "path of the file being patched is unknown")

    p.set_path("./usr/local/Kobo/libboggle.so")
    must_err(lambda: p.tombstone("usr/local/Kobo/libsudoku.so"), "does not match")
    must_err(lambda: p.tombstone("libboggle.so"), "does not match")
    p.tombstone("usr/local/Kobo/libboggle.so")
    assert len(p.get_bytes()) == 0
    must_err(lambda: p.tombstone("usr/local/Kobo/libboggle.so"), "already empty")
    p.check_elf(orig)  # tombstones are not checked

    # kobopatch-apply style absolute path
    q = Patcher(b"x")
    q.set_path("/tmp/rootfs/usr/local/Kobo/libboggle.so")
    q.tombstone("usr/local/Kobo/libboggle.so")


def test_elf_rejects_non_arm(fwfile):
    must_err(lambda: parse_elf(b"not an elf"), "parse elf")

    buf = bytearray(fwfile(LIBNICKEL))
    struct.pack_into("<H", buf, 18, EM_386)
    must_err(lambda: Patcher(buf).elf_lazy_binding(), "not a 32-bit little-endian ARM ELF")
