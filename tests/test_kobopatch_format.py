"""Ported from kobopatch patchfile/kobopatch/patch_test.go and elf_test.go,
plus tests for the YAML dialect and the fixes."""

import pytest

from kobopatcher import yamlnodes as Y
from kobopatcher.errors import GoPanic
from kobopatcher.patchfile import kobopatch as K
from kobopatcher.patchlib.patcher import Patcher


def to_instruction(y):
    node = Y.parse(y)
    instnode = None if node is None else Y.decode_strict(node, K._INSTNODE)
    return K._to_instruction(instnode)


@pytest.mark.parametrize(
    "msg,y,err",
    [
        ("None", "", "expected instruction, got nothing"),
        ("Unknown", "Unknown: true", 'line 1: unknown instruction type "Unknown"'),
        ("TooMany", '{BaseAddress: 1, FindBaseAddressString: "test"}', "line 1: multiple types found in instruction, maybe you forgot a '-'"),
        (
            "StructExtra",
            'FindReplaceString: {Find: "test", Replace: "test", Extra: "asd"}',
            "line 1: error decoding instruction: yaml: unmarshal errors:\n  line 1: field Extra not found in type kobopatch.FindReplaceString",
        ),
    ],
)
def test_instruction_node_errors(msg, y, err):
    with pytest.raises(K.KobopatchError) as e:
        to_instruction(y)
    assert str(e.value) == err


def test_instruction_node_values():
    name, v = to_instruction("BaseAddress: 1")
    assert name == "BaseAddress" and v == K.FlexAbsOffset(offset=1, inline=True)
    name, v = to_instruction("BaseAddress: 0")
    assert v != K.FlexAbsOffset(offset=1, inline=True)
    name, v = to_instruction('FindReplaceString: {Find: "test", Replace: "test"}')
    assert (v.find, v.replace, v.must_match_length) == ("test", "test", False)


@pytest.mark.parametrize(
    "y,exp",
    [
        ("BaseAddress: 1", K.FlexAbsOffset(offset=1, inline=True)),
        ("BaseAddress: {Offset: 1}", K.FlexAbsOffset(offset=1)),
        ("BaseAddress: {Sym: Test}", K.FlexAbsOffset(sym="Test")),
        ("BaseAddress: {SymPLT: Test}", K.FlexAbsOffset(sym_plt="Test")),
        ("BaseAddress: {SymPLTTail: Test}", K.FlexAbsOffset(sym_plt_tail="Test")),
    ],
)
def test_flexabsoffset_baseaddress(y, exp):
    _, v = to_instruction(y)
    assert v == exp
    # re-marshal
    assert "BaseAddress: " + v.to_yaml() == y


@pytest.mark.parametrize(
    "y,exp",
    [
        ("ReplaceBytes: {Base: 1}", K.FlexAbsOffset(offset=1, inline=True)),
        ("ReplaceBytes: {Base: {Offset: 1}}", K.FlexAbsOffset(offset=1)),
        ("ReplaceBytes: {Base: {Sym: Test}}", K.FlexAbsOffset(sym="Test")),
        ("ReplaceBytes: {Base: {SymPLT: Test}}", K.FlexAbsOffset(sym_plt="Test")),
        ("ReplaceBytes: {Base: {SymPLTTail: Test}}", K.FlexAbsOffset(sym_plt_tail="Test")),
    ],
)
def test_flexabsoffset_replacebytes_base(y, exp):
    _, v = to_instruction(y)
    assert v.base == exp


# --- from patchfile/kobopatch/elf_test.go ------------------------------------------


