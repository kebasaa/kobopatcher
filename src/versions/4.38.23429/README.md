# Patch set for firmware 4.38.23429 (Kobo Aura H2O, N250)

> **All patches are UNTESTED on the device and disabled by default.** Undo by
> installing the stock `KoboRoot.tgz` of 4.38.23429.

These patches switch off work that this device cannot use. They are the
patches from §4.1–§4.3 of `docs/kobopatch-extensions.md` in
[kebasaa/kobo_aura_h2o](https://github.com/kebasaa/kobo_aura_h2o), written
with kobopatcher's structure-aware instructions (`ElfLazyBinding`,
`StubFunction`, `NopCall`, `Tombstone`), so the original kobopatch cannot apply them.

| File | Patch | What it does |
|---|---|---|
| `src/libnickel.yaml`, `src/lazy/*.yaml` (22 libraries) | `Lazy binding like firmware 4.7` | Clears `DF_1_NOW`, removes `DT_BIND_NOW` and turns `PT_GNU_RELRO` into `PT_NULL`. Imports are then bound on first use, not all at process start (24,442 in libnickel). |
| `src/libnickel.yaml` | `Disable sketch overlay timer on page turn` | NOPs the `SketchNote::disableWithTimer(int)` calls in `ReadingView::nextPage/prevPage` |
| `src/libnickel.yaml` | `Disable forced full refresh on PDF page turn` | NOPs the `QWidget::setAttribute` calls in `ReadingView::nextPage/prevPage` (optional: more ghosting) |
| `src/libnickel.yaml` | `Disable Kobo Plus promo check on Home` | `HomePageController::maybeShowKoboPlusPromo` returns immediately |
| `src/tombstone/*.yaml` (8 plugins) | `Do not load this plugin` | Replaces boggle, rushhour, solitaire, sudoku, scribble, wikipedia, pdb and sandbox with 0-byte files, so nickel doesn't `dlopen` them. Their Beta Features entries stop working. |

**Lazy binding differs from the doc's raw §4.1 patch.** Every one of the
22 libraries also has a legacy `DT_BIND_NOW` dynamic entry. glibc treats that
entry as eager binding on its own, independently of `DF_1_NOW`. Clearing
`DF_1_NOW` alone therefore changes nothing. Dropping RELRO as well gives up the
hardening and still gains nothing. `ElfLazyBinding` also removes `DT_BIND_NOW`.
`--check-elf` rejects the raw two-field patch for this reason.

`ElfLazyBinding` refuses a library in these cases:
- it is not eagerly bound;
- its `R_ARM_JUMP_SLOT` GOT slots don't all point to the PLT header (checked:
  they do in all 22);
- `PT_GNU_RELRO` is not inside a writable `PT_LOAD`.

## Usage

```sh
cd src/versions/4.38.23429
kobopatcher --check-elf -f /path/to/kobo-update-4.38.23429.zip
```

The output goes to `out/KoboRoot.tgz`, with a verbose log in `out/log.txt`.
To install it on the device (and to undo it), follow
[Installing the patched firmware on the device](../../../README.md#installing-the-patched-firmware-on-the-device)
in the main README.
Enable patches with `Enabled: yes` in the `src/` files, or without editing them
via `overrides:` in `kobopatch.yaml`:

```yaml
overrides:
  src/libnickel.yaml:
    Disable Kobo Plus promo check on Home: yes
```

## Device test ladder

Measure after each step (boot time, first PDF open, `VmRSS` of nickel; see §6
of the doc):

1. The three `libnickel.yaml` stubs. Check that page turns, highlights and Home
   still work.
2. Plugin tombstones. Check with `grep -c /usr/local/Kobo/lib /proc/$(pidof nickel)/maps`.
3. Lazy binding for `libnickel` and `librmsdk`.
4. Lazy binding for QtCore, QtGui, QtWidgets and QtWebKit.
5. Lazy binding for the rest.

## Verified offline (2026-11-07)

These checks ran with every patch enabled against
`kobo-update-4.38.23429.zip`. The output was re-read with `readelf`:
- all 22 libraries have `FLAGS_1: None`, no `BIND_NOW` and no `GNU_RELRO`;
  the former RELRO phdr shows as `NULL`;
- the libnickel stub reads `70 47 00 BF` @ `0xd5f2c0`;
- the NOPs read `00 BF 00 BF` @ `0xf5bb28`, `0xf5bae4`, `0xf5bb3a`, `0xf5baf6`;
- the 8 plugins are 0-byte files with their original mode and owner;
- readelf printed no warnings, and `--check-elf` passed for all 30 files.

The unit tests in `patchlib/elf_test.go` assert the doc's offsets and bytes
against the same binaries (set `KOBOPATCH_TEST_ROOTFS` to the extracted rootfs).
