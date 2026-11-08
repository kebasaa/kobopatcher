# kobopatcher usage reference

The [README](../README.md) is a tutorial for the guided mode. This page is the
reference: every command and option, the `kobopatch.yaml` format, working with a
patch-set folder by hand, and writing and testing patches. The patch instructions
themselves are described in [reference.md](reference.md).

- [Commands](#commands)
  - [kobopatcher (guided mode)](#kobopatcher-guided-mode)
  - [kobopatcher restore](#kobopatcher-restore)
  - [kobopatcher with a kobopatch.yaml](#kobopatcher-with-a-kobopatchyaml)
  - [kobopatcher device](#kobopatcher-device)
  - [kobopatcher-apply, -cssextract, -symdump](#kobopatcher-apply)
  - [Environment variables](#environment-variables)
- [Where things are kept](#where-things-are-kept)
- [Working with a patch-set folder by hand](#working-with-a-patch-set-folder-by-hand)
- [The menu](#the-menu)
- [kobopatch.yaml reference](#kobopatchyaml-reference)
- [Patch files](#patch-files)
- [Testing patches](#testing-patches)
- [Translations, extra files and symlinks](#translations-extra-files-and-symlinks)
- [Publishing patch sets](#publishing-patch-sets)

## Commands

### kobopatcher (guided mode)

```
kobopatcher [start] [--target VERSION] [--model ID] [--mount PATH] [--workdir DIR]
            [--patchsets URL|DIR] [-f FIRMWARE.zip] [--enable/--disable/--only PATCH]
            [--no-menu] [--check-elf] [--yes]
```

Without a `kobopatch.yaml` in the current folder, `kobopatcher` runs the guided
mode (`kobopatcher start` does the same anywhere):

1. It looks for a connected Kobo and reads its model and firmware version. With a
   Kobo, it first asks what to do: patch, reset to stock firmware, or change device
   settings.
2. It downloads the index of patch sets and finds the one for that firmware. If a
   set exists for a newer firmware, it says so, but patches the version the Kobo
   runs.
3. It downloads the patch set into the workspace (see
   [Where things are kept](#where-things-are-kept)). If the published set changed
   since, it offers to update it; your choice of patches is kept.
4. It downloads Kobo's firmware zip for that version and model from Kobo's server,
   after asking, and checks its SHA-256.
5. It opens the menu to choose patches and saves the choice.
6. It builds `KoboRoot.tgz`.
7. If the Kobo runs that version, it offers to copy the file to it.

| Option | What it does |
|---|---|
| `--target VERSION` | Patch this firmware version instead of the connected Kobo's, e.g. without a Kobo. The result is only offered for installing if the Kobo runs that version. |
| `--model ID` | The model, as the last three digits of the device id (370 for the Aura H2O), when no Kobo is connected. Picks the right firmware download. |
| `--mount PATH` | The Kobo's drive, if it isn't found by itself. |
| `--workdir DIR` | Where patch sets, firmware and output are kept. Default: `~/kobopatcher`. |
| `--patchsets URL\|DIR` | Where patch sets come from. Default: the `patchsets` release of this repository. A folder can be `src/versions` of a clone, or a folder written by `tools/publish_patchsets.py`. |
| `-f FILE`, `--firmware FILE` | Use this firmware zip instead of downloading it. |
| `--enable`, `--disable`, `--only PATCH` | Change the choice before the menu opens (ids from the menu or `--list`, `*` wildcards). |
| `--no-menu` | Don't open the menu; use the saved choice (and the options above). |
| `--check-elf` | Check every patched program file (see below). |
| `-y`, `--yes` | Answer every question with yes and skip the menu. |

### kobopatcher restore

```
kobopatcher restore [--target VERSION] [--model ID] [--mount PATH] [--workdir DIR]
                    [--patchsets URL|DIR] [-f FIRMWARE.zip] [--full] [--out DIR] [--yes]
```

Resets the Kobo to its stock firmware, undoing all patches. It uses Kobo's own,
unmodified firmware of the version the Kobo runs (from the workspace, or downloaded
after asking).

| Option | What it does |
|---|---|
| (none) | Copies the stock `KoboRoot.tgz` to `.kobo/` on the Kobo. It replaces every file a patch could have changed; books, settings and accounts stay. |
| `--full` | Unpacks the whole firmware update into `.kobo/`, as Kobo's own updater does. For a Kobo that still misbehaves after the reset above. |
| `--out DIR` | Writes the stock files into DIR instead of onto a Kobo (with `--target`). |
| `--yes` | Don't ask; also replaces a `KoboRoot.tgz` that is already waiting on the Kobo. |
| others | As for the guided mode. |

If kobopatcher changed the Kobo's settings (`device settings`), `restore` offers to
put back the settings from before the first change.

A `KoboRoot.tgz` can add and replace files but not delete them. Files that patches
added (for example `/etc/kobo.qss` of the pruned-stylesheet patch) stay on the Kobo;
stock firmware doesn't use them. `restore` lists them.

### kobopatcher with a kobopatch.yaml

```
kobopatcher [OPTIONS] [CONFIG_FILE]
```

In a folder with a `kobopatch.yaml` (or with `CONFIG_FILE`), kobopatcher works like
kobopatch: it patches the firmware named in the config with the enabled patches.

| Parameter | What it does |
|---|---|
| `CONFIG_FILE` | The configuration to use. Default: `kobopatch.yaml` in the current directory. All paths inside it (`in`, `out`, `log`, patch files, extra files) are relative to the folder the config file is in. `-` reads the configuration from standard input (paths are then relative to the current directory). |
| `-f FILE`, `--firmware FILE` | Use this firmware instead of the one in `in:`. Either a firmware update zip, or a `.tar.xz` from [kobopatch-testdata](https://github.com/pgaskin/kobopatch-testdata) (a flat archive of the binaries, used for testing patches without the full firmware). |
| `-t`, `--run-tests` | Don't build a `KoboRoot.tgz`. Instead apply every patch of every patch file **on its own** to a fresh copy of its target and report which ones apply (✔) and which fail (✕), with the errors at the end. Useful to check a patch set against a firmware version. Only works with YAML patch files. Exits with status 1 if any patch fails. |
| `--check-elf` | After patching each program or library, parse it again and check that it is still well-formed: same size and headers, intact dynamic section and symbol tables, and consistent binding (eager binding and RELRO removed together, never one without the other), and that every stubbed function and removed call is still in place. Prints `CHECK-ELF ok` per file. Recommended when you use the ELF instructions. |
| `--list` | List every patch of the set by category: `[x]` on, `[ ]` off, its id, name, the files it patches, its group and NEW/EXPERIMENTAL. Add `--json` for JSON. Doesn't patch. |
| `--select` | Choose patches in the menu (see [The menu](#the-menu)). **s** saves the choice to `kobopatch.yaml`; **p** saves and patches. |
| `--enable PATCH` | Switch a patch on for this run. `PATCH` is its id (from `--list`) or name; `*` and `?` are wildcards. Repeatable. Switching on a patch switches off the others in its group. |
| `--disable PATCH` | Switch a patch off for this run. Repeatable. |
| `--only PATCH` | Switch every patch off except these. Repeatable. |
| `--save` | Write the choice made with `--enable`/`--disable`/`--only` to the `overrides:` of `kobopatch.yaml` (with a backup, `kobopatch.yaml.bak`), so later runs use it too. |
| `--version` | Print the version and exit. |
| `-h`, `--help` | Show the help text and exit. |

Exit status: 0 on success, 1 on an error (the message starts with `Error:`),
2 for invalid command-line options.

```bash
kobopatcher --list                                  # what's in the set
kobopatcher --enable custom-font-sizes --save       # remember a choice
kobopatcher --only "lazy*" --check-elf              # a run with only those patches
```

### kobopatcher device

Works with a Kobo connected over USB (tap **Connect** on it first). kobopatcher
finds the `KOBOeReader` drive by itself; `--mount PATH` names it if it doesn't.
Everything that changes the Kobo shows what it will do and asks first.

```
kobopatcher device [--mount PATH] info [--config kobopatch.yaml]
kobopatcher device [--mount PATH] install [KoboRoot.tgz] [--config kobopatch.yaml] [--yes]
kobopatcher device [--mount PATH] settings [--list | --select | --enable ID | --disable ID | --set ID=VALUE | --undo] [--yes]
kobopatcher device [--mount PATH] db [--list | --run ACTION] [--backup-dir DIR] [--yes]
```

| Command | What it does |
|---|---|
| `info` | Shows the model and firmware version. With `--config`, checks that a patch set matches the firmware. |
| `install` | Copies `KoboRoot.tgz` (default `out/KoboRoot.tgz`) to `.kobo/` on the Kobo. With `--config`, refuses if the patch set is for another firmware version. Then eject the Kobo. |
| `settings` | Switches settings in `.kobo/Kobo/Kobo eReader.conf` (see the [README](../README.md#device-settings-and-database-maintenance)): `--list`, `--select` (menu; **a** writes), `--enable ID`, `--disable ID`, `--set ID=VALUE` for settings that need a value, `--undo` to restore the last backup. A backup is made before every change. |
| `db` | Database maintenance: `--list`, `--run ACTION`. The database is copied to `--backup-dir` (default `./kobo-backup-TIMESTAMP`) first. |
| `--yes` | Don't ask before changing something. |

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

**`kobopatcher-cssextract BINARY_FILE`** writes every zlib-compressed CSS
stylesheet in the binary (for example `nickel`) to `cssextract.out.css` in the
current directory. Each stylesheet is preceded by a comment with its offset,
length and SHA1. Use it to find text for `FindZlib` and hashes for `FindZlibHash`.

**`kobopatcher-symdump BINARY_FILE`** writes the dynamic symbols of an ARM program
or library to `symdump.out.json` in the current directory: name, address, index,
type, GOT slot, PLT entry, PLT tail stub and the demangled C++ name. Use it to find
names for `BaseAddress: {Sym: ...}`, `SymPLT`, `StubFunction` and `NopCall`.

### Environment variables

| Variable | What it does |
|---|---|
| `KOBOPATCHER_HOME` | The workspace folder (default `~/kobopatcher`). |
| `KOBOPATCHER_PATCHSETS_URL` | Where patch sets come from, like `--patchsets`. |
| `KOBOPATCHER_NO_TUI=1` | Use the numbered text menu instead of the full-screen one. |
| `KOBOPATCHER_GO_COMPAT=1` | Behave exactly like kobopatch, including its bugs (for comparisons; see [differences.md](differences.md)). |

## Where things are kept

The guided mode keeps one folder per firmware version in the workspace,
`~/kobopatcher/` by default:

```
~/kobopatcher/4.38.23429/
  kobopatch.yaml               the patch set's configuration; your choice is in its overrides:
  src/                         the patch files
  source.yaml                  where Kobo publishes this firmware, with its checksum
  kobo-update-4.38.23429.zip   the firmware, downloaded once
  out/KoboRoot.tgz             the result
  out/log.txt                  the detailed log of the last build
```

Running `kobopatcher` again reuses all of it. Deleting the folder starts from
scratch (the firmware is then downloaded again).

kobopatcher connects to two places, and only in the guided mode and `restore`:
- `github.com/kebasaa/kobopatcher/releases/download/patchsets/` for the patch sets;
- Kobo's servers for the firmware: `ereaderfiles.kobo.com`, and, for models whose
  download folder kobopatcher doesn't know, Kobo's update service
  `api.kobobooks.com`. It sends only the model number there, with a placeholder
  serial number.

## Working with a patch-set folder by hand

Instead of the guided mode you can use a patch-set folder directly, as with
kobopatch:

1. Get a folder: a set from [`src/versions`](../src/versions) of this repository
   (clone it or download it as a zip), one from the workspace, or a community set
   from [kobopatch-patches](https://github.com/pgaskin/kobopatch-patches/releases)
   (for firmware up to 4.38.21908; it also contains scripts and binaries of the
   original kobopatch, which you don't need). Old sets have no categories, so the
   menu lists their patches under "Other".
2. Put the firmware zip of exactly that version where `in:` in `kobopatch.yaml`
   says (our sets: next to `kobopatch.yaml`; community sets: in `src/`), or pass it
   with `-f`. Kobo's download links for every version and model are collected on
   [pgaskin.net/KoboStuff/kobofirmware.html](https://pgaskin.net/KoboStuff/kobofirmware.html).
3. Choose patches with `kobopatcher --select`, or edit `Enabled: yes/no` in the
   patch files, or list your choice under `overrides:` in `kobopatch.yaml`:

   ```yaml
   overrides:
     src/libnickel/reading-layout.yaml:
       My 10 line spacing values: yes
     src/libnickel/menus.yaml:
       Hide browser from beta features: yes
   ```

   The keys must be written exactly as in the `patches:` section, and the patch
   names exactly as in the patch file. Only one patch of a `PatchGroup` can be on.
4. Run `kobopatcher` in the folder (or `kobopatcher path/to/kobopatch.yaml`). It
   shows each file it patches and each patch it applies (`APPLY`) or skips
   (`SKIP`). If a patch can't be applied, it stops with an error saying which patch
   and which instruction failed, and writes no output. The details go to the log
   file (`log:`, usually `out/log.txt`).
5. Install `out/KoboRoot.tgz` with `kobopatcher device install --config
   kobopatch.yaml`, or copy it to `KOBOeReader/.kobo/KoboRoot.tgz` yourself (show
   hidden folders first: **View → Show → Hidden items** in Windows Explorer,
   **Cmd + Shift + .** in macOS Finder, usually **Ctrl + H** on Linux), then eject
   the Kobo.

## The menu

Every patch of a set is one entry, shown in its category (patch files can set it
with a `Category` line). A patch that changes several files, like lazy binding for
22 libraries, is still one entry. Patches in the same `PatchGroup` are
alternatives: switching one on switches the others off.

| Key | |
|---|---|
| Up/Down, j/k, PgUp/PgDn, Home/End | Move |
| Space | Switch the patch on/off (on a category: collapse it) |
| Enter or c | Collapse/expand a category; C: all |
| a / n | All / none of the current category |
| / | Filter by text (Esc clears) |
| e | Edit a value (device settings) |
| r | Back to the defaults |
| s / p | Save / save and build |
| q | Quit (asks if something changed) |
| ? | Help |

Without a terminal (or with `KOBOPATCHER_NO_TUI=1`) the menu is a numbered list:
type numbers or ranges (`3 5-7`) to switch, `d N` for details, `s` to save, `q` to
quit.

The choice is saved as `overrides:` in `kobopatch.yaml`, only for patches that
differ from their default.

## kobopatch.yaml reference

```yaml
version: 4.38.23429                  # required
in: kobo-update-4.38.23429.zip       # required
out: out/KoboRoot.tgz                # required
log: out/log.txt                     # required
patches:
  src/libnickel/reading-fonts.yaml: usr/local/Kobo/libnickel.so.1.0.0
  src/nickel/home-library.yaml: usr/local/Kobo/nickel
overrides:
  src/nickel/home-library.yaml:
    Some patch: yes
    Another patch: no
lrelease: /usr/lib/qt6/bin/lrelease
translations:
  src/nickel-de.ts: usr/local/Kobo/translations/nickel-de.qm
files:
  src/hyph_de.dic: usr/share/hyphen/hyph_de.dic
  src/fonts/MyFont.ttf:
    - usr/local/Kobo/fonts/MyFont.ttf
  src/kobo.qss:
    dest: etc/kobo.qss
    if: "src/libnickel/experimental.yaml: Use a stylesheet pruned for the Aura H2O"
    mode: 0644
symlinks:
  usr/share/hyphen/hyph_de.dic: usr/share/hyphen/hyph_de_DE.dic
```

| Key | Meaning |
|---|---|
| `version` | The firmware version the patches are for. Required. `device install --config` compares it with the Kobo's version. |
| `in` | The firmware update zip (or a kobopatch-testdata `.tar.xz`). Required. `-f` overrides it. |
| `out` | Where to write `KoboRoot.tgz`. Required. Its folder is created if needed. |
| `log` | Where to write the detailed log. Required. |
| `patchFormat` | Deprecated and optional. The format of each patch file comes from its extension: `.yaml`/`.yml` is the YAML format, `.patch` the old patch32lsb format. If given, it must be `kobopatch` or `patch32lsb`. |
| `patches` | Patch file → the path of the file it patches inside `KoboRoot.tgz` (without a leading `./` or `/`). Several patch files can patch the same file; they are applied in the order listed. A target that isn't in the firmware is an error. |
| `overrides` | Patch file (spelled exactly as in `patches`) → patch name → `yes`/`no`. Overrides the `Enabled` state of those patches. A name that doesn't exist is an error. |
| `lrelease` | Path to Qt's `lrelease`, used for `translations`. Default: found on the PATH (also as `lrelease-qt6`/`lrelease-qt5` and in the usual Qt install folders). |
| `translations` | Qt translation source (`.ts`) → path of the compiled `.qm` inside `KoboRoot.tgz`, which must start with `usr/local/Kobo/translations/`. |
| `files` | Local file → path (or list of paths) inside `KoboRoot.tgz` where it is installed. Paths must not start with `/`. Files are installed with mode 0777, owned by root. A path can also be written as `{dest: PATH, if: "PATCHFILE: patch name", mode: 0644}`: then the file is only added when that patch is on, with that mode. |
| `symlinks` | Target → symlink path, both inside `KoboRoot.tgz`. The target must be one of the `files` destinations; the link points to `/<target>`. |

Unknown keys and duplicate keys are errors, which catches typos.

## Patch files

A patch file maps patch names to lists of instructions. Each list item has
exactly one instruction (an illustrative example; the symbol and bytes are made up):

```yaml
Increase the maximum font size:
  - Enabled: no
  - PatchGroup: Font size
  - Category: "Reading: fonts and text"
  - Description: Allows font sizes up to 150.
  - BaseAddress: {Sym: "_ZN19ReadingSettingsView13fontSizeSliderEv", Rel: 0x20}
  - ReplaceBytes: {Offset: 4, FindH: 4F F0 5A 01, ReplaceH: 4F F0 96 01}
```

- Patches are applied in alphabetical order of their names. Before each patch,
  the *base address* (the position that offsets count from) is reset to 0.
- `Enabled` (`yes`/`no`), `Description`, `PatchGroup`, `Category` and
  `Experimental` describe the patch; everything else changes the file. A patch must
  contain at least one instruction that changes something.
- Only one patch per `PatchGroup` may be enabled.
- Numbers can be decimal (`16`), hex (`0x10`), octal (`0o20` or `020`) or binary
  (`0b10000`). Quoted values are always text.
- Indent with spaces, never tabs.

| Instruction | What it does |
|---|---|
| `BaseAddress` | Set the base address: a number, a symbol name, or `{Offset \| Sym \| SymPLT \| SymPLTTail, Rel}`. |
| `FindBaseAddressString` / `FindBaseAddressHex` | Set the base address to the first occurrence of a string / of hex bytes. |
| `ReplaceBytes` | Replace bytes at base+`Offset`; bytes as lists (`Find: [0x4f, 0xf0]`), hex (`FindH`/`ReplaceH`) or generated Thumb branches (`FindInstBLX`, `ReplaceInstBLX`, `FindInstBW`, `ReplaceInstBW`, `ReplaceInstNOP`); `Base` sets the base address for this instruction only; `CheckOnly` only checks. |
| `ReplaceString` | Replace a string at or after base+`Offset` (shorter replacements are padded). |
| `FindReplaceString` | Find a string and replace it. |
| `ReplaceInt` / `ReplaceFloat` | Replace a byte value / an 8-byte double at base+`Offset`. |
| `ReplaceText` | Replace text in a text file such as a shell script; the length may change. |
| `FindZlib` / `FindZlibHash` | Set the base address to the compressed CSS stylesheet containing some text / with a SHA1. |
| `ReplaceZlib` / `ReplaceZlibGroup` | Change text inside that stylesheet (recompressed; must still fit). |
| `ElfLazyBinding` | Make a library resolve its imported functions on first use instead of all at start (as firmware did until 4.30). |
| `ElfClearDynFlag`, `ElfRemoveDynEntry`, `ElfDisableProgramHeader` | The individual steps of `ElfLazyBinding`. |
| `StubFunction` | Make a function return immediately (optionally with a value). |
| `NopCall` | Remove one call instruction, after checking it calls the expected function. |
| `Tombstone` | Replace the file with an empty one (how Kobo itself disables plugins). |
| `FindBaseAddressSymbol`, `ReplaceBytesAtSymbol`, `ReplaceBytesNOP`, `ReplaceBLX` | Deprecated forms kept for old patch files. |

The ELF instructions work on the structure of the program or library instead of
offsets found by hand: kobopatcher finds the right place itself (in the dynamic
section, the program headers or the symbol table), so the same patch works on many
firmware versions. Each one checks its preconditions and refuses unsafe cases:

```yaml
Lazy binding like firmware 4.7:
  - Enabled: no
  - ElfLazyBinding: true

Disable Kobo Plus promo check on Home:
  - Enabled: no
  - StubFunction: {Sym: _ZN18HomePageController22maybeShowKoboPlusPromoEv, Return: void}
```

Files ending in `.patch` use the old format of the `patch32lsb` tool. It is
supported for old patch sets; new patches should use YAML. The full reference of
all instructions and both formats is in [reference.md](reference.md).

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

## Publishing patch sets

The sets in [`src/versions`](../src/versions) are generated by
`tools/make_patchset.py` (a new firmware version is added there). Each has a
`source.yaml` saying where Kobo publishes the firmware and its checksum.

On every change to `src/versions` on `main`, the `patchsets` workflow runs
`tools/publish_patchsets.py`, which packs each set into `kobopatcher-<version>.zip`
and writes `index.json`, and uploads them to the
[`patchsets` release](https://github.com/kebasaa/kobopatcher/releases/tag/patchsets).
The guided mode downloads from there. To try sets before publishing them:

```bash
kobopatcher --patchsets src/versions
```