@pytest.mark.parametrize(
    "name,y,err",
    [
        ("ClearDynFlag", "- ElfClearDynFlag: {Tag: FLAGS_1, Mask: 0x1}", ""),
        ("ClearDynFlagDT", "- ElfClearDynFlag: {Tag: DT_FLAGS, Mask: 0x8}", ""),
        ("ClearDynFlagBadTag", "- ElfClearDynFlag: {Tag: NEEDED, Mask: 0x1}", "unsupported dynamic tag"),
        ("ClearDynFlagBindNow", "- ElfClearDynFlag: {Tag: BIND_NOW, Mask: 0x1}", "not a bit field"),
        ("ClearDynFlagNoMask", "- ElfClearDynFlag: {Tag: FLAGS_1}", "Mask must not be 0"),
        ("RemoveDynEntry", "- ElfRemoveDynEntry: {Tag: BIND_NOW}", ""),
        ("RemoveDynEntryBad", "- ElfRemoveDynEntry: {Tag: FLAGS_1}", "only BIND_NOW"),
        ("DisablePhdr", "- ElfDisableProgramHeader: {Type: PT_GNU_RELRO}", ""),
        ("DisablePhdrShort", "- ElfDisableProgramHeader: {Type: GNU_RELRO}", ""),
        ("DisablePhdrBad", "- ElfDisableProgramHeader: {Type: PT_LOAD}", "unsupported program header type"),
        ("LazyBinding", "- ElfLazyBinding: true", ""),
        ("LazyBindingFalse", "- ElfLazyBinding: false", "must be true"),
        ("StubVoid", "- StubFunction: {Sym: _ZN3Foo3barEv, Return: void}", ""),
        ("StubInt", "- StubFunction: {Sym: _ZN3Foo3barEv, Return: 1}", ""),
        ("StubNoReturn", "- StubFunction: {Sym: _ZN3Foo3barEv}", "Return must be specified"),
        ("StubBigReturn", "- StubFunction: {Sym: _ZN3Foo3barEv, Return: 256}", "between 0 and 255"),
        ("StubBadReturn", "- StubFunction: {Sym: _ZN3Foo3barEv, Return: maybe}", "integer from 0 to 255 or void"),
        ("NopCall", "- NopCall: {Sym: _ZN3Foo3barEv, Rel: 0xc, Target: _ZN3Foo3bazEv}", ""),
        ("NopCallNoTarget", "- NopCall: {Sym: _ZN3Foo3barEv, Rel: 0xc}", "Sym and Target must be specified"),
        ("Tombstone", "- Tombstone: usr/local/Kobo/libboggle.so", ""),
        ("TombstoneNotAlone", "- Tombstone: usr/local/Kobo/libboggle.so\n  - ElfLazyBinding: true", "must be the only instruction"),
    ],
)
def test_elf_instructions_parse_validate(name, y, err):
    try:
        ps = K.parse(("Test:\n  - Enabled: yes\n  " + y + "\n").encode())
        ps.validate()
        e = None
    except K.KobopatchError as ex:
        e = str(ex)
    if err == "":
        assert e is None
    else:
        assert e is not None and err in e


def test_elf_instructions_apply(fwfile):
    orig = fwfile("usr/local/Kobo/libnickel.so.1.0.0")
    buf = bytearray(orig)
    ps = K.parse(
        b"""
Lazy binding:
  - Enabled: yes
  - ElfLazyBinding: true
Stub promo:
  - Enabled: yes
  - StubFunction: {Sym: _ZN18HomePageController22maybeShowKoboPlusPromoEv, Return: void}
No sketch timer:
  - Enabled: yes
  - NopCall: {Sym: _ZN11ReadingView8nextPageEv, Rel: 0xc, Target: _ZN10SketchNote16disableWithTimerEi}
  - NopCall: {Sym: _ZN11ReadingView8prevPageEv, Rel: 0xc, Target: _ZN10SketchNote16disableWithTimerEi}
Disabled:
  - Enabled: no
  - StubFunction: {Sym: _ZN11ReadingView8nextPageEv, Return: 0}
"""
    )
    pt = Patcher(buf)
    ps.apply_to(pt)
    for off, exp in [
        (0xF4, b"\x00\x00\x00\x00"),
        (0x1684AB4 - 8, b"\x00\x00\x00\x00"),
        (0xD5F2C0, bytes([0x70, 0x47, 0x00, 0xBF])),
        (0xF5BB28, bytes([0x00, 0xBF, 0x00, 0xBF])),
        (0xF5BAE4, bytes([0x00, 0xBF, 0x00, 0xBF])),
        (0xF5BB1C, orig[0xF5BB1C:0xF5BB20]),
    ]:
        assert bytes(buf[off : off + len(exp)]) == exp, hex(off)
    pt.check_elf(orig)
    with pytest.raises(K.KobopatcherError):
        ps.apply_to(Patcher(buf))


