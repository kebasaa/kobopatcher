"""The kobopatch YAML patch format.

A patch file maps patch names to lists of single-key instructions:

    Patch name:
      - Enabled: no
      - Description: What it does.
      - PatchGroup: Optional group (at most one patch per group may be enabled)
      - BaseAddress: 0x1234
      - ReplaceBytes: {Offset: 4, FindH: 00 20, ReplaceH: 01 20}

Patches are applied in alphabetical order of their names; the base address
is reset to 0 before each patch.
"""

from .. import yamlnodes as Y
from ..compat import go_compat
from ..errors import GoPanic, KobopatcherError
from ..gofmt import hexbytes_spaced, quote
from ..patchlib import asm
from . import out

_PTR = "0xc000012345"  # stands in for Go pointer addresses in %#v output


class KobopatchError(KobopatcherError):
    pass


def _log(msg: str) -> None:
    from . import log

    log(msg)


# --- FlexAbsOffset ------------------------------------------------------------


class FlexAbsOffset:
    """An absolute offset given directly (Offset), by symbol (Sym), by PLT
    entry (SymPLT) or by the PLT entry's Thumb tail stub (SymPLTTail), plus
    an optional relative offset (Rel). A plain integer or string can be used
    in place of the mapping."""

    __slots__ = ("offset", "sym", "sym_plt", "sym_plt_tail", "inline", "rel")
    go_type = "kobopatch.FlexAbsOffset"

    def __init__(self, offset=None, sym=None, sym_plt=None, sym_plt_tail=None, inline=False, rel=None):
        self.offset, self.sym, self.sym_plt, self.sym_plt_tail = offset, sym, sym_plt, sym_plt_tail
        self.inline, self.rel = inline, rel

    def go_repr(self, typ=None) -> str:
        typ = typ or self.go_type
        if not go_compat():
            parts = []
            for n, v in (("Offset", self.offset), ("Sym", self.sym), ("SymPLT", self.sym_plt), ("SymPLTTail", self.sym_plt_tail), ("Rel", self.rel)):
                if v is not None:
                    if isinstance(v, int):
                        v = ("-0x%X" % -v if v < 0 else "0x%X" % v) if n != "Rel" else str(v)
                    else:
                        v = quote(v)
                    parts.append("%s: %s" % (n, v))
            return "{" + ", ".join(parts) + "}"

        def p(v, t):
            return "(*%s)(nil)" % t if v is None else "(*%s)(%s)" % (t, _PTR)

        return "%s{Offset:%s, Sym:%s, SymPLT:%s, SymPLTTail:%s, Inline:%s, Rel:%s}" % (
            typ,
            p(self.offset, "int32"),
            p(self.sym, "string"),
            p(self.sym_plt, "string"),
            p(self.sym_plt_tail, "string"),
            "true" if self.inline else "false",
            p(self.rel, "int32"),
        )

    def validate(self):
        if self.offset is not None and self.offset < 0:
            raise KobopatchError("offset must be positive, got %d" % self.offset)
        c = sum(v is not None for v in (self.offset, self.sym, self.sym_plt, self.sym_plt_tail))
        if c == 0:
            raise KobopatchError("no offset method specified (%s)" % self.go_repr())
        if c > 1:
            raise KobopatchError("multiple offset methods specified (%s)" % self.go_repr())

    def resolve(self, pt) -> int:
        self.validate()
        rel = self.rel or 0
        if self.offset is not None:
            off = self.offset + rel
            if not go_compat():
                return _i32(off)
            # kobopatch added Rel a second time below
        elif self.sym is not None:
            off = pt.resolve_sym(self.sym)
        elif self.sym_plt is not None:
            off = pt.resolve_sym_plt(self.sym_plt)
        else:
            off = pt.resolve_sym_plt_tail(self.sym_plt_tail)
        return _i32(off + rel)

    def to_yaml(self) -> str:
        if self.inline:
            if self.offset is not None:
                return str(self.offset)
            if self.sym is not None:
                return _yscalar(self.sym)
        parts = []
        for n, v in (("Offset", self.offset), ("Sym", self.sym), ("SymPLT", self.sym_plt), ("SymPLTTail", self.sym_plt_tail), ("Rel", self.rel)):
            if v is not None:
                parts.append("%s: %s" % (n, v if isinstance(v, int) else _yscalar(v)))
        return "{" + ", ".join(parts) + "}"

    def __eq__(self, other):
        return isinstance(other, FlexAbsOffset) and all(getattr(self, a) == getattr(other, a) for a in self.__slots__)


def _i32(v: int) -> int:
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v & 0x80000000 else v


def _yscalar(s: str) -> str:
    return s if s and all(c.isalnum() or c in "_.:$" for c in s) and not s[0].isdigit() else quote(s)


_FLEX_DATA = Y.Struct(
    "kobopatch.FlexAbsOffsetData",
    [
        ("Offset", "offset", Y.Ptr(Y.Int(32))),
        ("Sym", "sym", Y.Ptr(Y.String())),
        ("SymPLT", "sym_plt", Y.Ptr(Y.String())),
        ("SymPLTTail", "sym_plt_tail", Y.Ptr(Y.String())),
        ("Rel", "rel", Y.Ptr(Y.Int(32))),
    ],
)


