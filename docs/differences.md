# Differences from kobopatch

kobopatcher behaves like kobopatch 0.16 (plus the ELF instructions), except for
these deliberate changes:

1. `{Offset: X, Rel: Y}` in `BaseAddress`/`Base`/`FindInstBLX` and friends means
   X + Y. kobopatch computed X + 2Y.
2. `ReplaceBytesNOP` counts its `Offset` from the base address like every other
   instruction. kobopatch treated it as an absolute offset.
3. `FindZlib`: a stylesheet at offset 0 can be found, and the last
   whitespace-normalising comparison treats both sides the same way. When a
   `ReplaceZlib` result is too big, 4-space indentation is removed first (kobopatch
   looked for 5 spaces).
4. patch32lsb: negative offsets are read as 32-bit two's complement
   (`FFFFFFF0` = −16; kobopatch turned it into −240), `patch_name` doesn't carry
   over to the next `<Patch>`, and patches apply in file order.
5. Symbol addresses are converted from virtual addresses to file offsets, and
   the PLT decoder works with virtual addresses throughout. (On Kobo's libraries
   the two are equal, so this changes nothing there.)
6. The ELF checks reject a file that isn't 32-bit ARM. (kobopatch's check only
   rejected files that were neither.)
7. Situations where kobopatch crashed (negative offsets, a stylesheet at the
   very end of a file, malformed patch32lsb strings, an instruction without a
   value) are normal errors.
8. Everything happens in a fixed order: several patch files for one target in
   `kobopatch.yaml` order, overrides, translations, extra files and symlinks in
   the order written, and errors and checksums sorted. kobopatch used random
   orders for these.
9. Command line: a config path like `dir/kobopatch.yaml` works; `patchFormat` is
   optional; `.yml` is accepted; `--help` exits with status 0; `-t` can test
   `Tombstone` patches; `kobopatcher-apply` takes the format from the file
   extension; there is a `--version` option; and there is no 60-second pause on
   Windows (kobopatcher is a terminal program).
10. Error messages show values instead of Go pointer addresses, and YAML syntax
    errors are worded by PyYAML.
11. C++ names nested more than a few thousand levels deep can't be demangled on
    Windows (Python's stack is smaller there); no real symbol is nested that
    deeply.

The banner and help name kobopatcher instead of kobopatch.

`KOBOPATCHER_GO_COMPAT=1` switches all of these back to kobopatch's behaviour,
which is how the comparisons in [parity.md](parity.md) were made. It only affects
patching; the guided mode, `restore`, `device` and the menu are kobopatcher
additions (without a `kobopatch.yaml`, kobopatcher then reports the missing file
like kobopatch instead of starting the guided mode).
