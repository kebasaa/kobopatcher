# kobopatcher reference

The complete reference of the patch formats. For installation, the
configuration file and the command line, see the [README](../README.md).

- [Concepts](#concepts)
- [YAML patch files](#yaml-patch-files)
- [Meta instructions](#meta-instructions)
- [Finding the place to patch](#finding-the-place-to-patch)
- [Replacing data](#replacing-data)
- [Compressed CSS (zlib)](#compressed-css-zlib)
- [ELF instructions](#elf-instructions)
- [Deprecated instructions](#deprecated-instructions)
- [Values: numbers, strings, bytes](#values-numbers-strings-bytes)
- [The patch32lsb format](#the-patch32lsb-format)

## Concepts

- **The target** is one file inside `KoboRoot.tgz`, e.g.
  `usr/local/Kobo/libnickel.so.1.0.0`. A patch file always patches one target;
  `kobopatch.yaml` says which.
- **Offsets** are positions in the target file, counted in bytes from 0.
- **The base address** (also *cur*) is the position that `Offset`s count from.
  It is 0 at the start of each patch and is moved by `BaseAddress` and the
  `Find…` instructions.
- **Every replacement is checked.** The bytes to replace are given together with
  the replacement, and kobopatcher refuses the patch if the file doesn't
  contain them where expected. A patch written for one firmware version
  therefore fails cleanly on another instead of corrupting it.
- **Symbols** are the exported function and variable names of a program or
  library, as listed by `kobopatcher-symdump`. Kobo's code is C++, so names are
  *mangled* (`_ZN11ReadingView8nextPageEv`); most instructions also accept the
  demangled form (`ReadingView::nextPage()`), but the mangled name is unambiguous.
- **Thumb code.** Almost all of Kobo's code is ARM Thumb-2. Functions in Thumb
  mode have the lowest bit of their symbol value set; kobopatcher clears it when
  it resolves an address.

## YAML patch files

```yaml
Name of the first patch:
  - Enabled: no
  - Description: What it does, for the user.
  - PatchGroup: Optional group name
  - BaseAddress: 0x123456
  - ReplaceBytes: {Offset: 4, FindH: 4F F0 5A 01, ReplaceH: 4F F0 96 01}

Name of the second patch:
  - Enabled: yes
  - FindReplaceString: {Find: "Library", Replace: "Books  "}
```

- The file is a mapping from patch names to lists. Each list item is a mapping
  with **exactly one** key, the instruction. (A forgotten `-` puts two
  instructions into one item, which is reported as "multiple types found in
  instruction".)
- Patches are applied in alphabetical order of their names. The base address is
  reset to 0 before each patch. Instructions run in the order written.
- A patch needs at least one instruction that changes something, even when it
  is disabled.
- Duplicate patch names, unknown instructions, unknown fields and duplicate
  fields are errors.
- Indent with spaces. Tabs are reported as such.

Flow style (`{Key: value, ...}`) and block style are equivalent:

```yaml
  - ReplaceBytes:
      Offset: 4
      FindH: 4F F0 5A 01
      ReplaceH: 4F F0 96 01
```

## Meta instructions

| Instruction | Value | Meaning |
|---|---|---|
| `Enabled` | `yes`/`no` (also `true`/`false`, `on`/`off`) | Whether the patch is applied. Default: no. If given several times, the last one counts. `overrides` in `kobopatch.yaml` replaces it. |
| `Description` | text | Shown to users reading the file. At most one. Use `|` for several lines. |
| `PatchGroup` | text | At most one enabled patch per group (per patch file). A patch can be in several groups. In the menu, group members behave like radio buttons. |
| `Category` | text | kobopatcher extension: the menu category (`kobopatcher --list`, `--select`). Patches without one are listed under "Other". |
| `Experimental` | `yes`/`no` | kobopatcher extension: marks a new patch that hasn't been run on a device; the menu shows it as NEW/EXPERIMENTAL with a warning. |

## Finding the place to patch

### BaseAddress

Sets the base address.

```yaml
  - BaseAddress: 0x659DA4                                  # an offset
  - BaseAddress: _ZN11ReadingView8nextPageEv               # a symbol
  - BaseAddress: {Sym: _ZN11ReadingView8nextPageEv, Rel: 0xc}
  - BaseAddress: {SymPLT: _ZN7QWidget4showEv}              # the symbol's PLT entry
  - BaseAddress: {SymPLTTail: _ZN7QWidget4showEv}          # the Thumb stub before it
  - BaseAddress: {Offset: 0x659DA4, Rel: 8}
```

This value form, a *flexible offset*, is also used by `ReplaceBytes.Base`,
`FindInstBLX`, `ReplaceInstBLX`, `FindInstBW` and `ReplaceInstBW`:

| Field | Meaning |
|---|---|
| `Offset` | A file offset (≥ 0). A plain number in place of the mapping means `{Offset: n}`. |
| `Sym` | The address of a defined dynamic symbol (Thumb bit cleared). A plain text in place of the mapping means `{Sym: text}`; note that a *quoted* number is text, i.e. a symbol name. |
| `SymPLT` | The address of the symbol's PLT entry, i.e. where calls to an imported function go. |
| `SymPLTTail` | The address of the Thumb `bx pc` stub just before that PLT entry, used by Thumb tail calls (`b.w`). |
| `Rel` | Added to the address. Optional; may be negative. |

Exactly one of `Offset`, `Sym`, `SymPLT` and `SymPLTTail` must be given.

### FindBaseAddressString

```yaml
  - FindBaseAddressString: "a:link, a:visited, a:hover, a:active {"
```

Sets the base address to the first occurrence of the text (UTF-8) in the file.
Error if it doesn't occur.

### FindBaseAddressHex

```yaml
  - FindBaseAddressHex: 4F F0 5A 01 0B 46
```

Sets the base address to the first occurrence of the bytes. Spaces are ignored.

## Replacing data

### ReplaceBytes

```yaml
  - ReplaceBytes: {Offset: 0x10, Find: [0xBE, 0xF5, 0xAE, 0xE9], Replace: [0x00, 0x46, 0x00, 0x46]}
  - ReplaceBytes: {Offset: 0x10, FindH: BE F5 AE E9, ReplaceH: 00 46 00 46}
  - ReplaceBytes: {Offset: 0x10, FindH: BE F5 AE E9, ReplaceInstNOP: true}
  - ReplaceBytes: {Offset: 0x22, FindInstBLX: {SymPLT: _ZN7QWidget4showEv}, ReplaceInstBLX: {SymPLT: _ZN7QWidget4hideEv}}
  - ReplaceBytes: {Base: {Sym: _ZN11ReadingView8nextPageEv}, Offset: 0xc, FindH: 00 F0 10 F8, ReplaceH: 00 BF 00 BF}
  - ReplaceBytes: {Offset: 0x10, FindH: BE F5 AE E9, CheckOnly: true}
```

Replaces the bytes at base + `Offset`, which must be exactly the find bytes.
Find and replace must have the same length.

| Field | Meaning |
|---|---|
| `Offset` | Relative to the base address (default 0). |
| `Find`, `Replace` | Bytes as a list of numbers 0–255. |
| `FindH`, `ReplaceH` | Bytes as hex text; spaces are ignored. Override `Find`/`Replace`. |
| `FindInstBLX`, `ReplaceInstBLX` | Generate a Thumb-2 `BLX` (call into ARM code) from base + `Offset` to the given flexible offset. |
| `FindInstBW`, `ReplaceInstBW` | Generate a Thumb-2 `B.W` (branch) from base + `Offset` to the given flexible offset. |
| `ReplaceInstNOP` | `true`: replace with as many `00 46` (`mov r0, r0`) as needed. The find length must be even. |
| `FindBLX` | Deprecated: like `FindInstBLX: {Offset: n}`. |
| `Base` | A flexible offset to use as the base address for this instruction only. The base address is restored afterwards. |
| `CheckOnly` | `true`: only check that the find bytes are there; `Replace` must be empty. |

For safety, a `BLX` to a PLT entry (`SymPLT`) can't be replaced by one to a
non-PLT target or vice versa, and the same for `B.W` and `SymPLTTail`. (Split
the change into two `ReplaceBytes` with placeholder bytes if you really mean it.)

### ReplaceString

```yaml
  - ReplaceString: {Offset: 0x27, Find: "b", Replace: "_"}
  - ReplaceString: {Find: "/mnt/onboard/.kobo/dict/dicthtml%1", Replace: "/mnt/onboard/.kobo/dict/micthtml%1", MustMatchLength: true}
```

Replaces the **first occurrence at or after** base + `Offset`. A shorter
replacement is followed by a NUL byte and the rest of the original, so the
length stays the same; a longer one is an error. With `MustMatchLength: true`,
different lengths are an error when the file is loaded. Lengths are bytes of
UTF-8.

### FindReplaceString

```yaml
  - FindReplaceString: {Find: "Library", Replace: "Books"}
```

`FindBaseAddressString` followed by `ReplaceString` at offset 0. Also takes
`MustMatchLength`.

### ReplaceInt

```yaml
  - ReplaceInt: {Offset: 0x2, Find: 1, Replace: 2}
```

Replaces one byte (0–255) at base + `Offset`.

### ReplaceFloat

```yaml
  - ReplaceFloat: {Offset: 0x8, Find: 1.07, Replace: 0.85}
```

Replaces an 8-byte little-endian IEEE double at base + `Offset`.

### ReplaceText

```yaml
  - ReplaceText: {Find: "umount -a -r\n", Replace: "touch /etc/.onboard_clean 2>/dev/null\numount -a -r\n"}
  - ReplaceText: {Find: "echo a", Replace: "echo b", Count: 2}
```

kobopatcher extension for text files such as the boot scripts in
`etc/init.d/`. Replaces the text; unlike `ReplaceString`, the replacement may be
longer or shorter, so the file changes size. `Find` must occur exactly once (or
exactly `Count` times), so a script that changed between firmware versions is
refused instead of being edited in the wrong place. Refused on ELF files.
Several patches can edit the same script as long as their `Find` texts stay
unique.

## Compressed CSS (zlib)

`nickel` contains its Qt stylesheets compressed with zlib. These instructions
change them. They need Python's zlib module to produce exactly the output of
classic zlib, which kobopatcher checks.

### FindZlib

```yaml
  - FindZlib: "#boggleContainer[qApp_deviceIsPika=true]"
```

Sets the base address to the compressed stylesheet that contains the text. The
comparison ignores differences in whitespace and in the minification that
`ReplaceZlib` may apply. The text must occur in exactly one stylesheet.

### FindZlibHash

```yaml
  - FindZlibHash: cc9d7ce57f8746517ea692b7c65e9ed74c1d765b
```

Sets the base address to the stylesheet with this SHA1 (of the uncompressed
text), as shown by `kobopatcher-cssextract`.

### ReplaceZlib

```yaml
  - ReplaceZlib: {Offset: 0, Find: "qproperty-visible: false;", Replace: "qproperty-visible: true;"}
```

Replaces every occurrence of the text in the stylesheet at base + `Offset` and
recompresses it. The new compressed data must not be longer than the old; if
it is, kobopatcher first removes indentation, then spaces after `:` and before
`{`, then line breaks, and fails only if that still isn't enough. If the find
text isn't found as written, the same minification is tried on it.

### ReplaceZlibGroup

```yaml
  - ReplaceZlibGroup:
      Offset: 0
      Replacements:
        - {Find: "qproperty-visible: false;", Replace: "qproperty-visible: true;"}
        - {Find: "font-size: 12pt;", Replace: "font-size: 14pt;"}
```

Like `ReplaceZlib`, but all replacements are made before recompressing, so only
the final result has to fit.

## ELF instructions

These work on 32-bit little-endian ARM ELF files (Kobo's programs and
libraries). They locate what they change from the file's structure and check
that the change is safe; all writes check the original bytes like
`ReplaceBytes`. Use `--check-elf` with them.

### ElfLazyBinding

```yaml
  - ElfLazyBinding: true
```

Makes the library resolve imported functions on first use (lazy binding), as
Kobo's firmware did until 4.30, instead of resolving all of them when the
program starts. It clears `DF_1_NOW` in `DT_FLAGS_1` and `DF_BIND_NOW` in
`DT_FLAGS` (if set), removes the `DT_BIND_NOW` entry, and turns the
`PT_GNU_RELRO` program header into `PT_NULL` (lazy binding writes to the GOT,
which RELRO would make read-only).

It refuses the file if it isn't eagerly bound, if it has no `DT_PLTGOT` or no
`R_ARM_JUMP_SLOT` relocations, if the GOT slots don't all point to the PLT
header (which lazy resolution needs), or if `PT_GNU_RELRO` isn't inside a
writable segment. Afterwards nothing eager may be left. The value must be
`true`.

### ElfClearDynFlag

```yaml
  - ElfClearDynFlag: {Tag: FLAGS_1, Mask: 0x1}
```

Clears bits in `DT_FLAGS` or `DT_FLAGS_1` (`Tag`: `FLAGS`, `FLAGS_1`, with or
without `DT_`, any case). Warns and changes nothing if the tag is absent; fails
if the tag occurs twice or none of the bits are set.

### ElfRemoveDynEntry

```yaml
  - ElfRemoveDynEntry: {Tag: BIND_NOW}
```

Removes the `DT_BIND_NOW` entry (the only one supported) by moving the following
entries up and adding a second `DT_NULL`. Fails if a relocation points into the
moved entries. Warns if the tag is absent.

### ElfDisableProgramHeader

```yaml
  - ElfDisableProgramHeader: {Type: PT_GNU_RELRO}
```

Sets the type of the `PT_GNU_RELRO` program header (the only one supported) to
`PT_NULL`. It must be inside a writable `PT_LOAD`. Fails if there is none.

### StubFunction

```yaml
  - StubFunction: {Sym: _ZN18HomePageController22maybeShowKoboPlusPromoEv, Return: void}
  - StubFunction: {Sym: _ZN3Foo9isEnabledEv, Return: 0}           # Foo::isEnabled() returns 0
```

Overwrites the start of the function with an immediate return. `Return` is
`void` or a number 0–255 (the value returned in `r0`).

| Mode | void | value n |
|---|---|---|
| Thumb | `70 47 00 BF` (`bx lr; nop`) | `nn 20 70 47` (`movs r0, #n; bx lr`) |
| ARM | `1E FF 2F E1` (`bx lr`) | `nn 00 A0 E3 1E FF 2F E1` (`mov r0, #n; bx lr`) |

The symbol must be a function defined in the file, large enough for the stub,
in an executable segment. Applying the same stub twice is refused ("already
stubbed").

### NopCall

```yaml
  - NopCall: {Sym: _ZN11ReadingView8nextPageEv, Rel: 0xc, Target: _ZN10SketchNote16disableWithTimerEi}
```

Replaces the 4-byte Thumb `BL`/`BLX` at `Sym` + `Rel` with two `nop`s
(`00 BF 00 BF`), after decoding it and checking that it calls `Target`: the
target's definition in this file, its PLT entry or its PLT tail stub. `Rel`
must be even and inside the function.

### Tombstone

```yaml
Do not load this plugin:
  - Enabled: no
  - Tombstone: usr/local/Kobo/libboggle.so
```

Replaces the file with an empty (0-byte) one; Kobo's own firmware ships empty
files this way to disable plugins. The path must be the target's path, as a
check against using the patch file for the wrong target. It must be the only
instruction of its patch.

## Deprecated instructions

Kept so that old patch files keep working. Don't use them in new patches.

| Instruction | Replacement | Notes |
|---|---|---|
| `FindBaseAddressSymbol: "ReadingView::nextPage()"` | `BaseAddress: {Sym: ...}` | Matches the **demangled** name and sets the base address to the raw symbol value, i.e. **one more** than the function's address for Thumb functions. Patches using it account for that. |
| `ReplaceBytesAtSymbol: {Symbol, Offset, Find/FindH, Replace/ReplaceH, FindBLX}` | `ReplaceBytes` with `Base` | `FindBaseAddressSymbol` followed by `ReplaceBytes`. |
| `ReplaceBytesNOP: {Offset, Find/FindH/FindBLX}` | `ReplaceBytes` with `ReplaceInstNOP` | Replaces with `00 46` pairs. `Offset` is relative to the base address. |
| `ReplaceBLX: {Offset, Find, Replace}` | `ReplaceBytes` with `FindInstBLX`/`ReplaceInstBLX` | Find and Replace are the old and new target offsets. |

## Values: numbers, strings, bytes

- **Numbers:** decimal, `0x` hex, `0o` or leading-`0` octal (`010` is 8), `0b`
  binary; `_` may separate digits. Offsets are 32-bit signed; `ReplaceInt` and
  byte lists take 0–255.
- **Strings:** plain, `'single'` or `"double"` quoted (double quotes allow
  escapes such as `\n`, `\t`, `\x00`, `é`), or block scalars (`|`, `>`).
  Text is encoded as UTF-8. A quoted number is a string.
- **Booleans:** `yes`/`no`, `true`/`false`, `on`/`off` and their capitalisations.
- **Bytes:** a list of numbers (`[0x00, 0x46]`), or hex text in the `…H` fields.

## The patch32lsb format

Files ending in `.patch` use the line-based format of the original patch32lsb
tool.

```
# a comment
<Patch>
patch_name = `Change dicthtml strings to micthtml`
patch_enable = `no`
patch_group = `Dictionary`
replace_string = C9717C, `%1/dicthtml%2`, `%1/micthtml%2`
replace_bytes = 659DA4, BEF5AEE9, 00460046
</Patch>
```

- Each patch is enclosed in `<Patch>` … `</Patch>` (any case). Lines starting
  with `#` and empty lines are comments.
- Every other line is `key = value`, with at least one space on each side of
  `=`.
- Strings are in backticks and may use the escapes `\n \r \t \v \" \' \` \0 \\
  \xHH`.
- Offsets and bytes are hex without `0x`. Negative offsets are written as 32-bit
  two's complement (`FFFFFFF0` = −16).

| Key | Value | Meaning |
|---|---|---|
| `patch_name` | `` `name` `` | Required, unique. |
| `patch_enable` | `` `yes` `` or `` `no` `` | Required, exactly once, after `patch_name`. |
| `patch_group` | `` `group` `` | Optional, at most once. |
| `base_address` | `OFFSET` | Like `BaseAddress`. |
| `find_base_address` | `` `text` `` | Like `FindBaseAddressString`. |
| `replace_bytes` | `OFFSET, FIND, REPLACE` (hex) | Like `ReplaceBytes`. |
| `replace_string` | ``OFFSET, `find`, `replace` `` | Like `ReplaceString`. |
| `find_replace_string` | `` `find`, `replace` `` | Like `FindReplaceString`. |
| `replace_int` | `OFFSET, FIND, REPLACE` (decimal values) | Like `ReplaceInt`. |
| `replace_float` | `OFFSET, FIND, REPLACE` (decimal values) | Like `ReplaceFloat`. |
| `find_zlib` | `` `text` `` | Like `FindZlib`. |
| `find_zlib_hash` | `` `sha1` `` | Like `FindZlibHash`. |
| `replace_zlib` | ``OFFSET, `find`, `replace` `` | Like `ReplaceZlib`. |

Patches are applied in the order they appear in the file.