def test_tombstone_apply():
    ps = K.parse(b"Tombstone:\n  - Enabled: yes\n  - Tombstone: usr/local/Kobo/libboggle.so\n")
    pt = Patcher(b"\x7fELF...")
    pt.set_path("./usr/local/Kobo/libboggle.so")
    ps.apply_to(pt)
    assert len(pt.get_bytes()) == 0
    pt = Patcher(b"\x7fELF...")
    pt.set_path("./usr/local/Kobo/libsudoku.so")
    with pytest.raises(K.KobopatcherError):
        ps.apply_to(pt)


# --- YAML dialect ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "y,exp",
    [
        ("BaseAddress: 0x10", 16),
        ("BaseAddress: 010", 8),  # Go ParseInt base 0: C-style octal
        ("BaseAddress: 0o10", 8),
        ("BaseAddress: 0b101", 5),
        ("BaseAddress: 1_000", 1000),
        ("BaseAddress: 1.9", 1),  # yaml.v3 truncates floats into ints
    ],
)
def test_yaml_ints(y, exp):
    _, v = to_instruction(y)
    assert v.offset == exp and v.sym is None


def test_yaml_quoted_number_is_a_symbol():
    _, v = to_instruction('BaseAddress: "16"')
    assert v.sym == "16" and v.offset is None


@pytest.mark.parametrize("v,exp", [("yes", True), ("Yes", True), ('"on"', True), ("no", False), ("OFF", False), ("true", True), ("n", False)])
def test_yaml_bools(v, exp):
    ps = K.parse(("P:\n  - Enabled: %s\n  - FindZlib: x\n" % v).encode())
    assert ps.parsed["P"].enabled is exp


def test_yaml_bad_bool():
    with pytest.raises(K.KobopatchError) as e:
        K.parse(b"P:\n  - Enabled: maybe\n  - FindZlib: x\n")
    assert "cannot unmarshal !!str `maybe` into kobopatch.Enabled" in str(e.value)


def test_yaml_duplicate_keys():
    with pytest.raises(K.KobopatchError) as e:
        K.parse(b"P:\n  - FindZlib: x\nP:\n  - FindZlib: y\n")
    assert 'line 3: mapping key "P" already defined at line 1' in str(e.value)
    with pytest.raises(K.KobopatchError) as e:
        K.parse(b"P:\n  - ReplaceString: {Find: a, Find: b, Replace: c}\n")
    assert 'mapping key "Find" already defined' in str(e.value)


def test_yaml_tabs():
    with pytest.raises(K.KobopatchError) as e:
        K.parse(b"P:\n\t- FindZlib: x\n")
    assert str(e.value).startswith("patch file contains tabs")


def test_yaml_bytes_need_a_sequence():
    with pytest.raises(K.KobopatchError) as e:
        K.parse(b"P:\n  - ReplaceBytes: {Find: abc, Replace: def}\n")
    assert "cannot unmarshal !!str `abc` into []uint8" in str(e.value)
    ps = K.parse(b"P:\n  - ReplaceBytes: {Find: [1, 0x2], Replace: [3, 4]}\n")
    assert ps.parsed["P"].instructions[0].inst.find == [1, 2]


def test_null_instruction(both_modes):
    exc = GoPanic if both_modes else K.KobopatchError
    with pytest.raises(exc):
        K.parse(b"P:\n  - Enabled:\n  - FindZlib: x\n")


def test_sorted_application_order(capsys):
    ps = K.parse(b"b:\n  - Enabled: yes\n  - ReplaceString: {Find: a, Replace: b}\na:\n  - Enabled: no\n  - ReplaceString: {Find: a, Replace: b}\n")
    ps.apply_to(Patcher(b"aaa"))
    assert capsys.readouterr().out == "  SKIP  `a`\n  APPLY `b`\n"


def test_patch_group_conflict():
    ps = K.parse(
        b"a:\n  - Enabled: yes\n  - PatchGroup: g\n  - FindZlib: x\nb:\n  - Enabled: yes\n  - PatchGroup: g\n  - FindZlib: x\n"
    )
    with pytest.raises(K.KobopatchError) as e:
        ps.validate()
    assert str(e.value) == 'patch "b": more than one patch enabled in PatchGroup "g" (other patch is "a")'


def test_flexabsoffset_rel(both_modes):
    """kobopatch added Rel twice for {Offset, Rel}."""
    f = K.FlexAbsOffset(offset=0x10, rel=4)
    assert f.resolve(Patcher(b"")) == (0x18 if both_modes else 0x14)
