# kobopatcher

kobopatcher changes the firmware of Kobo eReaders with *patches*: small,
checked edits that change how the device behaves. For example, it can change the
font sizes and line spacing on offer, what the home screen shows, how margins
work, or which features load at all.

You connect your Kobo, run `kobopatcher`, tick the patches you want in a menu, and
kobopatcher does the rest: it finds out which firmware your Kobo runs, downloads
the matching patches and Kobo's firmware, builds the patched files and copies
them to the Kobo. The Kobo installs them when you unplug it. One command undoes
everything again.

kobopatcher is an independent Python reimplementation of
[kobopatch](https://github.com/pgaskin/kobopatch) by Patrick Gaskin, and reads
the same patch files. It runs in a terminal on Linux, macOS and Windows.

> **Warning.** kobopatcher and its patches are unofficial and not supported by
> Kobo. A patch is only as good as its author's testing; patches marked **NEW**
> have not been run on a device yet. Read what a patch does before you switch it
> on. You can always go back to the unmodified firmware
> (see [Reset to stock firmware](#reset-to-stock-firmware)), but you use patches
> at your own risk.

## Contents

- [Quick start](#quick-start)
- [Tutorial](#tutorial)
  - [1. Install kobopatcher](#1-install-kobopatcher)
  - [2. Connect your Kobo](#2-connect-your-kobo)
  - [3. Run kobopatcher](#3-run-kobopatcher)
  - [4. Choose your patches](#4-choose-your-patches)
  - [5. Build and copy to the Kobo](#5-build-and-copy-to-the-kobo)
  - [6. Let the Kobo install it](#6-let-the-kobo-install-it)
- [Changing your patches later](#changing-your-patches-later)
- [Reset to stock firmware](#reset-to-stock-firmware)
- [When Kobo updates your device](#when-kobo-updates-your-device)
- [Without a connected Kobo](#without-a-connected-kobo)
- [Device settings and database maintenance](#device-settings-and-database-maintenance)
- [Which firmware versions have patches](#which-firmware-versions-have-patches)
- [FAQ](#faq)
- [Troubleshooting](#troubleshooting)
- [More documentation](#more-documentation)
- [Credits and license](#credits-and-license)

## Quick start

```bash
pipx install git+https://github.com/kebasaa/kobopatcher
```

Plug in your Kobo, tap **Connect** on its screen, then:

```bash
kobopatcher
```

Answer its questions, pick patches in the menu, press **p**, and unplug the Kobo
when it says so. The tutorial below goes through the same steps in detail.

## Tutorial

### 1. Install kobopatcher

kobopatcher needs Python 3.10 or newer. Install it with
[pipx](https://pipx.pypa.io/), which puts the `kobopatcher` command on your PATH
without touching your system Python:

```bash
pipx install git+https://github.com/kebasaa/kobopatcher
```

Check that it works:

```bash
kobopatcher --version
```

<details>
<summary>Other ways to install</summary>

With pip, into a virtual environment:

```bash
python3 -m venv ~/kobopatcher-venv
~/kobopatcher-venv/bin/pip install git+https://github.com/kebasaa/kobopatcher
```

When a release is published on the
[releases page](https://github.com/kebasaa/kobopatcher/releases), it also has
standalone programs for Linux, macOS and Windows that don't need Python.
Download the one for your system, make it executable (`chmod +x` on Linux and
macOS), and use it in place of `kobopatcher`.
</details>

### 2. Connect your Kobo

Connect the Kobo to your computer with a USB cable and tap **Connect** on its
screen. It shows up as a drive called `KOBOeReader`. Leave it connected.

kobopatcher finds the drive by itself. If it doesn't, tell it where it is with
`--mount`, for example `--mount /media/$USER/KOBOeReader` (Linux),
`--mount /Volumes/KOBOeReader` (macOS) or `--mount E:\` (Windows).

### 3. Run kobopatcher

Open a terminal and run:

```bash
kobopatcher
```

(Run it in any folder except a patch-set folder; see
[More documentation](#more-documentation) for working with those by hand.)

kobopatcher finds the Kobo and asks what you want to do:

```
[1] Looking for a connected Kobo
Found a Kobo Aura H2O at /media/you/KOBOeReader, running firmware 4.38.23429.
What do you want to do?
  1) Patch the firmware  (default)
  2) Reset to stock firmware (undo all patches)
  3) Change device settings
  4) Quit
Choose 1-4:
```

Press **Enter** to patch. kobopatcher then gets what it needs:

```
[2] Finding the patch set for firmware 4.38.23429

[3] Setting up the workspace
Downloading the patch set into /home/you/kobopatcher/4.38.23429
The firmware isn't here yet: https://ereaderfiles.kobo.com/firmwares/kobo5/Aug2025/kobo-update-4.38.23429.zip
Download kobo-update-4.38.23429.zip (116 MB) from Kobo's server? [Y/n]
```

- The **patch set** is the list of patches for exactly this firmware version.
  kobopatcher downloads it from this project.
- The **firmware** is Kobo's own update file for your model and version; the
  patches are applied to the programs inside it. kobopatcher downloads it from
  Kobo's server once you say yes, checks that it is the right file, and keeps it
  for next time.

Both go into a folder per firmware version, `~/kobopatcher/4.38.23429/`.

### 4. Choose your patches

Next, kobopatcher opens the menu:

```
Patches for firmware 4.38.23429  -  /home/you/kobopatcher/4.38.23429
- Reading: fonts and text (5 of 10 on)
    [x]  Custom font sizes
    [ ]  Force user font-family in ePubs  (group)
    [x]  Un-Force user font-family in KePubs
    ...
- Performance (experimental) (0 of 9 on)
    [ ]  Disable Kobo Plus promo check on Home  (NEW)
    ...
-----------------------------------------------------------------------
Changes the range of sizes on the font size slider so that there are
more of the small sizes and fewer of the large sizes. ...
```

- Move with the **arrow keys**. The description of the patch under the cursor is
  shown at the bottom (or on the right in a wide terminal).
- Press **Space** to switch a patch on `[x]` or off `[ ]`.
- Some patches are **alternatives**: switching one on switches the other off. The
  menu tells you when that happens.
- Patches marked **NEW** are experimental: they apply to the firmware, but nobody
  has run them on a device yet. Leave them off at first; if you want to try them,
  switch them on one at a time (each patch set's README gives an order).
- Patches marked **NOT RECOMMENDED** (in red) work, but are best left off; the
  description says why. "All on" leaves them off.
- Some patches are switched on by default: the well-known community patches that
  most people want. You can switch them off.
- Press **/** to search, **?** for all keys, **q** to quit without building.

When you're happy, press **p** to build. Your choice is saved, so next time the
menu starts where you left off.

### 5. Build and copy to the Kobo

kobopatcher now builds `KoboRoot.tgz`, the archive of patched files:

```
[5] Building KoboRoot.tgz
Patching ./usr/local/Kobo/libnickel.so.1.0.0
  APPLY `Custom font sizes`
  ...
Successfully saved patched KoboRoot.tgz to out/KoboRoot.tgz.

[6] Installing on the Kobo
Copy KoboRoot.tgz to the Kobo now? [Y/n]
```

Say yes. kobopatcher checks once more that the Kobo runs the version the patches
are for, and copies the file into the Kobo's hidden `.kobo` folder.

If a patch can't be applied, kobopatcher stops with an error naming the patch and
builds nothing; nothing is copied to the Kobo. See
[Troubleshooting](#troubleshooting).

### 6. Let the Kobo install it

1. **Eject the Kobo safely** (Windows: "Safely Remove Hardware"; macOS: Eject in
   Finder; Linux: eject it in the file manager) and unplug the cable.
2. The Kobo notices the file and restarts. It checks the archive, unpacks it over
   its system files, deletes it and restarts once more. This takes a minute or
   two; **don't switch it off meanwhile**. (A damaged archive fails the check and
   is deleted without being installed.)
3. Check that the patches work, for example the new font sizes in the reading
   settings.

That's it.

## Changing your patches later

Connect the Kobo and run `kobopatcher` again. Everything is already downloaded, so
it goes straight to the menu with your previous choice. Change it, press **p**,
copy, eject. The new `KoboRoot.tgz` replaces all patched files, so a patch you
switched off is undone too.

If the published patch set was improved in the meantime, kobopatcher offers to
update it first; your choice is kept.

## Reset to stock firmware

To undo all patches, connect the Kobo and run:

```bash
kobopatcher restore
```

(or choose **Reset to stock firmware** in `kobopatcher`'s first question).
kobopatcher copies Kobo's own, unmodified files of the firmware your Kobo runs to
it. Eject the Kobo; it installs them like an update. Your books, settings and
accounts stay.

- If kobopatcher changed the Kobo's settings (see
  [Device settings](#device-settings-and-database-maintenance)), it offers to put
  them back as well.
- Files that patches *added* (like the pruned stylesheet `/etc/kobo.qss`) can't be
  deleted this way. They stay on the Kobo but aren't used by the stock firmware;
  kobopatcher lists them.
- If the Kobo still misbehaves afterwards, `kobopatcher restore --full` reinstalls
  the whole firmware update, as Kobo's own updater does.
- **By hand:** open `kobo-update-<version>.zip` (in `~/kobopatcher/<version>/`),
  copy the `KoboRoot.tgz` inside it to `KOBOeReader/.kobo/` (show hidden folders:
  **View → Show → Hidden items** in Windows Explorer, **Cmd + Shift + .** in macOS
  Finder, usually **Ctrl + H** on Linux), eject.
- If the Kobo no longer starts at all, a factory reset brings back a working
  system; Kobo's help site describes how to do one on each model. It erases the
  books and settings on the Kobo.

## When Kobo updates your device

A firmware update from Kobo replaces the patched files with unpatched ones, so the
patches are gone. Run `kobopatcher` again: it sees the new version and uses the
patch set for it, if there is one yet (see
[Which firmware versions have patches](#which-firmware-versions-have-patches)).

**Updating your Kobo to a newer firmware on purpose.** kobopatcher always patches
the version your Kobo runs. If it tells you that patches exist for a newer
version, update the Kobo first: connect it to Wi-Fi and sync, and it offers the
update; or install the update by hand by unpacking Kobo's
`kobo-update-<version>.zip` into the `.kobo` folder and ejecting. Then run
`kobopatcher` again.

## Without a connected Kobo

You can prepare everything without the Kobo and copy the result later:

```bash
kobopatcher --target 4.38.23429
```

Without a Kobo, kobopatcher doesn't know your model. It downloads the firmware for
the model the patch set was made with, and says which one that is. For another
model, add `--model` with its model number (shown by `kobopatcher device info`,
e.g. `--model 371` for the Glo HD), or give it the firmware zip you have with
`-f kobo-update-4.38.23429.zip`.

The result is in `~/kobopatcher/4.38.23429/out/KoboRoot.tgz`. Copy it into the
Kobo's `.kobo` folder, or connect the Kobo and run:

```bash
kobopatcher device install ~/kobopatcher/4.38.23429/out/KoboRoot.tgz --config ~/kobopatcher/4.38.23429/kobopatch.yaml
```

## Device settings and database maintenance

Some improvements aren't patches but settings or database work on the Kobo. Connect
it, tap **Connect**, and choose **Change device settings** in `kobopatcher`, or run
these directly:

```bash
kobopatcher device info              # model and firmware
kobopatcher device settings --select # settings in a menu
kobopatcher device db --list         # database maintenance
```

Settings (in `Kobo eReader.conf`; switching one off removes it again; a backup is
made before every change, and `kobopatcher device settings --undo` restores it):

| Id | What it does |
|---|---|
| `quiet-ui-logging` | UI log messages at warning instead of debug level (less work on every tap). |
| `no-reading-stats` | Stops collecting reading statistics, which pile up on a device that never syncs. |
| `no-periodic-sync` | No automatic daily sync and its wake-ups (keep it off if you sync with Calibre-Web automatically). |
| `no-reagl` | Normal page-turn waveform instead of REAGL; try it as an A/B test. |
| `no-forced-gc16` | Fewer full refreshes on pages with images (more ghosting). |
| `cover-sleep-screen` | The book cover full-screen while asleep (also needed for custom sleep images). |
| `ignore-captive-portal` | Use Wi-Fi networks without internet access. |
| `calibre-web-store` | Use a Calibre-Web server as store and sync: `--set calibre-web-store=https://SERVER/kobo/TOKEN`. |

Database actions (each copies the database first):

| Action | What it does |
|---|---|
| `analytics-cleanup` | Deletes the optional analytics events, then compacts and checks the database. |
| `compact` | Rebuilds `KoboReader.sqlite` with 4 KB pages and checks it. |
| `date-shift` | Moves reading dates stuck in 2012 (after the clock reset) to now, keeping their order. Set the clock first. |
| `secure-db-rebuild` | Moves the broken secure database (`BookReader.sqlite`) off the Kobo, which then creates a new one. Needed before Calibre-Web sync works. Loses Kobo-store tokens. You have to type `rebuild` to confirm. |

Eject the Kobo afterwards. Settings take effect when it restarts.

## Which firmware versions have patches

kobopatcher's patch sets are kept in [`src/versions`](src/versions) and downloaded
automatically:

| Firmware | Patch set |
|---|---|
| 4.38.23429 | [`src/versions/4.38.23429`](src/versions/4.38.23429) |
| 4.38.23852 | [`src/versions/4.38.23852`](src/versions/4.38.23852) (the newest for the Aura H2O) |

Each contains:
- a selection of the community patches of
  [kobopatch-patches](https://github.com/pgaskin/kobopatch-patches) 4.38.21908,
  adapted to the version; some are on by default;
- new patches from an analysis of the Kobo Aura H2O (lazy binding, function stubs,
  plugins that need not load, input methods, a pruned stylesheet, boot-script
  tweaks), marked **NEW**, off by default and **not yet run on a device**.

Every patch applies to its firmware (checked with `kobopatcher -t`). The sets were
made with, and checked against, the Aura H2O's firmware. For other models of the
same version, a patch that doesn't match your model's files is refused rather than
applied wrongly. Each set's README lists the recommended order for trying the NEW
patches and what was left out.

For **older firmware** (up to 4.38.21908), use the community patch sets of
[kobopatch-patches](https://github.com/pgaskin/kobopatch-patches/releases) by hand
(see [docs/usage.md](docs/usage.md#working-with-a-patch-set-folder-by-hand)). For
other versions there is no patch set yet: a set for a nearby version won't apply.

## FAQ

**Is it safe?**
kobopatcher refuses a patch that doesn't match the firmware exactly, so a patch
can't land in the wrong place. It never changes the Kobo without asking first,
and `kobopatcher restore` undoes everything. Whether a particular patch is a good
idea on your device is another question: start with few patches, and leave NEW
ones off until you're comfortable.

**Is kobopatcher as reliable as kobopatch?**
It was compared with kobopatch on the published patch sets for 50 firmware
versions: with every patch applied on its own, and with all compatible patches
together, both produce byte-identical files (see [docs/parity.md](docs/parity.md)).

**What does kobopatcher download, and from where?**
Only in the guided mode and `restore`: the patch set from this project's GitHub
releases, and the firmware from Kobo's server `ereaderfiles.kobo.com`, after
asking. For models it doesn't know, it asks Kobo's update service which download
folder to use, sending only the model number. Everything goes into
`~/kobopatcher/`.

**Which firmware version must be installed?**
Exactly the one the patches are for. kobopatcher takes care of that when the Kobo
is connected. Patches for another version either fail to apply or, worse, are
applied to a device whose other files don't match.

**Can I use old patch sets made for kobopatch?**
Yes. Run `kobopatcher` in the patch set's folder, where you would have run
`kobopatch.sh` or `kobopatch.bat`; see [docs/usage.md](docs/usage.md). A few
kobopatch bugs are fixed, which can change the result of patches that relied on
them; see [docs/differences.md](docs/differences.md).

**How do I update kobopatcher?**
`pipx upgrade kobopatcher`, or `pipx install --force git+https://github.com/kebasaa/kobopatcher`.

**Where is my choice of patches stored?**
In `~/kobopatcher/<version>/kobopatch.yaml`, under `overrides:`. Deleting the
folder starts from scratch.

**Can I write my own patches?**
Yes; see [docs/usage.md](docs/usage.md#patch-files) and the full
[instruction reference](docs/reference.md).

## Troubleshooting

- **`No Kobo found`:** tap **Connect** on the Kobo's screen (it has to show that
  it's connected), check the cable, or pass the drive with `--mount`.
- **`There is no patch set for firmware ...`:** nobody has made one for that
  version yet. See [Which firmware versions have patches](#which-firmware-versions-have-patches).
- **`could not connect to ...`:** kobopatcher needs internet access to download the
  patch set and the firmware the first time. Behind a proxy, set `HTTPS_PROXY`.
- **`the server's certificate couldn't be checked`:** your Python has no
  certificate store. On macOS with Python from python.org, run
  **Install Certificates.command** in its folder in Applications.
- **`Kobo doesn't publish this firmware there for your model`:** download
  `kobo-update-<version>.zip` for your model from
  [pgaskin.net/KoboStuff/kobofirmware.html](https://pgaskin.net/KoboStuff/kobofirmware.html)
  and pass it with `-f`.
- **`doesn't have the expected SHA-256`:** the download was damaged or isn't the
  expected file. Nothing was kept; try again.
- **A patch failed (`could not find specified bytes at offset`):** the patch
  doesn't match this firmware: wrong version, wrong model, or another enabled patch
  already changed those bytes. Switch that patch off and build again. The log is in
  `~/kobopatcher/<version>/out/log.txt`; include it when reporting a problem.
- **`more than one patch enabled in PatchGroup`:** enable only one patch of that
  group (the menu does this for you).
- **`a KoboRoot.tgz is already waiting on the Kobo`:** a previous update hasn't
  been installed. Eject the Kobo and let it install, or delete
  `KOBOeReader/.kobo/KoboRoot.tgz`.
- **CSS patches are slow:** your Python's zlib module isn't classic zlib (e.g.
  zlib-ng on Windows with Python 3.14), so kobopatcher uses its pure-Python
  compressor. The result is the same. Python 3.13 or a standalone program is
  faster.
- **`zlib self-test failed`:** neither Python's zlib module nor kobopatcher's own
  compressor produced classic zlib's output. Please report this with your Python
  version and operating system.

## More documentation

- [docs/usage.md](docs/usage.md): every command and option, the workspace,
  working with a patch-set folder by hand (as with kobopatch), the menu keys, the
  `kobopatch.yaml` format, writing and testing patches, and how the patch sets
  are published.
- [docs/reference.md](docs/reference.md): every patch instruction, with examples.
- [docs/differences.md](docs/differences.md): where kobopatcher behaves
  differently from kobopatch.
- [docs/parity.md](docs/parity.md): the comparison with kobopatch.
- [docs/development.md](docs/development.md): running the tests and building.

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
