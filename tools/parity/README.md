# Parity tests against Go kobopatch

These tools compare kobopatcher with the Go kobopatch it reimplements. The
results are in [docs/parity.md](../../docs/parity.md).

kobopatcher fixes some kobopatch bugs (README, "Differences from kobopatch").
Setting `KOBOPATCHER_GO_COMPAT=1` (or `kobopatcher.compat.set_go_compat(True)`)
switches them back to kobopatch's behaviour. In that mode the output must be
identical to Go; in the default mode every difference must come from one of
the listed fixes.

## What you need

- Go (1.23+), and a kobopatch checkout or release to compare with.
- [kobopatch-patches](https://github.com/pgaskin/kobopatch-patches) and
  [kobopatch-testdata](https://github.com/pgaskin/kobopatch-testdata) (about
  1.3 GB), cloned anywhere.
- Optionally firmware update zips.

Build the reference binaries:

```bash
git clone https://github.com/pgaskin/kobopatch && cd kobopatch
go build -o /tmp/goref/kobopatch ./kobopatch
go build -o /tmp/goref/kobopatch-apply ./tools/kobopatch-apply
go build -o /tmp/goref/symdump ./tools/symdump
go build -o /tmp/goref/cssextract ./tools/cssextract
cd -
cp -r tools/parity/goref /tmp/goref-src && cd /tmp/goref-src
go mod edit -replace github.com/pgaskin/kobopatch=$OLDPWD/../kobopatch   # or use the release in go.mod
go build -o /tmp/goref/goref . && cd -
```

## The tests

| Tool | Compares |
|---|---|
| `harness.py` | Every patch of every patch file, applied on its own, plus all compatible patches together and the file as it is: patched bytes (SHA256) or the exact error text. Uses `goref` (a batch applier) on the Go side and runs in-process on the Python side. |
| `cli_diff.py sets` | The whole `kobopatch` command per patch file: exit status, output lines, every member of `KoboRoot.tgz` (except the mtime). |
| `cli_diff.py tests` | `-t` results per version. |
| `cli_diff.py errors` | Exit status and messages for a catalogue of broken configurations and patch files. |
| `cli_diff.py tools` | `symdump` and `cssextract` output for every patch target (nickel, libnickel, libadobe, librmsdk) in the testdata tarballs and firmware zips. |
| `demangle_diff.py` | The C++ demangler against `ianlancetaylor/demangle`. |
| `armasm_diff.py` | The ARM decoder against `rsc.io/arm/armasm`. |
| `css_diff.py` | `IsCSS` and the CSS tokenizer against `riking/cssparse`. |
| `elf_diff.py` | The ELF instructions and `CheckELF` against kobopatch's `patchlib/elf.go`. |
| `p32_diff.py` | The patch32lsb format. |

Example:

```bash
P=kobopatch-patches T=kobopatch-testdata
python tools/parity/harness.py jobs --patches $P --testdata $T -o jobs.jsonl
python tools/parity/harness.py go /tmp/goref/goref jobs.jsonl go.jsonl
python tools/parity/harness.py py jobs.jsonl py-compat.jsonl --go-compat
python tools/parity/harness.py compare go.jsonl py-compat.jsonl     # must print 0 different
python tools/parity/harness.py py jobs.jsonl py.jsonl
python tools/parity/harness.py compare go.jsonl py.jsonl            # differences = fixes
python tools/parity/cli_diff.py sets --go /tmp/goref/kobopatch --patches $P --testdata $T --go-compat
python tools/parity/cli_diff.py tests --go /tmp/goref/kobopatch --patches $P --testdata $T --go-compat
python tools/parity/cli_diff.py errors --go /tmp/goref/kobopatch --testdata $T --go-compat
python tools/parity/cli_diff.py tools --go-symdump /tmp/goref/symdump --go-cssextract /tmp/goref/cssextract --testdata $T
```

The Go helpers for the component tests are printed or written by the
respective scripts (see `--help`).