def _flex_unmarshal(node):
    try:
        return FlexAbsOffset(offset=Y.decode_strict(node, Y.Int(32)), inline=True)
    except Y.YAMLError:
        pass
    try:
        return FlexAbsOffset(sym=Y.decode_strict(node, Y.String()), inline=True)
    except Y.YAMLError:
        pass
    try:
        d = Y.decode_strict(node, _FLEX_DATA)
    except Y.YAMLError as e:
        raise Y.YAMLError("line %d: %s" % (Y.line(node), e)) from None
    return FlexAbsOffset(**d)


FLEX = Y.Custom("kobopatch.FlexAbsOffset", _flex_unmarshal)


# --- StubReturn -----------------------------------------------------------------


class StubReturn:
    __slots__ = ("void", "value")

    def __init__(self, void=False, value=0):
        self.void, self.value = void, value

    def __str__(self):
        return "void" if self.void else str(self.value)


def _stubret_unmarshal(node):
    try:
        return StubReturn(value=Y.decode_strict(node, Y.Int(32, unsigned=True)))
    except Y.YAMLError:
        pass
    try:
        if Y.decode_strict(node, Y.String()) == "void":
            return StubReturn(void=True)
    except Y.YAMLError:
        pass
    raise Y.YAMLError("line %d: Return must be an integer from 0 to 255 or void" % Y.line(node))


# --- instructions -----------------------------------------------------------------


class Instruction:
    """Base class: a decoded instruction (a struct or a named scalar)."""

    name = "?"
    modifies = True

    def apply_to(self, pt, log):
        raise NotImplementedError


class _Scalar(Instruction):
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


class Enabled(_Scalar):
    name, modifies = "Enabled", False


class Description(_Scalar):
    name, modifies = "Description", False


class PatchGroup(_Scalar):
    name, modifies = "PatchGroup", False


class BaseAddress(_Scalar):
    name = "BaseAddress"

    def apply_to(self, pt, log):
        b = self.value
        log("BaseAddress(%s)" % b.go_repr("kobopatch.BaseAddress"))
        try:
            offset = b.resolve(pt)
        except KobopatcherError as e:
            raise KobopatchError("BaseAddress: resolve address (%s): %s" % (b.go_repr("kobopatch.BaseAddress"), e)) from None
        log("  BaseAddress(%d)" % offset)
        pt.base_address(offset)


class FindBaseAddressHex(_Scalar):
    name = "FindBaseAddressHex"

    def apply_to(self, pt, log):
        log("FindBaseAddressHex(%s)" % quote(self.value))
        try:
            buf = _sscanf_hex(self.value.replace(" ", ""))
        except ValueError as e:
            raise KobopatchError("FindBaseAddressHex: error parsing hex: %s" % e) from None
        try:
            pt.find_base_address(buf)
        except KobopatcherError as e:
            raise KobopatchError("FindBaseAddressHex: %s" % e) from None


def _sscanf_hex(s: str) -> bytes:
    """fmt.Sscanf(s, "%x\\n", &[]byte)."""
    i = 0
    hexd = "0123456789abcdefABCDEF"
    while i < len(s) and s[i] in hexd:
        i += 1
    digits = s[:i]
    if not digits:
        raise ValueError("expected integer" if i == len(s) and not s else "expected integer")
    if len(digits) % 2:
        # Go scans pairs of hex digits; an odd count leaves one digit behind
        digits, i = digits[:-1], i - 1
        if not digits:
            raise ValueError("expected integer")
    rest = s[i:]
    if rest not in ("", "\n"):
        raise ValueError("unexpected newline" if rest.startswith("\n") else "newline in format does not match input")
    return bytes.fromhex(digits)


class FindBaseAddressString(_Scalar):
    name = "FindBaseAddressString"

    def apply_to(self, pt, log):
        log("FindBaseAddressString(%s) | hex:%s" % (quote(self.value), self.value.encode("utf-8", "surrogateescape").hex()))
        pt.find_base_address_string(self.value)


class FindZlib(_Scalar):
    name = "FindZlib"

    def apply_to(self, pt, log):
        log("FindZlib(%s) | hex:%s" % (quote(self.value), self.value.encode("utf-8", "surrogateescape").hex()))
        pt.find_zlib(self.value)


class FindZlibHash(_Scalar):
    name = "FindZlibHash"

    def apply_to(self, pt, log):
        log("FindZlibHash(%s) | hex:%s" % (quote(self.value), self.value.encode("utf-8", "surrogateescape").hex()))
        pt.find_zlib_hash(self.value)


class FindBaseAddressSymbol(_Scalar):
    """Deprecated: use BaseAddress instead."""

    name = "FindBaseAddressSymbol"

    def apply_to(self, pt, log):
        log("FindBaseAddressSymbol(%s)" % quote(self.value))
        pt.find_base_address_symbol(self.value)


