"""The Patcher: applies changes to a binary, starting from a base address."""

import hashlib
import struct

from ..compat import go_compat
from ..errors import GoPanic, KobopatcherError
from ..gofmt import quote
from . import asm
from .zlibcss import compress, inflate_lenient, strip_whitespace, zlib_selftest


class PatchError(KobopatcherError):
    pass


class ZlibItem:
    __slots__ = ("offset", "css")

    def __init__(self, offset: int, css: str):
        self.offset, self.css = offset, css


class Patcher:
    """Applies patches to a buffer in place. All operations start from cur."""

    def __init__(self, buf):
        self.buf = buf if isinstance(buf, bytearray) else bytearray(buf)
        self.cur = 0
        self.hook = None
        self._dynsyms = None
        self._dynsyms_pltgot = False
        # for the ELF instructions (see elfpatch)
        self.path = ""
        self.elf_syms = None
        self.elf_stubs = []
        self.elf_nops = []

    # --- basics -----------------------------------------------------------

    def get_bytes(self) -> bytearray:
        return self.buf

    def get_cur(self) -> int:
        return self.cur

    def reset_base_address(self) -> None:
        self.cur = 0

    def set_hook(self, fn) -> None:
        """fn(offset, find, replace) is called before every change; if it
        raises, the change is aborted. None removes the hook."""
        self.hook = fn

    def set_path(self, name: str) -> None:
        """Record the path of the file being patched (used by Tombstone)."""
        self.path = name

    def _call_hook(self, off, find, replace):
        if self.hook is not None:
            try:
                self.hook(off, bytes(find), None if replace is None else bytes(replace))
            except Exception as e:  # noqa: BLE001 - the hook's error is passed on
                raise PatchError("hook returned error: %s" % e) from e

    def _check_start(self, off: int, what: str):
        """kobopatch slices buf[off:], which panics for a negative offset."""
        if off < 0:
            if go_compat():
                raise GoPanic("runtime error: slice bounds out of range [%d:]" % off)
            raise PatchError("%s: offset before start of buf" % what)

    def _pos(self, offset: int) -> int:
        """cur + offset (kobopatch computed it in int32, which wraps around)."""
        pos = self.cur + offset
        if go_compat():
            pos &= 0xFFFFFFFF
            if pos & 0x80000000:
                pos -= 1 << 32
        return pos

    def base_address(self, offset: int) -> None:
        if offset < 0:
            raise PatchError("BaseAddress: offset less than 0")
        if offset >= len(self.buf):
            raise PatchError("BaseAddress: offset greater than length of buf")
        self.cur = offset

    def find_base_address(self, find: bytes) -> None:
        if len(find) > len(self.buf):
            raise PatchError("FindBaseAddress: length of bytes to find greater than buf")
        i = self.buf.find(find)
        if i < 0:
            raise PatchError("FindBaseAddress: could not find bytes")
        self.cur = i

    def find_base_address_string(self, find: str) -> None:
        try:
            self.find_base_address(find.encode("utf-8", "surrogateescape"))
        except PatchError as e:
            raise PatchError("FindBaseAddressString: %s" % e) from None

    # --- replacements -----------------------------------------------------

    def replace_bytes(self, offset: int, find: bytes, replace: bytes) -> None:
        self._replace("ReplaceBytes", offset, bytes(find), bytes(replace), True)

    def replace_string(self, offset: int, find: str, replace: str) -> None:
        fb = find.encode("utf-8", "surrogateescape")
        rb = replace.encode("utf-8", "surrogateescape")
        if len(rb) < len(fb):
            # like patch32lsb: terminate the shorter replacement with a NUL and
            # keep the rest of the original
            rb += b"\x00"
            rb = rb + fb[len(rb) :]
        self._replace("ReplaceString", offset, fb, rb, False)

    def replace_int(self, offset: int, find: int, replace: int) -> None:
        self._replace("ReplaceInt", offset, bytes([find & 0xFF]), bytes([replace & 0xFF]), True)

    def replace_float(self, offset: int, find: float, replace: float) -> None:
        self._replace("ReplaceFloat", offset, struct.pack("<d", find), struct.pack("<d", replace), True)

    def _replace(self, what, offset, fbuf, rbuf, strict):
        try:
            self._replace_value(offset, fbuf, rbuf, strict, what)
        except PatchError as e:
            raise PatchError("%s: %s" % (what, e)) from None

    def _replace_value(self, offset, fbuf, rbuf, strict, what):
        pos = self._pos(offset)
        if len(self.buf) < pos:
            raise PatchError("offset past end of buf")
        if len(fbuf) != len(rbuf):
            raise PatchError("length mismatch in byte replacement")
        if len(self.buf) < pos + len(fbuf):
            raise PatchError("replaced value past end of buf")
        self._check_start(pos, "offset")
        idx = self.buf.find(fbuf, pos)
        if idx < 0:
            raise PatchError("could not find specified bytes")
        if strict and idx != pos:
            raise PatchError("could not find specified bytes at offset")
        self._call_hook(pos, fbuf, rbuf)
        self.buf[idx : idx + len(fbuf)] = rbuf

    def replace_text(self, find: str, replace: str, count=None) -> None:
        """Replace text in a text file; the file may change size.

        find must occur exactly once (or exactly count times). Refused on ELF
        files, where changing the size would break every offset.
        """
        if self.buf[:4] == b"\x7fELF":
            raise PatchError("ReplaceText: refusing to change the size of an ELF file (use ReplaceString)")
        fb = find.encode("utf-8", "surrogateescape")
        rb = replace.encode("utf-8", "surrogateescape")
        n = self.buf.count(fb)
        want = 1 if count is None else count
        if n != want:
            if n == 0:
                raise PatchError("ReplaceText: could not find %s" % quote(find))
            raise PatchError("ReplaceText: %s occurs %d times, expected %d" % (quote(find), n, want))
        self._call_hook(self.buf.find(fb), fb, rb)
        self.buf[:] = bytes(self.buf).replace(fb, rb)

    # --- zlib -------------------------------------------------------------

    def _check_zlib(self):
        msg = zlib_selftest()
        if msg:
            raise PatchError(
                "zlib self-test failed (%s); kobopatcher needs classic zlib for the zlib instructions, "
                "see the README (Troubleshooting)" % msg
            )

    def extract_zlib(self):
        """All zlib streams that contain CSS, as ZlibItems."""
        from .css import is_css

        self._check_zlib()
        # FindZlib/FindZlibHash extract every stream each time; cache the
        # result for the current content of the buffer
        key = hashlib.sha1(self.buf).digest()
        cached = getattr(self, "_zlib_cache", None)
        if cached is not None and cached[0] == key:
            return [ZlibItem(z.offset, z.css) for z in cached[1]]
        items = []
        buf = self.buf
        i = buf.find(b"\x78\x9c")
        n = len(buf)
        while 0 <= i < n - 2:
            r = inflate_lenient(buf, i)
            if r.err not in (None, "corrupt input", "invalid checksum"):
                if r.err == "invalid header":
                    raise PatchError("could not initialize zlib reader: zlib: invalid header")
                raise PatchError("could not decompress stream: %s" % r.err)
            dbuf = r.data
            if dbuf:
                try:
                    css = dbuf.decode("utf-8")
                except UnicodeDecodeError:
                    css = None
                if css is not None and is_css(css):
                    tbuf = compress(dbuf)
                    if not buf.startswith(tbuf, i) or len(tbuf) < 4:
                        raise PatchError(
                            "sanity check failed: recompressed data does not match original (this is a bug, so please report it)"
                        )
                    items.append(ZlibItem(i, css))
            i = buf.find(b"\x78\x9c", i + 1)
        self._zlib_cache = (key, items)
        return [ZlibItem(z.offset, z.css) for z in items]

    def find_zlib(self, find: str) -> None:
        """Move cur to the CSS zlib stream containing find (not sensitive to whitespace)."""
        if len(find.encode("utf-8", "surrogateescape")) > len(self.buf):
            raise PatchError("FindZlib: length of string to find greater than buf")
        try:
            z = self.extract_zlib()
        except PatchError as e:
            raise PatchError("FindZlib: could not extract zlib streams: %s" % e) from None
        compat = go_compat()
        found = None

        def hit(css, f):
            return f in css or strip_whitespace(f) in strip_whitespace(css)

        for zi in z:
            css = zi.css
            ok = hit(css, find)
            if not ok:
                # handle the minification done by ReplaceZlib
                css = css.replace("\n    ", "\n").replace("\n  ", "\n").replace("\n ", "\n")
                findm = find.replace("\n  ", "\n").replace("\n ", "\n").replace("\n    ", "\n")
                ok = hit(css, findm)
                if not ok:
                    css = css.replace(": ", ":").replace(" {", "{")
                    findm = findm.replace(": ", ":").replace(" {", "{")
                    ok = hit(css, findm)
                    if not ok:
                        if compat:
                            css = css.replace("\n", "").replace("{ ", "").replace("; ", "")
                            findm = findm.replace("{ ", "{").replace("; ", ";")
                        else:
                            css = css.replace("\n", "").replace("{ ", "{").replace("; ", ";")
                            findm = findm.replace("\n", "").replace("{ ", "{").replace("; ", ";")
                        ok = hit(css, findm)
            if ok:
                # kobopatch tracked "found" as a non-zero offset, so a stream
                # at offset 0 could never be found
                if found is not None and not (compat and found == 0):
                    raise PatchError("FindZlib: substring to find is not unique")
                found = zi.offset
        if found is None or (compat and found == 0):
            raise PatchError("FindZlib: could not find string")
        self.cur = found

    def find_zlib_hash(self, hash_: str) -> None:
        """Move cur to the CSS zlib stream with this SHA1 (see kobopatcher-cssextract)."""
        if len(hash_.encode("utf-8", "surrogateescape")) != 40:
            raise PatchError("FindZlibHash: invalid hash")
        try:
            z = self.extract_zlib()
        except PatchError as e:
            raise PatchError("FindZlibHash: could not extract zlib streams: %s" % e) from None
        want = strip_whitespace(hash_)
        for zi in z:
            if hashlib.sha1(zi.css.encode("utf-8")).hexdigest() == want:
                self.cur = zi.offset
                return
        raise PatchError("FindZlibHash: could not find hash")

    def replace_zlib(self, offset: int, find: str, replace: str) -> None:
        self.replace_zlib_group(offset, [(find, replace)])

    def replace_zlib_group(self, offset: int, repl) -> None:
        """Replace parts of the CSS zlib stream at cur+offset, all at once."""
        self._check_zlib()
        pos = self._pos(offset)
        if go_compat() and pos + 2 < 0:
            # Go checks the upper bound of buf[pos:pos+2] first
            raise GoPanic("runtime error: slice bounds out of range [:%d]" % (pos + 2))
        self._check_start(pos, "ReplaceZlib")
        if self.buf[pos : pos + 2] != b"\x78\x9c":
            if len(self.buf) < pos + 2 and go_compat():
                raise GoPanic("runtime error: slice bounds out of range [:%d] with capacity %d" % (pos + 2, len(self.buf)))
            raise PatchError("ReplaceZlib: not a zlib stream")
        r = inflate_lenient(self.buf, pos)
        if r.err not in (None, "corrupt input", "invalid checksum"):
            raise PatchError("ReplaceZlib: could not decompress stream: %s" % r.err)
        dbuf = r.data
        try:
            dbuf.decode("utf-8")
        except UnicodeDecodeError:
            dbuf = b""
        if not dbuf:
            raise PatchError("ReplaceZlib: not a valid zlib stream")
        tbuf = compress(dbuf)
        if not self.buf.startswith(tbuf, pos) or len(tbuf) < 4:
            raise PatchError(
                "ReplaceZlib: sanity check failed: recompressed original data does not match original (this is a bug, so please report it)"
            )
        for find, replace in repl:
            fb = find.encode("utf-8", "surrogateescape")
            if fb not in dbuf:
                find = find.replace("\n    ", "\n").replace("\n  ", "\n").replace("\n ", "\n")
                fb = find.encode("utf-8", "surrogateescape")
                if fb not in dbuf:
                    find = find.replace(": ", ":").replace(" {", "{")
                    fb = find.encode("utf-8", "surrogateescape")
                    if fb not in dbuf:
                        find = find.replace("\n", "").replace("; ", ";").replace("{ ", "{")
                        fb = find.encode("utf-8", "surrogateescape")
                        if fb not in dbuf:
                            raise PatchError(
                                "ReplaceZlib: find string not found in stream (%s)" % find.replace("\n", "\\n")
                            )
            dbuf = _replace_all(dbuf, fb, replace.encode("utf-8", "surrogateescape"))
        nbuf = compress(dbuf)
        if len(nbuf) > len(tbuf):
            # remove indentation to save space
            indent = b"\n     " if go_compat() else b"\n    "
            dbuf = dbuf.replace(indent, b"\n").replace(b"\n  ", b"\n").replace(b"\n ", b"\n")
            nbuf = compress(dbuf)
        if len(nbuf) > len(tbuf):
            # remove spaces after colons
            dbuf = dbuf.replace(b": ", b":").replace(b" {", b"{")
            nbuf = compress(dbuf)
        if len(nbuf) > len(tbuf):
            # remove newlines
            dbuf = dbuf.replace(b"\n", b"").replace(b"; ", b";").replace(b"{ ", b"{")
            nbuf = compress(dbuf)
        if len(nbuf) > len(tbuf):
            raise PatchError(
                "ReplaceZlib: new compressed data is %d bytes longer than old data (try removing whitespace or unnecessary css)"
                % (len(nbuf) - len(tbuf))
            )
        self._call_hook(pos, tbuf, nbuf)
        self.buf[pos : pos + len(nbuf)] = nbuf
        r2 = inflate_lenient(self.buf, pos)
        if r2.data != dbuf:
            raise PatchError(
                "ReplaceZlib: decompressed new data does not match new data (this is a bug, so please report it)"
            )

    # --- symbols ----------------------------------------------------------

    def extract_dynsyms(self, need_pltgot: bool):
        """The dynamic symbols, with PLT/GOT information if need_pltgot.

        Cached; the cache is only cleared by Tombstone (like kobopatch).
        """
        from .elffile import ElfFile
        from .syms import decdynsym

        if self._dynsyms is None or (need_pltgot and not self._dynsyms_pltgot):
            try:
                e = ElfFile(self.buf)
            except Exception as ex:  # noqa: BLE001
                raise PatchError("load elf: %s" % ex) from None
            try:
                ds = decdynsym(e, not need_pltgot)
            except Exception as ex:  # noqa: BLE001
                raise PatchError("load syms (pltgot: %s): %s" % ("true" if need_pltgot else "false", ex)) from None
            self._dynsyms = ds
            self._dynsyms_pltgot = need_pltgot
        return self._dynsyms

    def _get_dynsym(self, name, need_pltgot):
        try:
            ds = self.extract_dynsyms(need_pltgot)
        except PatchError as e:
            raise PatchError("get dynsyms: %s" % e) from None
        for s in ds:
            if s.name == name:
                return s
        for s in ds:
            if s.demangled == name:
                return s
        raise PatchError("no such symbol %s" % quote(name))

    def _sym_off(self, vaddr: int) -> int:
        """Symbol addresses are vaddrs; kobopatch used them as file offsets."""
        if go_compat():
            return vaddr
        from .elffile import PT_LOAD, ElfFile

        try:
            e = ElfFile(self.buf)
        except Exception:  # noqa: BLE001
            return vaddr
        for p in e.progs:
            if p.type == PT_LOAD and p.vaddr <= vaddr < p.vaddr + p.filesz:
                return vaddr - p.vaddr + p.off
        return vaddr

    def resolve_sym(self, name: str) -> int:
        try:
            s = self._get_dynsym(name, False)
        except PatchError as e:
            raise PatchError("ResolveSym(%s): %s" % (quote(name), e)) from None
        return self._sym_off(s.offset)

    def resolve_sym_plt(self, name: str) -> int:
        try:
            s = self._get_dynsym(name, True)
        except PatchError as e:
            raise PatchError("ResolveSymPLT(%s): %s" % (quote(name), e)) from None
        if s.offset_plt == 0:
            raise PatchError("ResolveSymPLT(%s) = %s: no PLT entry found" % (quote(name), s.go_repr()))
        return self._sym_off(s.offset_plt)

    def resolve_sym_plt_tail(self, name: str) -> int:
        try:
            s = self._get_dynsym(name, True)
        except PatchError as e:
            raise PatchError("ResolveSymPLTTail(%s): %s" % (quote(name), e)) from None
        if s.offset_plt == 0:
            raise PatchError("ResolveSymPLTTail(%s) = %s: no PLT entry found" % (quote(name), s.go_repr()))
        if s.offset_plt_tail == 0:
            raise PatchError("ResolveSymPLTTail(%s) = %s: no tail stub before PLT entry" % (quote(name), s.go_repr()))
        return self._sym_off(s.offset_plt_tail)

    # --- ELF (see elfpatch) ------------------------------------------------

    def elf_clear_dyn_flag(self, tag: int, mask: int):
        from . import elfpatch

        return elfpatch.elf_clear_dyn_flag(self, tag, mask)

    def elf_remove_dyn_entry(self, tag: int) -> bool:
        from . import elfpatch

        return elfpatch.elf_remove_dyn_entry(self, tag)

    def elf_disable_program_header(self, typ: int) -> bool:
        from . import elfpatch

        return elfpatch.elf_disable_program_header(self, typ)

    def elf_lazy_binding(self):
        from . import elfpatch

        return elfpatch.elf_lazy_binding(self)

    def stub_function(self, sym: str, ret):
        from . import elfpatch

        return elfpatch.stub_function(self, sym, ret)

    def nop_call(self, sym: str, rel: int, target: str) -> int:
        from . import elfpatch

        return elfpatch.nop_call(self, sym, rel, target)

    def tombstone(self, name: str) -> None:
        from . import elfpatch

        elfpatch.tombstone(self, name)

    def check_elf(self, orig: bytes) -> None:
        from . import elfpatch

        elfpatch.check_elf(self, orig)

    # --- deprecated helpers -------------------------------------------------

    def find_base_address_symbol(self, find: str) -> None:
        """Move cur to a symbol by its demangled C++ name.

        Warning: all symbols are off by one (the raw st_value, Thumb bit
        included) for historical reasons. Deprecated: use resolve_sym.
        """
        from .demangled import demangle
        from .elffile import ElfFile

        try:
            e = ElfFile(self.buf)
        except Exception as ex:  # noqa: BLE001
            raise PatchError("FindBaseAddressSymbol: could not open file as elf binary: %s" % ex) from None
        try:
            syms = e.dynamic_symbols()
        except Exception as ex:  # noqa: BLE001
            raise PatchError("FindBaseAddressSymbol: could not read dynsyms: %s" % ex) from None
        for sym in syms:
            name = demangle(sym.name)
            if name is None:
                name = sym.name
            if find != "" and find == name:
                self.cur = sym.value & 0xFFFFFFFF
                if self.cur & 0x80000000:
                    self.cur -= 1 << 32  # int32 like kobopatch
                return
        raise PatchError("FindBaseAddressSymbol: could not find symbol")

    def replace_blx(self, offset: int, find: int, replace: int) -> None:
        """Replace a BLX at cur+offset; find and replace are the target offsets.

        Deprecated: assemble the instruction with asm_blx and use replace_bytes.
        """
        pos = self.cur + offset
        if len(self.buf) < pos:
            raise PatchError("ReplaceBLX: offset past end of buf")
        self._check_start(pos, "ReplaceBLX")
        f, r = asm.asm_blx(pos, find), asm.asm_blx(pos, replace)
        if not self.buf.startswith(f, pos):
            raise PatchError("ReplaceBLX: could not find bytes")
        self._call_hook(pos, f, r)
        self.buf[pos : pos + len(r)] = r

    def replace_bytes_nop(self, offset: int, find: bytes) -> None:
        """Replace an instruction with 00 46 (MOV r0, r0) as many times as needed.

        kobopatch used offset as an absolute offset, ignoring the base
        address; kobopatcher makes it relative to cur like every other
        instruction. Deprecated: use ReplaceBytes with ReplaceInstNOP.
        """
        pos = offset if go_compat() else self.cur + offset
        if len(self.buf) < pos:
            raise PatchError("ReplaceBytesNOP: offset past end of buf")
        if len(find) % 2 != 0:
            raise PatchError("ReplaceBytesNOP: find not a multiple of 2")
        self._check_start(pos, "ReplaceBytesNOP")
        r = b"\x00\x46" * (len(find) // 2)
        if not self.buf.startswith(bytes(find), pos):
            raise PatchError("ReplaceBytesNOP: could not find bytes")
        self._call_hook(pos, find, r)
        self.buf[pos : pos + len(r)] = r


def _replace_all(b: bytes, old: bytes, new: bytes) -> bytes:
    """bytes.Replace(b, old, new, -1), including Go's behaviour for an empty old."""
    if old:
        return b.replace(old, new)
    # Go inserts new before every UTF-8 sequence and at the end
    s = b.decode("utf-8", "surrogateescape")
    nb = new
    out = [nb]
    for ch in s:
        out.append(ch.encode("utf-8", "surrogateescape"))
        out.append(nb)
    return b"".join(out)
