# Parity with Go kobopatch

kobopatcher was checked against the Go kobopatch it reimplements before the Go
code was retired. This page records what was compared and the results
(November 2026). The tools are in [tools/parity](../tools/parity), and how to
rerun them is described there.

**Reference:** kobopatch 0.16.0 plus the ELF instructions (the Go fork at
commit 7102644), built with Go 1.27.

**Two modes.** kobopatcher fixes some kobopatch bugs (README, "Differences from
kobopatch"). With `KOBOPATCHER_GO_COMPAT=1` it switches them back to
kobopatch's behaviour.
- **go_compat mode** must be identical to Go.
- **Default mode** may only differ where one of the fixes applies.

**Corpus:**
- every patch set of [kobopatch-patches](https://github.com/pgaskin/kobopatch-patches)
  (50 firmware versions, 4.9.11311 to 4.39.22801);
- the binaries of those versions from [kobopatch-testdata](https://github.com/pgaskin/kobopatch-testdata)
  (63 archives);
- five Kobo firmware update zips (4.7.10413, 4.30.18838, 4.34.20097, 4.38.23429,
  4.38.23852);
- kobopatcher's own 4.38.23429 patch set.

## Results

| Test | Compared | go_compat mode | Default mode |
|---|---|---|---|
| **Every patch on its own** (`harness.py`): patched bytes (SHA256) or the exact error text | 4,774 patches of 665 patch files in 51 sets (50 upstream versions and the 4.38.23429 set) | 4,774 identical | 4,774 identical |
| **All compatible patches together** (one per PatchGroup), per patch file | 665 | 665 identical | 665 identical |
| **Each patch file with its own Enabled states** | 665 | 665 identical | 665 identical |
| **The whole command** (`cli_diff.py sets`): exit status, output and every member of `KoboRoot.tgz` (except the mtime) | 635 patch files | 635 identical | 635 identical |
| **Test mode `-t`** (`cli_diff.py tests`): ✔/✕ per patch and the error list | 50 versions | 50 identical | 50 identical |
| **Error catalogue** (`cli_diff.py errors`): exit status and message for broken configs and patch files | 46 cases | 45 identical | 41 identical |
| **`symdump`, `cssextract`** (`cli_diff.py tools`): output file | 544 outputs (go_compat), 512 (default) | 544 identical | 448 identical |
| **The 4.38.23429 set**, all 33 patches, `--check-elf`, on `kobo-update-4.38.23429.zip` | 30 tgz members | identical | identical |

In total, the 6,104 patched outputs differ in 4,969 distinct ways, so the
comparison covers real changes, not just untouched files.

**Go's random order.** kobopatch prints the overrides of a patch file, and the
list of formats in one error message, in Go's random map order. The CLI
comparison treats those lines as a set.

### The differences, explained

- **Error catalogue, both modes:** `yaml: line 1: did not find expected node
  content` vs `yaml: line 2: expected the node content, but found '<stream
  end>'`. YAML syntax errors are worded by PyYAML instead of Go's yaml.v3
  (difference 10). The tab error is reworded to match.
- **Error catalogue, default mode:** 4 messages print a flexible offset as
  `{Sym: "_ZN3Foo3barEv"}` instead of Go's
  `kobopatch.BaseAddress{Offset:(*int32)(nil), Sym:(*string)(0xc000…), …}`
  (difference 10).
- **Tools, default mode:** all 64 differences are `symdump` of the `nickel`
  executable.
  - nickel's text segment is loaded at 0x10000 above its file offset, so
    kobopatch's PLT decoder, which uses file offsets, computed GOT slots that
    never match the relocations. kobopatch reports no PLT entry (`OffsetPLT: 0`)
    for any symbol of nickel.
  - kobopatcher's default mode works with virtual addresses (difference 5) and
    finds all of them.
  - In libraries, where addresses and offsets are equal, the outputs are
    identical.

None of the published patches depends on a fixed bug: the default-mode
results for all of them are identical to Go.

## Component tests

Each ported library was compared with its Go original on large inputs:

| Component | Go original | Inputs | Mismatches |
|---|---|---|---|
| C++/Rust demangler | ianlancetaylor/demangle | 1,558,578 names (every dynamic symbol of 223 firmware ELF files, the upstream test inputs, fuzzed names) | 0 |
| ARM decoder | rsc.io/arm/armasm | about 15 million words (2 million random, every `.plt` word of 1,153 ELF files, every table entry) | 0 |
| CSS detection and tokenizer | riking/cssparse + kobopatch `IsCSS` | about 1.2 million inputs (all firmware CSS streams, mutations, generated snippets), comparing token streams | 0 |
| ELF instructions and CheckELF | kobopatch `patchlib/elf.go` | 198 runs, 628 operations, on the 22 libraries of 4 firmware versions | 0 |
| patch32lsb format | kobopatch `patchfile/patch32lsb` | 7,004 generated patch files (go_compat mode) | 0 |
| `fmt.Sscanf` subset (patch32lsb) | Go `fmt` | 700,000 inputs | 0 |
| zlib compressor (fallback for zlib-ng) | classic zlib 1.3.1 | 1,679 inputs, 92.5 MB, including every stream of 5 nickel binaries | 0 |

In default mode, every patch32lsb difference (387 of 4,002 generated files) is
attributed to one of the listed fixes.

## Unit tests

kobopatch's Go tests are ported to pytest, with their vectors and expected
hashes:
- `TestZlib` and `TestReal` on the upstream test binaries;
- the 22-library lazy-binding oracle;
- the YAML instruction tests;
- the patch32lsb escape tests;
- the assembler vectors.

Together with tests for every fix in both modes, the CLI end to end (including a
real `lrelease`) and Go-generated fixtures, the suite has about 1,140 tests. CI
runs it on Linux, macOS and Windows with Python 3.10 to 3.14.
