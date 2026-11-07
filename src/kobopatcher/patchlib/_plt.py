"""Decoding the ARM PLT into (PLT entry, Thumb tail stub, GOT slot) triples.

Each PLT entry is expected to be two ADDs and an LDR (the sequence the ARM
ELF ABI recommends and GCC/Clang emit), optionally preceded by a Thumb
`bx pc; mov r8, r8` stub. The GOT slot is found by emulating the three
instructions. See https://stackoverflow.com/a/32808179.
"""

from ..compat import go_compat
from ._armasm import (
    PC,
    R12,
    AddrOffset,
    AddrPostIndex,
    AddrPreIndex,
    DecodeError,
    Imm,
    ImmAlt,
    Mem,
    ModeARM,
    Op,
    Reg,
    ShiftLeft,
    ShiftRight,
    decode,
    gosyntax,
    quote_insts,
)

M32 = 0xFFFFFFFF


class PLTError(Exception):
    pass


def decode_plt(e):
    from .syms import SymsError

    plt = e.section(".plt")
    if plt is None:
        raise SymsError("read .plt: no such section")
    try:
        buf = memoryview(plt.data())
    except Exception as ex:  # noqa: BLE001
        raise SymsError("read .plt: %s" % ex) from None
    got = e.section(".got")
    if got is None:
        raise SymsError("read .got: no such section")
    compat = go_compat()
    # kobopatch used file offsets here (the same as vaddrs in the text segment
    # of Kobo's libraries); kobopatcher uses the vaddrs the code works with
    base = plt.offset if compat else plt.addr
    got_start = got.offset if compat else got.addr

    try:
        return _decode(buf, base, got_start, got.size)
    except PLTError as ex:
        raise SymsError(str(ex)) from None


def _decode(buf, base, got_start, got_size):
    asmbufo, asmbufi = [], []
    asmbuftail = 0
    pltents = []
    pc = base
    while len(buf) != 0:
        try:
            t = decode(buf, ModeARM)
        except DecodeError:
            if len(buf) >= 4 and buf[0] == 0x78 and buf[1] == 0x47 and buf[2] == 0xC0 and buf[3] == 0x46:
                # Thumb: bx pc; mov r8, r8
                asmbuftail = pc
                buf = buf[4:]
                pc = (pc + 4) & M32
                continue
            # probably another Thumb instruction: skip it (even if it is
            # also valid ARM, the checks below catch it)
            buf = buf[2:]
            pc = (pc + 2) & M32
            continue
        asmbufo.append(pc)
        asmbufi.append(t)
        buf = buf[t.Len :]
        pc = (pc + t.Len) & M32

        # the processing happens at each LDR
        if t.Op != Op.LDR:
            if len(asmbufi) > 8:
                raise PLTError("parse .plt: at 0x%X: expected LDR instruction somewhere, cur %s" % (pc, quote_insts(asmbufi)))
            continue

        if len(asmbufi) != 3 or asmbufi[0].Op != Op.ADD or asmbufi[1].Op != Op.ADD or asmbufi[2].Op != Op.LDR:
            # discard the junk at the start of the PLT
            if len(pltents) == 0:
                if ((pc - base) & M32) > 128:
                    raise PLTError(
                        "parse .plt: at 0x%X: more than 128 bytes of junk at start of PLT, cur %s" % (pc, quote_insts(asmbufi))
                    )
                asmbufo, asmbufi, asmbuftail = [], [], 0
                continue
            raise PLTError("parse .plt: at 0x%X: expected 2 ADD instructions before each LDR, got %s" % (pc, quote_insts(asmbufi)))

        r12 = _emulate(pc, asmbufo, asmbufi)
        if r12 < got_start:
            raise PLTError(
                "parse .plt: entry at 0x%X: emulated GOT offset (ip/r12) 0x%X before GOT at 0x%X - 0x%X (size: 0x%X)"
                % (asmbufo[0], r12, got_start, got_start + got_size, got_size)
            )
        pltents.append((asmbufo[0], asmbuftail, r12))
        asmbufo, asmbufi, asmbuftail = [], [], 0

    seen = {}
    for i, ent in enumerate(pltents):
        if ent[2] in seen:
            j = seen[ent[2]]
            raise PLTError(
                "parse .plt: internal error: duplicate emulated got offsets (this is likely a bug) in entry %s (prev: %s)"
                % (_ent_repr(ent), _ent_repr(pltents[j]))
            )
        seen[ent[2]] = i
    return pltents


