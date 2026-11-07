"""kobopatcher-symdump: dump the dynamic symbols of an ARMv6+ 32-bit ELF file.

Writes symdump.out.json in the current directory: one JSON object per symbol
with its name, address, index, type, GOT slot, PLT entry, PLT tail stub and
demangled name. Useful for writing patches with BaseAddress {Sym/SymPLT/...}.
"""

import json
import sys

from .patchlib.patcher import Patcher


def _go_json(obj) -> str:
    """json.Marshal: compact, HTML characters escaped."""
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return s.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        sys.stderr.write("symdump dumps symbol addresses from an ARMv6+ 32-bit ELF executable\n")
        sys.stderr.write("Usage: kobopatcher-symdump BINARY_FILE\n")
        return 1
    try:
        with open(argv[0], "rb") as f:
            buf = f.read()
        ds = Patcher(buf).extract_dynsyms(True)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write("Error: %s\n" % e)
        return 1
    with open("symdump.out.json", "w", encoding="utf-8") as f:
        f.write("[\n")
        for i, s in enumerate(ds):
            if i:
                f.write(",\n")
            f.write(
                _go_json(
                    {
                        "Name": s.name,
                        "Offset": s.offset,
                        "Index": s.index,
                        "Type": s.type,
                        "OffsetGOT": s.offset_got,
                        "OffsetPLT": s.offset_plt,
                        "OffsetPLTTail": s.offset_plt_tail,
                        "Demangled": s.demangled,
                    }
                )
            )
        f.write("]\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