class ElfLazyBinding(_Scalar):
    name = "ElfLazyBinding"

    def apply_to(self, pt, log):
        log("ElfLazyBinding(%s)" % ("true" if self.value else "false"))
        if not self.value:
            raise KobopatchError("ElfLazyBinding: must be true")
        changes = []
        try:
            changes = pt.elf_lazy_binding()
        finally:
            for c in changes:
                log("  -> %s" % c)


class Tombstone(_Scalar):
    name = "Tombstone"

    def apply_to(self, pt, log):
        log("Tombstone(%s)" % quote(self.value))
        pt.tombstone(self.value)


class _Struct(Instruction):
    fields = ()

    def __init__(self):
        for _, attr, t in self.fields:
            setattr(self, attr, t.zero())


def _struct(cls):
    cls.T = Y.Struct("kobopatch." + cls.name, cls.fields, cls)
    return cls


@_struct
class FindReplaceString(_Struct):
    name = "FindReplaceString"
    fields = [("Find", "find", Y.String()), ("Replace", "replace", Y.String()), ("MustMatchLength", "must_match_length", Y.Bool())]

    def apply_to(self, pt, log):
        log("FindReplaceString(%s, %s)" % (quote(self.find), quote(self.replace)))
        try:
            pt.find_base_address_string(self.find)
            pt.replace_string(0, self.find, self.replace)
        except KobopatcherError as e:
            raise KobopatchError("FindReplaceString: %s" % e) from None


@_struct
class ReplaceString(_Struct):
    name = "ReplaceString"
    fields = [
        ("Offset", "offset", Y.Int(32)),
        ("Find", "find", Y.String()),
        ("Replace", "replace", Y.String()),
        ("MustMatchLength", "must_match_length", Y.Bool()),
    ]

    def apply_to(self, pt, log):
        log("ReplaceString(%d, %s, %s)" % (self.offset, quote(self.find), quote(self.replace)))
        pt.replace_string(self.offset, self.find, self.replace)


@_struct
class ReplaceInt(_Struct):
    name = "ReplaceInt"
    fields = [("Offset", "offset", Y.Int(32)), ("Find", "find", Y.Int(8, True)), ("Replace", "replace", Y.Int(8, True))]

    def apply_to(self, pt, log):
        log("ReplaceInt(%d, 0x%x, 0x%x)" % (self.offset, self.find, self.replace))
        pt.replace_int(self.offset, self.find, self.replace)


@_struct
class ReplaceFloat(_Struct):
    name = "ReplaceFloat"
    fields = [("Offset", "offset", Y.Int(32)), ("Find", "find", Y.Float()), ("Replace", "replace", Y.Float())]

    def apply_to(self, pt, log):
        log("ReplaceFloat(%d, %r, %r)" % (self.offset, self.find, self.replace))
        pt.replace_float(self.offset, self.find, self.replace)


_BYTES = Y.Slice(Y.Int(8, True))


def _hexdecode(s: str) -> bytes:
    """hex.DecodeString after removing spaces, with Go's error texts."""
    s = s.replace(" ", "")
    hexd = "0123456789abcdefABCDEF"
    for i, c in enumerate(s):
        if c not in hexd:
            if i == len(s) - 1 and len(s) % 2 == 1:
                break
            raise ValueError("encoding/hex: invalid byte: %s" % _go_rune_q(c))
    if len(s) % 2 == 1:
        # Go checks the pairs it can decode first
        for c in s[:-1]:
            if c not in hexd:
                raise ValueError("encoding/hex: invalid byte: %s" % _go_rune_q(c))
        raise ValueError("encoding/hex: odd length hex string")
    return bytes.fromhex(s)


def _go_rune_q(c: str) -> str:
    """%#U of a byte (hex.InvalidByteError): U+0067 'g'."""
    b = c.encode("utf-8", "surrogateescape")[0]
    ch = chr(b)
    if ch.isprintable() and b < 0x80:
        return "U+%04X '%s'" % (b, ch)
    return "U+%04X" % b