def _ent_repr(ent):
    return "patchlib.pltent{PLTOffset:0x%x, PLTOffsetTail:0x%x, GOTOffset:0x%x}" % ent


def _emulate(pc, asmbufo, asmbufi):
    reg = {PC: 0, R12: 0}  # other registers aren't used
    q = quote_insts(asmbufi)

    def fail(inst, instpc, i, msg):
        return PLTError("parse .plt: at 0x%X: parse entry %s: emulate inst %s at 0x%X: arg %d: %s" % (pc, q, inst, instpc, i + 1, msg))

    for n, inst in enumerate(asmbufi):
        instpc = asmbufo[n]
        # in ARM state, PC reads as the address of the current instruction + 8
        reg[PC] = (instpc + 8) & M32
        if inst.Op == Op.ADD:
            dst = None
            new = 0
            for i, arg in enumerate(inst.Args):
                if i == 0:
                    if not (isinstance(arg, Reg) and arg == R12):
                        raise fail(inst, instpc, i, "expected dest register for add to be ip (r12)")
                    dst = R12
                elif i in (1, 2):
                    if isinstance(arg, Reg):
                        if arg not in reg:
                            raise fail(inst, instpc, i, "unsupported register %s" % arg)
                        new = (new + reg[arg]) & M32
                    elif isinstance(arg, Imm):
                        new = (new + int(arg)) & M32
                    elif isinstance(arg, ImmAlt):
                        new = (new + int(arg.Imm())) & M32
                    else:
                        raise fail(inst, instpc, i, "unsupported arg type for %s" % gosyntax(arg))
                elif i == 3:
                    if arg is not None:
                        raise fail(inst, instpc, i, "expected nothing, got %s" % gosyntax(arg))
            reg[dst] = new
        elif inst.Op == Op.LDR:
            dst = base = None
            sub = False
            new = 0
            mode = None
            for i, arg in enumerate(inst.Args):
                if i == 0:
                    if not (isinstance(arg, Reg) and arg == PC):
                        raise fail(inst, instpc, i, "expected dest register for ldr to be pc")
                    dst = PC
                elif i == 1:
                    if not isinstance(arg, Mem):
                        raise fail(inst, instpc, i, "unsupported arg type for %s" % gosyntax(arg))
                    if arg.Base not in reg:
                        raise fail(inst, instpc, i, "unsupported base register %s" % arg.Base)
                    base = arg.Base
                    if arg.Mode not in (AddrPostIndex, AddrPreIndex, AddrOffset):
                        raise fail(inst, instpc, i, "unsupported addressing mode for ldr arg")
                    mode = arg.Mode
                    if arg.Sign != 0:
                        sub = arg.Sign < 0
                        if arg.Index not in reg:
                            raise fail(inst, instpc, i, "unsupported index register %s" % arg.Index)
                        new = reg[arg.Index]
                        if arg.Shift == ShiftLeft:
                            new = (new << arg.Count) & M32
                        elif arg.Shift == ShiftRight:
                            new = new >> arg.Count
                        else:
                            raise fail(inst, instpc, i, "unsupported shift mode %s for ldr arg" % arg.Shift)
                    else:
                        if arg.Offset < 0:
                            sub, new = True, (-arg.Offset) & M32
                        else:
                            sub, new = False, arg.Offset & M32
                elif i in (2, 3):
                    if arg is not None:
                        raise fail(inst, instpc, i, "expected nothing, got %s" % gosyntax(arg))
            d = -new if sub else new
            if mode == AddrPostIndex:
                reg[dst] = reg[base]
                reg[base] = (reg[base] + d) & M32
            elif mode == AddrPreIndex:
                reg[dst] = (reg[base] + d) & M32
                reg[base] = (reg[base] + d) & M32
            else:
                reg[dst] = (reg[base] + d) & M32
    return reg[R12]
