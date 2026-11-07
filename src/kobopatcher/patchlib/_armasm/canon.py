# Part of the Python port of rsc.io/arm/armasm
# (rsc.io/arm v0.0.0-20150420010332-9c32f2193064); see LICENSE.

"""Canonical text form of a decode result, for differential tests against Go.

``canonical(hexbytes)`` produces exactly the line that the Go reference
helper (see tools/parity/armasm_diff.py) prints for the same input::

    <hex>\\tERR\\t<error>
    <hex>\\tOK\\t<Op>\\t<int(Op)>\\t<Len>\\t<Enc %#x>\\t<%T=%#v of Args[0..3] joined by |>\\t<Inst.String()>\\t<GNUSyntax>\\t<%+q of []Inst{inst}>
"""

from __future__ import annotations

from .decode import DecodeError, decode
from .gnu import GNUSyntax
from .inst import ModeARM, gosyntax, gotype, quote_insts


def canonical(hexbytes: str, mode=ModeARM) -> str:
    try:
        inst = decode(bytes.fromhex(hexbytes), mode)
    except DecodeError as e:
        return f"{hexbytes}\tERR\t{e}"
    args = "|".join(f"{gotype(a)}={gosyntax(a)}" for a in inst.Args)
    return (f"{hexbytes}\tOK\t{inst.Op}\t{int(inst.Op)}\t{inst.Len}\t{inst.Enc:#x}\t{args}\t"
            f"{inst}\t{GNUSyntax(inst)}\t{quote_insts([inst])}")