@_struct
class ReplaceBytes(_Struct):
    name = "ReplaceBytes"
    fields = [
        ("Base", "base", Y.Ptr(FLEX)),
        ("Offset", "offset", Y.Int(32)),
        ("Find", "find", _BYTES),
        ("Replace", "replace", _BYTES),
        ("FindH", "find_h", Y.Ptr(Y.String())),
        ("ReplaceH", "replace_h", Y.Ptr(Y.String())),
        ("FindInstBLX", "find_inst_blx", Y.Ptr(FLEX)),
        ("ReplaceInstBLX", "replace_inst_blx", Y.Ptr(FLEX)),
        ("FindInstBW", "find_inst_bw", Y.Ptr(FLEX)),
        ("ReplaceInstBW", "replace_inst_bw", Y.Ptr(FLEX)),
        ("ReplaceInstNOP", "replace_inst_nop", Y.Ptr(Y.Bool())),
        ("FindBLX", "find_blx", Y.Ptr(Y.Int(32, True))),
        ("CheckOnly", "check_only", Y.Ptr(Y.Bool())),
    ]

    def apply_to(self, pt, log):
        log("ReplaceBytes(...)")
        cur_orig = pt.get_cur()
        cur = cur_orig
        find = bytes(self.find or b"")
        replace = bytes(self.replace or b"")
        find_inst_blx = self.find_inst_blx
        restore = False
        try:
            if self.base is not None:
                log("  Base.Resolve(%s)" % self.base.go_repr())
                try:
                    off = self.base.resolve(pt)
                except KobopatcherError as e:
                    raise KobopatchError("ReplaceBytes: expand Base=%s: %s" % (self.base.go_repr(), e)) from None
                cur = off
                log("    -> Offset: 0x%X" % off)
                try:
                    pt.base_address(cur)
                except KobopatcherError as e:
                    raise KobopatchError("ReplaceBytes: update cur to 0x%X: %s" % (cur, e)) from None
                restore = True
            if self.find_h is not None:
                try:
                    find = _hexdecode(self.find_h)
                except ValueError as e:
                    raise KobopatchError("ReplaceBytes: expand FindH=%s: %s" % (quote(self.find_h), e)) from None
            if self.replace_h is not None:
                try:
                    replace = _hexdecode(self.replace_h)
                except ValueError as e:
                    raise KobopatchError("ReplaceBytes: expand ReplaceH=%s: %s" % (quote(self.replace_h), e)) from None
            if self.find_blx is not None:
                find_inst_blx = FlexAbsOffset(offset=_i32(self.find_blx))
            pc = cur + self.offset
            for attr, fl, label, gen in (
                ("find", find_inst_blx, "FindInstBLX", asm.asm_blx),
                ("replace", self.replace_inst_blx, "ReplaceInstBLX", asm.asm_blx),
                ("find", self.find_inst_bw, "FindInstB", asm.asm_bw),
                ("replace", self.replace_inst_bw, "ReplaceInstB", asm.asm_bw),
            ):
                if fl is None:
                    continue
                try:
                    tgt = fl.resolve(pt)
                except KobopatcherError as e:
                    raise KobopatchError("ReplaceBytes: expand %s=%s: %s" % (label, fl.go_repr(), e)) from None
                buf = gen(pc & 0xFFFFFFFF, tgt & 0xFFFFFFFF)
                log("  %s(0x%X, 0x%X) -> %s" % (gen.__name__, pc, tgt, buf.hex()))
                if attr == "find":
                    find = buf
                else:
                    replace = buf
            if self.replace_inst_nop is not None:
                if not self.replace_inst_nop:
                    raise KobopatchError("ReplaceBytes: ReplaceInstNOP must either be true or unspecified")
                if len(find) % 2 != 0:
                    raise KobopatchError("ReplaceBytes: find not a multiple of 2 (len=%d)" % len(find))
                replace = b"\x00\x46" * (len(find) // 2)
            if self.check_only:
                if replace:
                    raise KobopatchError("ReplaceBytes: CheckOnly is true, but Replace is not empty")
                log("  CheckBytes(%d, %s)" % (self.offset, find.hex()))
                pt.replace_bytes(self.offset, find, find)
                return
            if find_inst_blx is not None and self.replace_inst_blx is not None:
                if (find_inst_blx.sym_plt is not None) != (self.replace_inst_blx.sym_plt is not None):
                    raise KobopatchError(
                        "ReplaceBytes: for safety, you cannot replace a BLX to a PLT entry with one to a non-PLT entry or vice-versa "
                        "(to suppress this warning, split the ReplaceBytes into two steps using placeholder bytes)"
                    )
            if self.find_inst_bw is not None and self.replace_inst_bw is not None:
                if (self.find_inst_bw.sym_plt_tail is not None) != (self.replace_inst_bw.sym_plt_tail is not None):
                    raise KobopatchError(
                        "ReplaceBytes: for safety, you cannot replace a B.W to a PLT entry's tail stub with one to a non-PLT "
                        "entry's tail stub or vice-versa (to suppress this warning, split the ReplaceBytes into two steps "
                        "using placeholder bytes)"
                    )
            log("  ReplaceBytes(%d, %s, %s) [cur:0x%X + off:%d -> abs:0x%X]" % (self.offset, find.hex(), replace.hex(), cur, self.offset, self.offset + cur))
            pt.replace_bytes(self.offset, find, replace)
        finally:
            if restore:
                log("  BaseAddress(0x%X) [restore]" % cur_orig)
                try:
                    pt.base_address(cur_orig)
                except KobopatcherError:
                    raise KobopatchError("ReplaceBytes: restore overridden cur to 0x%X" % cur_orig) from None


@_struct
class ReplaceZlib(_Struct):
    name = "ReplaceZlib"
    fields = [("Offset", "offset", Y.Int(32)), ("Find", "find", Y.String()), ("Replace", "replace", Y.String())]

    def apply_to(self, pt, log):
        log("ReplaceZlib(%d, %s, %s)" % (self.offset, quote(self.find), quote(self.replace)))
        pt.replace_zlib(self.offset, self.find, self.replace)


_REPL = Y.Struct(
    'struct { Find string "yaml:\\"Find\\""; Replace string "yaml:\\"Replace\\"" }',
    [("Find", "find", Y.String()), ("Replace", "replace", Y.String())],
)


@_struct
class ReplaceZlibGroup(_Struct):
    name = "ReplaceZlibGroup"
    fields = [("Offset", "offset", Y.Int(32)), ("Replacements", "replacements", Y.Slice(_REPL))]

    def apply_to(self, pt, log):
        repls = [(r["find"], r["replace"]) for r in (self.replacements or [])]
        log("ReplaceZlibGroup(%d, %r)" % (self.offset, repls))
        pt.replace_zlib_group(self.offset, repls)


@_struct
class ElfClearDynFlag(_Struct):
    name = "ElfClearDynFlag"
    fields = [("Tag", "tag", Y.String()), ("Mask", "mask", Y.Int(32, True))]

    def apply_to(self, pt, log):
        from ..patchlib.elffile import dt_name

        log("ElfClearDynFlag(%s, 0x%X)" % (quote(self.tag), self.mask))
        try:
            tag = parse_dyn_tag(self.tag)
        except KobopatchError as e:
            raise KobopatchError("ElfClearDynFlag: %s" % e) from None
        old, found = pt.elf_clear_dyn_flag(tag, self.mask)
        if not found:
            log("  -> %s not present, nothing to do" % dt_name(tag))
            out("    Warning: ElfClearDynFlag: %s not present, nothing changed\n" % dt_name(tag))
            return
        log("  -> %s: 0x%X -> 0x%X" % (dt_name(tag), old, old & ~self.mask & 0xFFFFFFFF))


@_struct
class ElfRemoveDynEntry(_Struct):
    name = "ElfRemoveDynEntry"
    fields = [("Tag", "tag", Y.String())]

    def apply_to(self, pt, log):
        from ..patchlib.elffile import dt_name

        log("ElfRemoveDynEntry(%s)" % quote(self.tag))
        try:
            tag = parse_dyn_tag(self.tag)
        except KobopatchError as e:
            raise KobopatchError("ElfRemoveDynEntry: %s" % e) from None
        if not pt.elf_remove_dyn_entry(tag):
            log("  -> %s not present, nothing to do" % dt_name(tag))
            out("    Warning: ElfRemoveDynEntry: %s not present, nothing changed\n" % dt_name(tag))
            return
        log("  -> %s removed" % dt_name(tag))


@_struct
class ElfDisableProgramHeader(_Struct):
    name = "ElfDisableProgramHeader"
    fields = [("Type", "type", Y.String())]

    def apply_to(self, pt, log):
        from ..patchlib.elffile import pt_name

        log("ElfDisableProgramHeader(%s)" % quote(self.type))
        try:
            typ = parse_prog_type(self.type)
        except KobopatchError as e:
            raise KobopatchError("ElfDisableProgramHeader: %s" % e) from None
        if not pt.elf_disable_program_header(typ):
            raise KobopatchError("ElfDisableProgramHeader: no %s program header" % pt_name(typ))
        log("  -> %s -> PT_NULL" % pt_name(typ))


@_struct
class StubFunction(_Struct):
    name = "StubFunction"
    fields = [("Sym", "sym", Y.String()), ("Return", "ret", Y.Ptr(Y.Custom("kobopatch.StubReturn", _stubret_unmarshal)))]

    def apply_to(self, pt, log):
        if self.ret is None:
            raise KobopatchError("StubFunction: Return must be specified (an integer or void)")
        log("StubFunction(%s, %s)" % (quote(self.sym), self.ret))
        off, orig = pt.stub_function(self.sym, None if self.ret.void else self.ret.value)
        log("  -> stubbed at 0x%X (original bytes: %s)" % (off, hexbytes_spaced(orig)))


@_struct
class NopCall(_Struct):
    name = "NopCall"
    fields = [("Sym", "sym", Y.String()), ("Rel", "rel", Y.Int(32)), ("Target", "target", Y.String())]

    def apply_to(self, pt, log):
        log("NopCall(%s, 0x%X, %s)" % (quote(self.sym), self.rel, quote(self.target)))
        off = pt.nop_call(self.sym, self.rel, self.target)
        log("  -> replaced call at 0x%X with NOPs" % off)


def _expand_hex(s):
    if s is None:
        return None
    try:
        return _hexdecode(s)
    except ValueError as e:
        raise KobopatchError("error expanding shorthand hex `%s`: %s" % (s, e)) from None


@_struct
class ReplaceBytesAtSymbol(_Struct):
    """Deprecated: use ReplaceBytes with Base instead."""

    name = "ReplaceBytesAtSymbol"
    fields = [
        ("Symbol", "symbol", Y.String()),
        ("Offset", "offset", Y.Int(32)),
        ("FindH", "find_h", Y.Ptr(Y.String())),
        ("ReplaceH", "replace_h", Y.Ptr(Y.String())),
        ("FindBLX", "find_blx", Y.Ptr(Y.Int(32, True))),
        ("Find", "find", _BYTES),
        ("Replace", "replace", _BYTES),
    ]

    def apply_to(self, pt, log):
        find = bytes(self.find or b"")
        replace = bytes(self.replace or b"")
        f = _expand_hex(self.find_h)
        if f is not None:
            find = f
        r = _expand_hex(self.replace_h)
        if r is not None:
            replace = r
        log("  ReplaceBytesAtSymbol(%s, %d, %s, %s)" % (quote(self.symbol), self.offset, find.hex(), replace.hex()))
        try:
            pt.find_base_address_symbol(self.symbol)
        except KobopatcherError as e:
            raise KobopatchError("ReplaceBytesAtSymbol: %s" % e) from None
        if self.find_blx is not None:
            find = asm.asm_blx((pt.get_cur() + self.offset) & 0xFFFFFFFF, self.find_blx)
        try:
            pt.replace_bytes(self.offset, find, replace)
        except KobopatcherError as e:
            raise KobopatchError("ReplaceBytesAtSymbol: %s" % e) from None


@_struct
class ReplaceBytesNOP(_Struct):
    """Deprecated: use ReplaceBytes with ReplaceInstNOP instead."""

    name = "ReplaceBytesNOP"
    fields = [
        ("Offset", "offset", Y.Int(32)),
        ("FindH", "find_h", Y.Ptr(Y.String())),
        ("FindBLX", "find_blx", Y.Ptr(Y.Int(32, True))),
        ("Find", "find", _BYTES),
    ]

    def apply_to(self, pt, log):
        find = bytes(self.find or b"")
        f = _expand_hex(self.find_h)
        if f is not None:
            find = f
        if self.find_blx is not None:
            find = asm.asm_blx((pt.get_cur() + self.offset) & 0xFFFFFFFF, self.find_blx)
        log("ReplaceBytesNOP(%d, %s)" % (self.offset, find.hex()))
        pt.replace_bytes_nop(self.offset, find)


@_struct
class ReplaceBLX(_Struct):
    """Deprecated: use ReplaceBytes with FindInstBLX and ReplaceInstBLX instead."""

    name = "ReplaceBLX"
    fields = [("Offset", "offset", Y.Int(32)), ("Find", "find", Y.Int(32, True)), ("Replace", "replace", Y.Int(32, True))]

    def apply_to(self, pt, log):
        log("ReplaceBLX(%d, 0x%x, 0x%x)" % (self.offset, self.find, self.replace))
        pt.replace_blx(self.offset, self.find, self.replace)


# instruction name -> (class, YAML type), in kobopatch's field order
INSTRUCTIONS = {}
for _cls, _t in [
    (Enabled, Y.Bool("kobopatch.Enabled")),
    (Description, Y.String("kobopatch.Description")),
    (PatchGroup, Y.String("kobopatch.PatchGroup")),
    (BaseAddress, Y.Custom("kobopatch.BaseAddress", _flex_unmarshal)),
    (FindBaseAddressHex, Y.String("kobopatch.FindBaseAddressHex")),
    (FindBaseAddressString, Y.String("kobopatch.FindBaseAddressString")),
    (FindZlib, Y.String("kobopatch.FindZlib")),
    (FindZlibHash, Y.String("kobopatch.FindZlibHash")),
    (FindReplaceString, FindReplaceString.T),
    (ReplaceString, ReplaceString.T),
    (ReplaceInt, ReplaceInt.T),
    (ReplaceFloat, ReplaceFloat.T),
    (ReplaceBytes, ReplaceBytes.T),
    (ReplaceZlib, ReplaceZlib.T),
    (ReplaceZlibGroup, ReplaceZlibGroup.T),
    (ElfClearDynFlag, ElfClearDynFlag.T),
    (ElfRemoveDynEntry, ElfRemoveDynEntry.T),
    (ElfDisableProgramHeader, ElfDisableProgramHeader.T),
    (ElfLazyBinding, Y.Bool("kobopatch.ElfLazyBinding")),
    (StubFunction, StubFunction.T),
    (NopCall, NopCall.T),
    (Tombstone, Y.String("kobopatch.Tombstone")),
    (FindBaseAddressSymbol, Y.String("kobopatch.FindBaseAddressSymbol")),
    (ReplaceBytesAtSymbol, ReplaceBytesAtSymbol.T),
    (ReplaceBytesNOP, ReplaceBytesNOP.T),
    (ReplaceBLX, ReplaceBLX.T),
]:
    INSTRUCTIONS[_cls.name] = (_cls, _t)


def parse_dyn_tag(s: str) -> int:
    from ..patchlib.elffile import DT_BIND_NOW, DT_FLAGS, DT_FLAGS_1, dt_name

    for t in (DT_FLAGS, DT_FLAGS_1, DT_BIND_NOW):
        n = dt_name(t)
        if s.lower() == n.lower() or s.lower() == n[3:].lower():
            return t
    raise KobopatchError("unsupported dynamic tag %s (expected FLAGS, FLAGS_1, or BIND_NOW)" % quote(s))


def parse_prog_type(s: str) -> int:
    from ..patchlib.elffile import PT_GNU_RELRO, pt_name

    n = pt_name(PT_GNU_RELRO)
    if s.lower() == n.lower() or s.lower() == n[3:].lower():
        return PT_GNU_RELRO
    raise KobopatchError("unsupported program header type %s (expected PT_GNU_RELRO)" % quote(s))


# --- patch sets -------------------------------------------------------------------


class ParsedInstruction:
    __slots__ = ("index", "line", "inst")

    def __init__(self, index, line, inst):
        self.index, self.line, self.inst = index, line, inst


class ParsedPatch:
    def __init__(self):
        self.enabled = False
        self.description = ""
        self.patch_groups = []
        self.instructions = []


_TOP = Y.Map(Y.String(), Y.Node(), "map[string]yaml.Node")
_PATCHNODE = Y.Slice(Y.Node(), "kobopatch.PatchNode")
_INSTNODE = Y.Map(Y.String(), Y.Node(), "kobopatch.InstructionNode")


def _to_instruction(instnode):
    """InstructionNode.ToInstruction: returns (name, value)."""
    if not instnode:
        raise KobopatchError("expected instruction, got nothing")
    found = None
    for name, node in instnode.items():
        if found is not None:
            raise KobopatchError("line %d: multiple types found in instruction, maybe you forgot a '-'" % Y.line(node))
        if name not in INSTRUCTIONS:
            raise KobopatchError("line %d: unknown instruction type %s" % (Y.line(node), quote(name)))
        try:
            value = Y.decode_strict(node, Y.Ptr(INSTRUCTIONS[name][1]))
        except Y.YAMLError as e:
            raise KobopatchError("line %d: error decoding instruction: %s" % (Y.line(node), e)) from None
        found = (name, value)
    return found


def _instnode_line(instnode, default):
    for node in (instnode or {}).values():
        return Y.line(node)
    return default


class PatchSet:
    def __init__(self):
        self.parsed = {}

    def names(self):
        """Patch names in alphabetical (byte-wise) order."""
        return sorted(self.parsed, key=lambda s: s.encode("utf-8", "surrogateescape"))

    def set_enabled(self, patch: str, enabled: bool) -> None:
        if patch not in self.parsed:
            raise KobopatchError("no such patch %s" % quote(patch))
        self.parsed[patch].enabled = enabled

    def validate(self) -> None:
        from ..patchlib.elffile import DT_BIND_NOW

        used = {}
        for name in self.names():
            patch = self.parsed[name]
            seen = set()
            for g in patch.patch_groups:
                if g in seen:
                    raise KobopatchError("patch %s: duplicate PatchGroup instruction for PatchGroup %s" % (quote(name), quote(g)))
                seen.add(g)
                if patch.enabled:
                    if g in used:
                        raise KobopatchError(
                            "patch %s: more than one patch enabled in PatchGroup %s (other patch is %s)" % (quote(name), quote(g), quote(used[g]))
                        )
                    used[g] = name
            if not patch.instructions:
                raise KobopatchError("patch %s: no instructions which modify anything" % quote(name))
            for pi in patch.instructions:
                pfx = "patch %s: line %d: inst %d" % (quote(name), pi.line, pi.index)
                i = pi.inst
                if isinstance(i, ElfClearDynFlag):
                    try:
                        t = parse_dyn_tag(i.tag)
                    except KobopatchError as e:
                        raise KobopatchError("%s: ElfClearDynFlag: %s" % (pfx, e)) from None
                    if t == DT_BIND_NOW:
                        raise KobopatchError("%s: ElfClearDynFlag: BIND_NOW is not a bit field (use ElfRemoveDynEntry)" % pfx)
                    if i.mask == 0:
                        raise KobopatchError("%s: ElfClearDynFlag: Mask must not be 0" % pfx)
                elif isinstance(i, ElfRemoveDynEntry):
                    try:
                        t = parse_dyn_tag(i.tag)
                    except KobopatchError as e:
                        raise KobopatchError("%s: ElfRemoveDynEntry: %s" % (pfx, e)) from None
                    if t != DT_BIND_NOW:
                        raise KobopatchError("%s: ElfRemoveDynEntry: only BIND_NOW can be removed" % pfx)
                elif isinstance(i, ElfDisableProgramHeader):
                    try:
                        parse_prog_type(i.type)
                    except KobopatchError as e:
                        raise KobopatchError("%s: ElfDisableProgramHeader: %s" % (pfx, e)) from None
                elif isinstance(i, ElfLazyBinding):
                    if not i.value:
                        raise KobopatchError("%s: ElfLazyBinding: must be true (remove the patch or disable it instead)" % pfx)
                elif isinstance(i, StubFunction):
                    if i.sym == "":
                        raise KobopatchError("%s: StubFunction: Sym must be specified" % pfx)
                    if i.ret is None:
                        raise KobopatchError("%s: StubFunction: Return must be specified (an integer or void)" % pfx)
                    if not i.ret.void and i.ret.value > 0xFF:
                        raise KobopatchError("%s: StubFunction: Return must be between 0 and 255" % pfx)
                elif isinstance(i, NopCall):
                    if i.sym == "" or i.target == "":
                        raise KobopatchError("%s: NopCall: Sym and Target must be specified" % pfx)
                elif isinstance(i, Tombstone):
                    if i.value == "":
                        raise KobopatchError("%s: Tombstone: path must be specified" % pfx)
                    if len(patch.instructions) != 1:
                        raise KobopatchError("%s: Tombstone: must be the only instruction in the patch" % pfx)
                elif isinstance(i, ReplaceBytesNOP):
                    if len(i.find or b"") % 2 != 0:
                        raise KobopatchError(
                            "%s: ReplaceBytesNOP: find must be a multiple of 2 to be replaced with 00 46 (MOV r0, r0)" % pfx
                        )
                elif isinstance(i, (ReplaceString, FindReplaceString)):
                    if i.must_match_length:
                        d = len(i.replace.encode("utf-8", "surrogateescape")) - len(i.find.encode("utf-8", "surrogateescape"))
                        if d < 0:
                            raise KobopatchError("%s: %s: replacement string %d chars too short" % (pfx, i.name, -d))
                        if d > 0:
                            raise KobopatchError("%s: %s: replacement string %d chars too long" % (pfx, i.name, d))
                elif isinstance(i, FindZlibHash):
                    if len(i.value.encode("utf-8", "surrogateescape")) != 40:
                        raise KobopatchError("%s: FindZlibHash: hash must be 40 chars long" % pfx)
                elif isinstance(i, ReplaceZlibGroup):
                    if not i.replacements:
                        raise KobopatchError("%s: ReplaceZlibGroup: no replacements specified" % pfx)
                    for n, r in enumerate(i.replacements):
                        if r["find"] == "" or r["replace"] == "":
                            raise KobopatchError("%s: ReplaceZlibGroup: replacement %d: Find and Replace must be set" % (pfx, n + 1))

    def apply_to(self, pt) -> None:
        _log("validating patch file\n")
        try:
            self.validate()
        except KobopatchError as e:
            err = KobopatchError("invalid patch file: %s" % e)
            out("  Error: %s\n" % err)
            raise err from None
        _log("looping over patches\n")
        for name in self.names():
            patch = self.parsed[name]
            _log("  Patch(%s) enabled=%s\n" % (quote(name), "true" if patch.enabled else "false"))
            pt.reset_base_address()
            if not patch.enabled:
                out("  SKIP  `%s`\n" % name)
                continue
            out("  APPLY `%s`\n" % name)
            for pi in patch.instructions:
                _log("      %s index=%d line=%d\n" % (pi.inst.name, pi.index, pi.line))
                try:
                    pi.inst.apply_to(pt, lambda m: _log("        %s\n" % m))
                except GoPanic:
                    raise
                except KobopatcherError as e:
                    err = KobopatchError("could not apply patch %s: line %d: inst %d: %s" % (quote(name), pi.line, pi.index, e))
                    _log("        %s" % err)
                    out("    Error: %s\n" % err)
                    raise err from None


def parse(buf: bytes) -> PatchSet:
    """Parse a kobopatch YAML patch file."""
    _log("parsing patch file: unmarshaling to map[string]yaml.Node\n")
    try:
        psn = Y.unmarshal(buf, _TOP) or {}
    except (Y.YAMLError, UnicodeDecodeError) as e:
        if b"\t" in bytes(buf):
            raise KobopatchError("patch file contains tabs (it should be indented with spaces, not tabs): %s" % e) from None
        raise KobopatchError(str(e)) from None

    ps = PatchSet()
    for name, node in psn.items():
        try:
            pn = Y.decode_strict(node, _PATCHNODE) or []
            ns = [Y.decode_strict(n, _INSTNODE) for n in pn]
        except Y.YAMLError as e:
            raise KobopatchError("line %d: patch %s: %s" % (Y.line(node), quote(name), e)) from None
        p = ps.parsed[name] = ParsedPatch()
        for i, instnode in enumerate(ns):
            try:
                iname, value = _to_instruction(instnode)
            except KobopatchError as e:
                raise KobopatchError("line %d: patch %s: instruction %d: %s" % (Y.line(node), quote(name), i + 1, e)) from None
            cls = INSTRUCTIONS[iname][0]
            ln = _instnode_line(instnode, Y.line(node))
            if value is None:
                # e.g. "- Enabled:" with no value
                if go_compat():
                    raise GoPanic("incomplete implementation (missing implementation of PatchableInstruction) for type <nil>")
                raise KobopatchError("line %d: patch %s: instruction %d: %s has no value" % (Y.line(node), quote(name), i + 1, iname))
            if cls is Enabled:
                p.enabled = bool(value)
            elif cls is Description:
                if p.description != "":
                    raise KobopatchError("patch %s: line %d: instruction %d: duplicate Description instruction" % (quote(name), ln, i + 1))
                p.description = value
            elif cls is PatchGroup:
                p.patch_groups.append(value)
            else:
                inst = value if isinstance(value, Instruction) else cls(value)
                p.instructions.append(ParsedInstruction(i + 1, ln, inst))
    return ps
