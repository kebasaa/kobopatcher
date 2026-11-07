# kobopatcher

kobopatcher is a command-line tool that patches the firmware of Kobo eReaders.
It takes a firmware update from Kobo, changes some of the programs inside it
according to *patch files*, and writes a `KoboRoot.tgz`. When you copy that file
to your Kobo, the device installs the patched programs on its next restart.

Patches change how the device behaves: for example the font sizes and line
spacing on offer, what the home screen shows, how the reader handles margins,
or which features load at all.

kobopatcher is an independent Python reimplementation of
[kobopatch](https://github.com/pgaskin/kobopatch) by Patrick Gaskin. It reads the
same configuration files and patch files, so the patch sets published for
kobopatch (such as [kobopatch-patches](https://github.com/pgaskin/kobopatch-patches))
work with it unchanged. It also adds instructions that understand the structure
of the programs they patch (see [Structure-aware ELF instructions](#structure-aware-elf-instructions)).
It runs in a terminal on Linux, macOS and Windows.

> **Warning.** kobopatcher and its patches are unofficial and not supported by
> Kobo. A patch is only as good as its author's testing: patches marked as
> untested have not been run on a device. Read what a patch does before you
> enable it. You can always undo patches by reinstalling the unmodified firmware
> (see [Undoing patches](#undoing-patches)), but you use them at your own risk.

## Contents

- [How it works](#how-it-works)
- [Installing kobopatcher](#installing-kobopatcher)
- [Patching your Kobo, step by step](#patching-your-kobo-step-by-step)
- [Installing the patched firmware on the device](#installing-the-patched-firmware-on-the-device)
- [Undoing patches](#undoing-patches)
- [Command-line reference](#command-line-reference)
- [kobopatch.yaml reference](#kobopatchyaml-reference)
- [Patch files](#patch-files)
- [Testing patches](#testing-patches)
- [Translations, extra files and symlinks](#translations-extra-files-and-symlinks)
- [The 4.38.23429 patch set in this repository](#the-43823429-patch-set-in-this-repository)
- [FAQ](#faq)
- [Troubleshooting](#troubleshooting)
- [Differences from kobopatch](#differences-from-kobopatch)
- [Development](#development)
- [Credits and license](#credits-and-license)

## How it works

1. A Kobo firmware update (`kobo-update-<version>.zip`) contains `KoboRoot.tgz`,
   an archive of the files that are installed on the device. The interesting
   ones are the reading app `nickel`, its library `libnickel.so.1.0.0`, and the
   libraries for PDFs and Adobe DRM (`librmsdk.so.1.0.0`, `libadobe.so`).
2. A *patch file* lists named patches for one of those files. Each patch is a
   list of small, checked edits: "at this place, these bytes must be found;
   replace them with those". A patch that doesn't match the file exactly is
   refused instead of being applied blindly, so a patch for one firmware version
   can't damage another.
3. `kobopatch.yaml` says which firmware to read, which patch files apply to
   which file, and which patches are switched on.
4. kobopatcher applies the enabled patches and writes a new `KoboRoot.tgz` that
   contains only the patched files (with their original permissions and owner).
5. A Kobo installs any `KoboRoot.tgz` it finds in its `.kobo` folder when it
   restarts.

Patches are tied to one firmware version, because the bytes they look for move
between versions. Your device must run **exactly** the firmware version the
patches were written for.

## Installing kobopatcher

kobopatcher needs Python 3.10 or newer. The recommended way to install it is
[pipx](https://pipx.pypa.io/), which puts the commands on your PATH without
touching your system Python:

```bash
pipx install git+https://github.com/kebasaa/kobopatcher
```

Or with pip, into a virtual environment:

```bash
python3 -m venv ~/kobopatcher-venv
~/kobopatcher-venv/bin/pip install git+https://github.com/kebasaa/kobopatcher
```

This installs four commands: `kobopatcher`, `kobopatcher-apply`,
`kobopatcher-cssextract` and `kobopatcher-symdump`. Check that it works:

```bash
kobopatcher --version
```

When a release is published on the
[releases page](https://github.com/kebasaa/kobopatcher/releases), it also has
standalone executables for Linux, macOS and Windows that don't need Python.
Download the one for your system, make it executable (`chmod +x` on Linux and
macOS), and use it in place of `kobopatcher` in the commands below.

**About CSS patches (`FindZlib`, `ReplaceZlib`):** they recompress data inside
`nickel`, and the result has to match classic zlib byte for byte. kobopatcher
uses Python's zlib module when it is classic zlib (most Python builds) and
otherwise its own pure-Python copy of zlib's compressor (for example on
Windows with Python 3.14, which uses zlib-ng). The copy gives the same bytes,
but CSS patches then take a few seconds longer.

## Patching your Kobo, step by step

### 1. Find out which firmware your Kobo runs

On the Kobo: **Settings → Device information → Software version**. It looks
like `4.38.23429`. Write it down.

### 2. Get a patch set for that version

A patch set is a folder with a `kobopatch.yaml` and a `src/` folder of patch
files. Use one of these:

- **The community patches:** download the zip for your version from the
  [kobopatch-patches releases](https://github.com/pgaskin/kobopatch-patches/releases)
  and unzip it. It contains `kobopatch.yaml` and `src/`. (It also contains
  scripts and binaries of the original kobopatch, which you don't need.)
- **A set from this repository**, e.g. [`src/versions/4.38.23429`](src/versions/4.38.23429)
  (see [below](#the-43823429-patch-set-in-this-repository)).
- **Your own**, written as described in [Patch files](#patch-files).

If there is no patch set for your exact version, don't use one for a nearby
version: its patches will fail to apply (or, for some instructions, could apply
in the wrong place).

### 3. Get the matching firmware update

You need the firmware update zip of **the same version**, for your model, e.g.
`kobo-update-4.38.23429.zip`. Kobo's own download links for every version and
model are collected on
[pgaskin.net/KoboStuff/kobofirmware.html](https://pgaskin.net/KoboStuff/kobofirmware.html).

Put the zip where `kobopatch.yaml` expects it. Look at its `in:` line:

```yaml
in: src/kobo-update-4.38.23429.zip
```

means the zip goes into the `src/` folder of the patch set. (You can also leave
it anywhere and pass it with `-f`, see step 5.)

### 4. Choose your patches

Open the patch files in `src/` with a text editor. Each patch starts with its
name and has a `Description` saying what it does:

```yaml
My 10 line spacing values:
  - Enabled: no
  - Description: |
      Replaces the line spacing values with ...
  - ...
```

Change `Enabled: no` to `Enabled: yes` for each patch you want. Some patches
have a `PatchGroup`: only one patch of a group can be enabled at a time.

Instead of editing the patch files, you can list your choices in the
`overrides:` section of `kobopatch.yaml`. That keeps them in one place and
survives replacing the patch files with a newer copy:

```yaml
overrides:
  src/libnickel.so.1.0.0.yaml:
    My 10 line spacing values: yes
    Custom font sizes: yes
  src/nickel.yaml:
    Remove beta features: yes
```

The keys must be written exactly as in the `patches:` section, and the patch
names exactly as in the patch file.

### 5. Run kobopatcher

Open a terminal in the patch set's folder (the one with `kobopatch.yaml`) and
run:

```bash
kobopatcher
```

or, from anywhere:

```bash
kobopatcher /path/to/patchset/kobopatch.yaml
```

To use a firmware zip that isn't where `in:` says:

```bash
kobopatcher -f ~/Downloads/kobo-update-4.38.23429.zip
```

kobopatcher shows each file it patches and each patch it applies (`APPLY`) or
skips (`SKIP`). If a patch can't be applied, it stops with an error saying which
patch and which instruction failed, and writes no output. Details of every step
go to the log file (`log:` in `kobopatch.yaml`, usually `out/log.txt`).

At the end it says:

```
Successfully saved patched KoboRoot.tgz to out/KoboRoot.tgz.
```

Optionally, add `--check-elf` to have kobopatcher re-check every patched
program file for structural damage before it writes the output:

```bash
kobopatcher --check-elf
```

## Installing the patched firmware on the device

1. **Check the version again.** The Kobo must still run the version you patched
   (Settings → Device information). If it updated itself in the meantime, get
   the patches and firmware for the new version and patch again.
2. **Connect the Kobo to your computer** with a USB cable and tap **Connect** on
   the device. It appears as a USB drive named `KOBOeReader`.
3. **Show hidden folders**, because the folder you need starts with a dot:
   - Windows Explorer: **View → Show → Hidden items**.
   - macOS Finder: press **Cmd + Shift + .** (dot).
   - Linux file managers: usually **Ctrl + H**.
4. **Copy `out/KoboRoot.tgz` into the `.kobo` folder** at the top of the
   `KOBOeReader` drive, so that it is at `KOBOeReader/.kobo/KoboRoot.tgz`. Don't
   rename it, don't unpack it, and don't put it anywhere else.

   From a terminal, for example:

   ```bash
   cp out/KoboRoot.tgz /media/$USER/KOBOeReader/.kobo/        # Linux
   cp out/KoboRoot.tgz /Volumes/KOBOeReader/.kobo/            # macOS
   ```

   ```powershell
   Copy-Item out\KoboRoot.tgz E:\.kobo\                       # Windows (use the Kobo's drive letter)
   ```

5. **Eject the Kobo safely** (Windows: "Safely Remove Hardware"; macOS: Eject in
   Finder; Linux: unmount/eject in the file manager or `udisksctl unmount`), then
   unplug the cable.
6. The Kobo notices the file and restarts. While it starts, it checks that the
   archive is intact, unpacks it over its system files, deletes it, and restarts
   once more. This takes a minute or two; don't switch it off meanwhile. (A
   damaged archive fails the check and is deleted without being installed.)
7. Check that the patches work (for example the new font sizes in the reading
   settings).

Every firmware update from Kobo replaces the patched files with unpatched ones.
After an update, patch the new version and install again.

## Undoing patches

To remove all patches, install the unmodified files of the same firmware
version:

- **Easiest:** take `KoboRoot.tgz` out of the original `kobo-update-<version>.zip`
  (open the zip; `KoboRoot.tgz` is at its top level) and install it exactly like
  a patched one (copy it to `.kobo`, eject, wait for the restart).
- **Full firmware reinstall:** unpack the whole `kobo-update-<version>.zip` into
  the `.kobo` folder (all files, including `KoboRoot.tgz`, `upgrade/` and the
  rest), eject, and let the device install it.

To remove only some patches, disable them, run kobopatcher again and install the
new `KoboRoot.tgz`. It replaces all files the patch set touches, so a disabled
patch is reverted too, as long as its file is still patched by the set. (A file
that no longer appears in `patches:` at all keeps its previous, patched copy;
reinstall the stock `KoboRoot.tgz` in that case.)

## Command-line reference

### kobopatcher

```
kobopatcher [OPTIONS] [CONFIG_FILE]
```

| Parameter | What it does |
|---|---|
| `CONFIG_FILE` | The configuration to use. Default: `kobopatch.yaml` in the current directory. All paths inside it (`in`, `out`, `log`, patch files, extra files) are relative to the folder the config file is in. `-` reads the configuration from standard input (paths are then relative to the current directory). |
| `-f FILE`, `--firmware FILE` | Use this firmware instead of the one in `in:`. Either a firmware update zip, or a `.tar.xz` from [kobopatch-testdata](https://github.com/pgaskin/kobopatch-testdata) (a flat archive of the binaries, used for testing patches without the full firmware). |
| `-t`, `--run-tests` | Don't build a `KoboRoot.tgz`. Instead apply every patch of every patch file **on its own** to a fresh copy of its target and report which ones apply (✔) and which fail (✕), with the errors at the end. Useful to check a patch set against a firmware version. Only works with YAML patch files. Exits with status 1 if any patch fails. |
| `--check-elf` | After patching each program or library, parse it again and check that it is still well-formed: same size and headers, intact dynamic section and symbol tables, and consistent binding (eager binding and RELRO removed together, never one without the other), and that every stubbed function and removed call is still in place. Prints `CHECK-ELF ok` per file. Recommended when you use the ELF instructions. |
| `--version` | Print the version and exit. |
| `-h`, `--help` | Show the help text and exit. |

Exit status: 0 on success, 1 on an error (the message starts with `Error:`),
2 for invalid command-line options.

### kobopatcher-apply

Applies one patch file to one file, without a `kobopatch.yaml` or firmware zip.
Handy while writing patches. The patches' own `Enabled` states are used.

```
kobopatcher-apply -i INPUT -p PATCHFILE -o OUTPUT [-f FORMAT] [-v]
```

| Parameter | What it does |
|---|---|
| `-i FILE`, `--input FILE` | The file to patch (required). |
| `-p FILE`, `--patch-file FILE` | The patch file (required). |
| `-o FILE`, `--output FILE` | Where to write the patched file; overwritten if it exists (required). |
| `-f NAME`, `--patch-format NAME` | `kobopatch` (YAML) or `patch32lsb` (the old format). Default: from the patch file's extension (`.yaml`/`.yml` or `.patch`). |
| `-v`, `--verbose` | Print every step of every instruction. |
| `-h`, `--help` | Show the help text. |

### kobopatcher-cssextract

```
kobopatcher-cssextract BINARY_FILE
```

Writes every zlib-compressed CSS stylesheet in the binary (for example `nickel`)
to `cssextract.out.css` in the current directory. Each stylesheet is preceded by
a comment with its offset, length and SHA1. Use it to find text for `FindZlib`
and hashes for `FindZlibHash`.

### kobopatcher-symdump

```
kobopatcher-symdump BINARY_FILE
```

Writes the dynamic symbols of an ARM program or library to `symdump.out.json`
in the current directory: name, address, index, type, GOT slot, PLT entry, PLT
tail stub and the demangled C++ name. Use it to find names for `BaseAddress:
{Sym: ...}`, `SymPLT`, `StubFunction` and `NopCall`.

## kobopatch.yaml reference

```yaml
version: 4.38.23429                  # required
in: src/kobo-update-4.38.23429.zip   # required
out: out/KoboRoot.tgz                # required
log: out/log.txt                     # required
patches:
  src/libnickel.so.1.0.0.yaml: usr/local/Kobo/libnickel.so.1.0.0
  src/nickel.yaml: usr/local/Kobo/nickel
  src/nickel.custom.yaml: usr/local/Kobo/nickel
overrides:
  src/nickel.yaml:
    Some patch: yes
    Another patch: no
lrelease: /usr/lib/qt6/bin/lrelease
translations:
  src/nickel-de.ts: usr/local/Kobo/translations/nickel-de.qm
files:
  src/hyph_de.dic: usr/share/hyphen/hyph_de.dic
  src/fonts/MyFont.ttf:
    - usr/local/Kobo/fonts/MyFont.ttf
symlinks:
  usr/share/hyphen/hyph_de.dic: usr/share/hyphen/hyph_de_DE.dic
```

| Key | Meaning |
|---|---|
| `version` | The firmware version the patches are for. Required; informational (it isn't compared with the firmware). |
| `in` | The firmware update zip (or a kobopatch-testdata `.tar.xz`). Required. `-f` overrides it. |
| `out` | Where to write `KoboRoot.tgz`. Required. The folder must exist. |
| `log` | Where to write the detailed log. Required. |
| `patchFormat` | Deprecated and optional. The format of each patch file comes from its extension: `.yaml`/`.yml` is the YAML format, `.patch` the old patch32lsb format. If given, it must be `kobopatch` or `patch32lsb`. |
| `patches` | Patch file → the path of the file it patches inside `KoboRoot.tgz` (without a leading `./` or `/`). Several patch files can patch the same file; they are applied in the order listed. A target that isn't in the firmware is an error. |
| `overrides` | Patch file (spelled exactly as in `patches`) → patch name → `yes`/`no`. Overrides the `Enabled` state of those patches. A name that doesn't exist is an error. |
| `lrelease` | Path to Qt's `lrelease`, used for `translations`. Default: found on the PATH (also as `lrelease-qt6`/`lrelease-qt5` and in the usual Qt install folders). |
| `translations` | Qt translation source (`.ts`) → path of the compiled `.qm` inside `KoboRoot.tgz`, which must start with `usr/local/Kobo/translations/`. |
| `files` | Local file → path (or list of paths) inside `KoboRoot.tgz` where it is installed. Paths must not start with `/`. Files are installed with mode 0777, owned by root. |
| `symlinks` | Target → symlink path, both inside `KoboRoot.tgz`. The target must be one of the `files` destinations; the link points to `/<target>`. |

Unknown keys and duplicate keys are errors, which catches typos.

## Patch files

### The YAML format

A patch file maps patch names to lists of instructions. Each list item has
exactly one instruction (an illustrative example; the symbol and bytes are made up):

```yaml
Increase the maximum font size:
  - Enabled: no
  - PatchGroup: Font size
  - Description: Allows font sizes up to 150.
  - BaseAddress: {Sym: "_ZN19ReadingSettingsView13fontSizeSliderEv", Rel: 0x20}
  - ReplaceBytes: {Offset: 4, FindH: 4F F0 5A 01, ReplaceH: 4F F0 96 01}
```

- Patches are applied in alphabetical order of their names. Before each patch,
  the *base address* (the position that offsets count from) is reset to 0.
- `Enabled` (`yes`/`no`), `Description` and `PatchGroup` describe the patch;
  everything else changes the file. A patch must contain at least one
  instruction that changes something.
- Only one patch per `PatchGroup` may be enabled.
- Numbers can be decimal (`16`), hex (`0x10`), octal (`0o20` or `020`) or binary
  (`0b10000`). Quoted values are always text.
- Indent with spaces, never tabs.

### Instructions

The full reference with every field and an example for each is in
[docs/reference.md](docs/reference.md). In short:

| Instruction | What it does |
|---|---|
| `BaseAddress` | Set the base address: a number, a symbol name, or `{Offset \| Sym \| SymPLT \| SymPLTTail, Rel}`. |
| `FindBaseAddressString` / `FindBaseAddressHex` | Set the base address to the first occurrence of a string / of hex bytes. |
| `ReplaceBytes` | Replace bytes at base+`Offset`; bytes as lists (`Find: [0x4f, 0xf0]`), hex (`FindH`/`ReplaceH`) or generated Thumb branches (`FindInstBLX`, `ReplaceInstBLX`, `FindInstBW`, `ReplaceInstBW`, `ReplaceInstNOP`); `Base` sets the base address for this instruction only; `CheckOnly` only checks. |
| `ReplaceString` | Replace a string at or after base+`Offset` (shorter replacements are padded). |
| `FindReplaceString` | Find a string and replace it. |
| `ReplaceInt` / `ReplaceFloat` | Replace a byte value / an 8-byte double at base+`Offset`. |
| `FindZlib` / `FindZlibHash` | Set the base address to the compressed CSS stylesheet containing some text / with a SHA1. |
| `ReplaceZlib` / `ReplaceZlibGroup` | Change text inside that stylesheet (recompressed; must still fit). |
| `ElfLazyBinding` | Make a library resolve its imported functions on first use instead of all at start (as firmware did until 4.30). |
| `ElfClearDynFlag`, `ElfRemoveDynEntry`, `ElfDisableProgramHeader` | The individual steps of `ElfLazyBinding`. |
| `StubFunction` | Make a function return immediately (optionally with a value). |
| `NopCall` | Remove one call instruction, after checking it calls the expected function. |
| `Tombstone` | Replace the file with an empty one (how Kobo itself disables plugins). |
| `FindBaseAddressSymbol`, `ReplaceBytesAtSymbol`, `ReplaceBytesNOP`, `ReplaceBLX` | Deprecated forms kept for old patch files. |

### Structure-aware ELF instructions

The usual instructions edit bytes at offsets the patch author found by hand.
The ELF instructions work on the structure of the program or library instead:
kobopatcher finds the right place itself (in the dynamic section, the program
headers or the symbol table), so the same patch works on many firmware
versions. Each instruction checks its preconditions and refuses unsafe cases
(see [docs/reference.md](docs/reference.md#elf-instructions)). For example:

```yaml
Lazy binding like firmware 4.7:
  - Enabled: no
  - ElfLazyBinding: true

Disable Kobo Plus promo check on Home:
  - Enabled: no
  - StubFunction: {Sym: _ZN18HomePageController22maybeShowKoboPlusPromoEv, Return: void}
```

### The old patch32lsb format

Files ending in `.patch` use the format of the original `patch32lsb` tool:

```
<Patch>
patch_name = `Increase the maximum font size`
patch_enable = `no`
replace_bytes = 659DA4, BEF5AEE9, 00460046
</Patch>
```

It is supported for old patch sets; see [docs/reference.md](docs/reference.md#the-patch32lsb-format).
New patches should use YAML.

## Testing patches

To check every patch of a set against a firmware version, without building a
`KoboRoot.tgz`:

```bash
kobopatcher -t
```

Each patch is applied on its own; the output lists ✔ or ✕ per patch and the
errors at the end. With a kobopatch-testdata archive you don't need the full
firmware:

```bash
kobopatcher -t -f kobopatch-testdata/4.38.23429.tar.xz
```

## Translations, extra files and symlinks

- **Translations:** put the `.ts` files somewhere in the patch set, list them in
  `translations`, and install Qt's linguist tools, which provide `lrelease`
  (e.g. `sudo apt install qt6-l10n-tools` on Debian/Ubuntu, `brew install qt` on
  macOS, or the Qt installer on Windows). kobopatcher compiles each `.ts` into a
  `.qm` and adds it to `KoboRoot.tgz`.
- **Extra files:** anything listed in `files` is added to `KoboRoot.tgz`, e.g.
  fonts or hyphenation dictionaries.
- **Symlinks:** links listed in `symlinks` are added too; their targets must be
  files added with `files`.

## The 4.38.23429 patch set in this repository

[`src/versions/4.38.23429`](src/versions/4.38.23429) contains patches for
firmware 4.38.23429 on the Kobo Aura H2O (N250). They switch off work this
device can't use: they restore lazy binding in 22 libraries, remove a sketch
overlay timer and a forced full refresh on page turns, skip a Kobo Plus promo
check, and stop 8 unused plugins from loading. **All of them are disabled by
default and untested on a device.** See its [README](src/versions/4.38.23429/README.md)
for what each patch does and the recommended order for trying them.

## FAQ

**Is it stable?**
kobopatcher was checked against kobopatch on the published patch sets for 50
firmware versions: with every patch applied on its own, and with all compatible
patches together, both produce byte-identical files (see
[docs/parity.md](docs/parity.md)). kobopatch's own tests are part of
kobopatcher's test suite. That says nothing about whether a particular patch is
a good idea on your device.

**What is YAML?**
A plain-text format for structured data (a superset of JSON). For patch files
you only need: `name:` starts a patch, `- Instruction: value` is a list item,
indentation is done with spaces.

**Can I use old patch sets made for kobopatch?**
Yes. kobopatcher reads the same `kobopatch.yaml`, YAML patch files and
`.patch` files. Run `kobopatcher` where you would have run `kobopatch.sh` or
`kobopatch.bat`. A few kobopatch bugs are fixed, which can change the result of
patches that relied on them; see [Differences from kobopatch](#differences-from-kobopatch).

**Which firmware version must be installed?**
Exactly the one the patches are for, the same as the firmware zip you patch.
Patches for another version either fail to apply or, worse, are applied to a
device whose other files don't match.

**How do I update kobopatcher?**
`pipx upgrade kobopatcher`, or `pipx install --force git+https://github.com/kebasaa/kobopatcher`.

**How do I undo or uninstall the patches?**
See [Undoing patches](#undoing-patches): install the stock `KoboRoot.tgz` of the
same version.

**Why does a replacement say the new string is too long or too short when it has the same number of characters?**
Lengths are counted in bytes of UTF-8, not characters. Letters outside plain
ASCII (é, ü, ß, curly quotes, ...) take two or more bytes.

**I changed one text and an unrelated text changed too. Why?**
Programs often store identical strings, or the end of one string, only once and
share it. Changing it changes every place that uses it. Choose a more specific
patch, or a different text.

**How do I add translations?**
See [Translations, extra files and symlinks](#translations-extra-files-and-symlinks).

**My Kobo updated itself and the patches are gone.**
A firmware update replaces the patched files. Patch the new version (when a patch
set exists for it) and install the new `KoboRoot.tgz`.

**Does kobopatcher change anything on my computer or on the device?**
On your computer it only writes the output and log files named in
`kobopatch.yaml`. It never connects to the device; you copy the result yourself.

## Troubleshooting

- **Something failed:** the message names the patch file, the patch and the
  instruction. The log file has every step. When reporting a problem, include
  the log file and the patch file.
- **`could not find specified bytes at offset`:** the patch doesn't match this
  firmware: wrong version, wrong model, or another enabled patch already changed
  those bytes.
- **`patch file contains tabs`:** indent with spaces.
- **`more than one patch enabled in PatchGroup`:** enable only one patch of that
  group.
- **`could not find target ... in the firmware`:** a `patches:` entry names a
  file that isn't in this firmware (typo, or a file of another model).
- **CSS patches are slow:** your Python's zlib module isn't classic zlib (e.g.
  zlib-ng on Windows with Python 3.14), so kobopatcher uses its pure-Python
  compressor. The result is the same. Python 3.13 or a standalone executable
  is faster.
- **`zlib self-test failed`:** neither Python's zlib module nor kobopatcher's own
  compressor produced classic zlib's output. Please report this with your
  Python version and operating system.
- **`could not find lrelease`:** install the Qt linguist tools, or set `lrelease:`
  in `kobopatch.yaml` to its path.

## Differences from kobopatch

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

The banner and help name kobopatcher instead of kobopatch.

## Development

```bash
git clone https://github.com/kebasaa/kobopatcher
cd kobopatcher
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

- Tests that need firmware files skip themselves when the files aren't there.
  `KOBOPATCHER_TEST_ROOTFS` points the ELF tests at an extracted 4.38.23429
  rootfs.
- [tools/parity](tools/parity) contains the differential tests against Go
  kobopatch, which produced [docs/parity.md](docs/parity.md); how to rerun them
  is described there.
- Standalone executables are built with PyInstaller by the `release` workflow
  (`.github/workflows/release.yml`).

## Credits and license

kobopatcher is based on [kobopatch](https://github.com/pgaskin/kobopatch) by
Patrick Gaskin, and on its patch format, patch sets and community at
[MobileRead](https://www.mobileread.com/forums/showthread.php?t=297338). It
includes Python ports of
[ianlancetaylor/demangle](https://github.com/ianlancetaylor/demangle) (BSD 3-clause),
[rsc.io/arm/armasm](https://github.com/rsc/arm) (BSD 3-clause) and
[riking/cssparse](https://github.com/riking/cssparse) (BSD 3-clause), and of
the compressor of [zlib](https://zlib.net) (zlib license); their licenses are
next to the ported code.

MIT License, see [LICENSE](LICENSE).
