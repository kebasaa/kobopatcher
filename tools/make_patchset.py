"""Build a kobopatcher patch set (src/versions/VERSION) for Kobo firmware 4.38.

    python tools/make_patchset.py VERSION --upstream KOBOPATCH-PATCHES/src/versions/4.38.21908 [--out DIR]

The set contains:

* the patches picked from kobopatch-patches 4.38.21908 (MIT, see NOTICE.md),
  each with a menu Category, the chosen default (on/off) and a credit comment,
  with the patches that moved since 4.38.21908 re-anchored;
* the patches built from the Kobo Aura H2O analysis (docs of
  kebasaa/kobo_aura_h2o), all off by default and marked Experimental:
  lazy binding, function stubs and call NOPs, plugin tombstones, IME and
  stylesheet loading, sickel and dhcpcd, and boot-script tweaks;
* kobopatch.yaml, the pruned global stylesheet (src/kobo.qss) and a README.

Version-specific values (the few fixed offsets) are in VERSIONS below; add a
new firmware there after checking the anchors.
"""

import argparse
import os
import re
import subprocess
import sys

UPSTREAM_VERSION = "4.38.21908"


def yq(s):
    """A YAML double-quoted string."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t") + '"'

# fixed offsets that differ per firmware (found with a masked byte signature
# of the 4.38.21908 code, see the git history)
VERSIONS = {
    "4.38.23429": {"comfortlight_base": 0xE91D74, "polyfill": False},
    "4.38.23852": {"comfortlight_base": 0xEA4364, "polyfill": True, "page_buttons_blx": True},
}

TARGETS = {
    "libnickel.so.1.0.0": ("libnickel", "usr/local/Kobo/libnickel.so.1.0.0"),
    "nickel": ("nickel", "usr/local/Kobo/nickel"),
    "librmsdk.so.1.0.0": ("librmsdk", "usr/local/Kobo/librmsdk.so.1.0.0"),
    "libadobe.so": ("libadobe", "usr/local/Kobo/libadobe.so"),
}

# category -> (file name, [patch names]); patch names as in kobopatch-patches
CATEGORIES = [
    ("Reading: fonts and text", "reading-fonts", [
        "Custom font sizes", "Unify font sizes",
        "Force user font-family in ePubs (Part 1 of 2)", "Force user font-family in ePubs (Part 2 of 2)",
        "Un-force font-family override p tags (std epubs)", "Un-Force user font-family in KePubs",
        "Default ePub sans-serif font (Gill Sans)", "Default ePub serif font (Amasis)",
        "Default ePub symbol font (Symbol)", "Default ePub monospace font", "ePub constant font sharpness",
    ]),
    ("Reading: line spacing, margins, layout", "reading-layout", [
        "My 10 line spacing values", "My 24 line spacing values", "Force user line spacing in KePubs",
        "Force user line spacing in ePubs (part 1 of 2)", "Force user line spacing in ePubs (Part 2 of 2)",
        "Custom left & right margins", "Custom kepub default margins", "ePub fixed top/bottom margins",
        "ePub disable built-in body padding-bottom", "Disable orphans/widows avoidance",
        "Ignore ePub book Adobe XPGT stylesheet (page-template.xpgt)", "Ignore ePub book CSS and Adobe XPGT stylesheets",
        "Set KePub hyphenation", "KePub stylesheet additions - text justify", "KePub stylesheet additions - word-spacing",
        "Un-Force user text-align in div,p tags in KePubs", "Un-force link decoration in KePubs",
    ]),
    ("Reading: header, footer, navigation, page turns", "reading-navigation", [
        "Custom header/footer page number text", "Remove title from reading header/footer", "Swap reading header/footer",
        "Reduce new header/footer height", "Custom header/footer captions", "Reduce top/bottom page spacer",
        "Custom navigation menu page number text", "Increase page navigation history", "Change TOC level indentation",
        "Increase size of kepub chapter progress chart", "Customise Header back button", "Both page turn sides go next",
        "Both page turn buttons go next", "Replace adobe page numbers toggle with invert screen",
    ]),
    ("Home screen and library", "home-library", [
        "Increase home screen cover size", "Remove footer (row3) on new home screen",
        "Remove footer (row3) and increase cover size on new home screen", "Remove recommendations (row1col2) from home screen",
        "Change Browse Kobo home screen link target - Activity", "New home screen subtitle custom font",
        "Rename new home screen footer", "Remove line from bottom tab bar", "Increase headlines font",
        "Increase library cover size", "Series list increase cover thumbnails", "Custom collection/author header title font",
        "Custom synopsis font size", "Custom synopsis details line spacing", "Increase Book Details synopsis area",
    ]),
    ("Dictionary", "dictionary", [
        "Dictionary pop-up - increase available text area", "Dictionary text font-family/font-size/line-height",
        "Shorten dictionary entry not found message",
    ]),
    ("Device, power and safety", "device", [
        "Block WiFi firmware upgrade", "Always show confirmation dialog before upgrading",
        "Custom Sleep/Power-off timeouts", "Larger Sleep/Power-off timeouts", "Allow USB storage even when device locked",
        "DeveloperSettings - AutoUsbGadget", "PowerSettings - UnlockEnabled", "Remove forgot pin button from lock screen",
        "Don't grab exclusive access to event0", "Allow rotation on all devices",
        "Allow showing info panel on random screensaver", "Customize ComfortLight settings",
    ]),
    ("Menus and features", "menus", [
        "Disable all tutorial dialogs", "Remove beta features not supported text", "Hide browser from beta features",
        "FeatureSettings - BookSpecificStats", "FeatureSettings - ExportHighlights", "FeatureSettings - MyWords",
        "Bulgarian Phonetic Keyboard (GloHD/ClaraHD/AuraOne/H2O2/Forma/Libra)", "Cyrillic Keyboard (GloHD/ClaraHD/AuraOne/H2O2)",
        "Greek Keyboard (GloHD/ClaraHD/AuraOne/H2O2)",
    ]),
    ("PDF", "pdf", ["Remove PDF map widget shown during panning"]),
]

ON_BY_DEFAULT = {
    "Custom font sizes", "Unify font sizes", "Un-force font-family override p tags (std epubs)",
    "Un-Force user font-family in KePubs", "ePub constant font sharpness", "Force user line spacing in KePubs",
    "ePub fixed top/bottom margins", "Disable orphans/widows avoidance", "Set KePub hyphenation",
    "KePub stylesheet additions - text justify", "Change TOC level indentation",
    "Increase size of kepub chapter progress chart", "Always show confirmation dialog before upgrading",
    "Allow USB storage even when device locked", "DeveloperSettings - AutoUsbGadget", "Disable all tutorial dialogs",
    "Remove beta features not supported text",
}

# the two halves of these patches become one menu entry (same name in both files)
RENAMES = {
    "Force user font-family in ePubs (Part 1 of 2)": "Force user font-family in ePubs",
    "Force user font-family in ePubs (Part 2 of 2)": "Force user font-family in ePubs",
    "Force user line spacing in ePubs (part 1 of 2)": "Force user line spacing in ePubs",
    "Force user line spacing in ePubs (Part 2 of 2)": "Force user line spacing in ePubs",
}

NOTES = {
    "Both page turn buttons go next": "Made for the Kobo Forma's page-turn buttons; the Aura H2O has none.",
    "Customize ComfortLight settings": "For models with ComfortLight; the Aura H2O doesn't have it.",
    "Bulgarian Phonetic Keyboard (GloHD/ClaraHD/AuraOne/H2O2/Forma/Libra)": "Made for other models' keyboard layout, not the Aura H2O (N250).",
    "Cyrillic Keyboard (GloHD/ClaraHD/AuraOne/H2O2)": "Made for other models' keyboard layout, not the Aura H2O (N250).",
    "Greek Keyboard (GloHD/ClaraHD/AuraOne/H2O2)": "Made for other models' keyboard layout, not the Aura H2O (N250).",
}

HEADER_RX = re.compile(r"^([^\s#][^\n]*):\s*$")


def split_blocks(text):
    """[(name, text)] of a kobopatch YAML file, each block with the comment
    lines directly above it."""
    lines = text.splitlines(keepends=True)
    starts = []
    for i, ln in enumerate(lines):
        m = HEADER_RX.match(ln)
        if m:
            j = i
            while j > 0 and lines[j - 1].startswith("#"):
                j -= 1
            starts.append((j, i, m.group(1)))
    blocks = []
    for k, (j, i, name) in enumerate(starts):
        end = starts[k + 1][0] if k + 1 < len(starts) else len(lines)
        body = "".join(lines[j:end]).rstrip("\n") + "\n"
        blocks.append((name, i - j, body))
    return blocks


def adapt(name, body, category, author, version):
    """Rename, set Enabled and Category, add the credit, re-anchor."""
    new_name = RENAMES.get(name, name)
    lines = body.splitlines(keepends=True)
    out = ["# Originally by %s in kobopatch-patches %s (MIT, see NOTICE.md).\n" % (author, UPSTREAM_VERSION)]
    done_enabled = False
    for ln in lines:
        if HEADER_RX.match(ln) and ln.startswith(name):
            ln = "%s:\n" % new_name
        elif not done_enabled and re.match(r"^\s*-\s*Enabled:\s*\S+", ln):
            indent = ln[: len(ln) - len(ln.lstrip())]
            ln = "%s- Enabled: %s\n%s- Category: %s\n" % (indent, "yes" if name in ON_BY_DEFAULT else "no", indent, yq(category))
            done_enabled = True
        out.append(ln)
    text = "".join(out)
    if name in NOTES:
        text = text.rstrip("\n") + "\n  # Note (kobopatcher): %s\n" % NOTES[name]
    # re-anchoring of the patches that moved after 4.38.21908
    if name == "Customize ComfortLight settings":
        text = text.replace(
            "  - BaseAddress: 0xE82E2C # find the base of the unnamed subroutine with the x-ref to _ZN5QTimeC1Eiiii",
            "  - BaseAddress: 0x%X # %s (0xE82E2C in 4.38.21908); find the base of the unnamed subroutine with the x-ref to _ZN5QTimeC1Eiiii"
            % (VERSIONS[version]["comfortlight_base"], version),
        )
    elif name == "Increase size of kepub chapter progress chart":
        # two stylesheets contain the rule; this is the one 4.38.21908 patched
        # (unchanged in 4.38.23429 and 4.38.23852)
        text = text.replace("  - BaseAddress: 0x196E0F6 # qss/ReadingMenuStatsView.qss",
                            "  - FindZlibHash: 182e3dc69c4d7e91340b1e03cf901a94a6ced20e # qss/ReadingMenuStatsView.qss")
    elif name == "Remove forgot pin button from lock screen":
        # the stylesheet exists twice (identical); patch both copies: after the
        # first is changed, its hash differs and the second one is found
        m = re.search(r"  - BaseAddress: 0x1972E04 # qss/PinCodeInputDialog.qss\n(  - ReplaceZlib:\n(?:      .*\n)+)", text)
        repl = m.group(1)
        anchor = "  - FindZlibHash: 0ec0849942386e73da9d40a5b873206b7720a6ff # qss/PinCodeInputDialog.qss (%s)\n"
        text = text.replace(m.group(0), anchor % "first copy" + repl + anchor % "second copy" + repl)
    elif name == "Both page turn buttons go next" and VERSIONS[version].get("page_buttons_blx"):
        # recompiled: the button now calls prevPageWithTimer through the PLT
        # (BLX at +64) instead of tail-calling it (B.W at +82)
        text = text.replace(
            '      Offset:        82\n'
            '      FindInstBW:    {SymPLTTail: "ReadingView::prevPageWithTimer()"}\n'
            '      ReplaceInstBW: {SymPLTTail: "ReadingView::nextPageWithTimer()"}\n',
            '      Offset:        64 # %s (82 with a B.W in 4.38.21908)\n'
            '      FindInstBLX:    {SymPLT: "ReadingView::prevPageWithTimer()"}\n'
            '      ReplaceInstBLX: {SymPLT: "ReadingView::nextPageWithTimer()"}\n' % version,
        )
    elif name == "Remove PDF map widget shown during panning":
        text = re.sub(
            r"  # TODO: figure out what broke the plt parsing in kobopatch for libadobe in 18730\+\n"
            r"  - ReplaceBytes: \{Offset:  80, FindInstBW: 0x1C224, ReplaceInstBW: 0x1B51C\}\n"
            r"  - ReplaceBytes: \{Offset: 192, FindInstBW: 0x1C224, ReplaceInstBW: 0x1B51C\}\n",
            "  # kobopatcher decodes libadobe's PLT, so the targets are given by name\n"
            '  - ReplaceBytes: {Offset:  80, FindInstBW: {SymPLTTail: "N3AdobeReader::updatePanningMap()"}, ReplaceInstBW: {SymPLTTail: "N3AdobeReader::hideMapWidget()"}}\n'
            '  - ReplaceBytes: {Offset: 192, FindInstBW: {SymPLTTail: "N3AdobeReader::updatePanningMap()"}, ReplaceInstBW: {SymPLTTail: "N3AdobeReader::hideMapWidget()"}}\n',
            text,
        )
    return text


# --- the patches from the Aura H2O analysis ---------------------------------------------------------

EXP_NOTE = "NEW: built from the Kobo Aura H2O analysis; not yet run on a device."
PERF = "Performance (experimental)"
BOOT = "Boot (experimental)"
PLUG = "Plugins (experimental)"
STORE = "Store (experimental)"

LAZY_LIBS = [
    ("libnickel.so.1.0.0", "usr/local/Kobo/libnickel.so.1.0.0"),
    ("librmsdk.so.1.0.0", "usr/local/Kobo/librmsdk.so.1.0.0"),
    ("libiwnn.so.1.0.0", "usr/local/Kobo/libiwnn.so.1.0.0"),
    ("libsunpinyin.so.3.0.0", "usr/local/Kobo/libsunpinyin.so.3.0.0"),
    ("libQtSolutions_IOCompressor-2.3.so.1.0.0", "usr/local/Kobo/libQtSolutions_IOCompressor-2.3.so.1.0.0"),
] + [
    ("libQt%s.so.4.6.2" % m, "usr/local/Trolltech/QtEmbedded-4.6.2-arm/lib/libQt%s.so.4.6.2" % m)
    for m in ("Concurrent", "Core", "DBus", "Gui", "Multimedia", "MultimediaWidgets", "Network", "PrintSupport",
              "Script", "Sensors", "Sql", "Svg", "WebKit", "WebKitWidgets", "Widgets", "Xml", "XmlPatterns")
]

PLUGINS = [
    ("libboggle.so", "the Boggle game"), ("librushhour.so", "the Rush Hour game"), ("libsolitaire.so", "the Solitaire game"),
    ("libsudoku.so", "the Sudoku game"), ("libscribble.so", "the Sketch Pad"), ("libwikipedia.so", "Wikipedia lookups"),
    ("libpdb.so", "the Instapaper/MOBI (.pdb) reader"), ("libsandbox.so", "the sandbox plugin"),
]


def meta(name, category, desc, enabled="no"):
    d = "\n".join("      " + ln for ln in (desc + "\n\n" + EXP_NOTE).splitlines())
    return "%s:\n  - Enabled: %s\n  - Category: %s\n  - Experimental: yes\n  - Description: |\n%s\n" % (name, enabled, yq(category), d)


LAZY = meta(
    "Lazy binding like firmware 4.7",
    PERF,
    "Since 4.30 these libraries resolve every imported function (about 39,700 in total) when a program "
    "starts, in nickel and in every Adobe helper. This switches them back to resolving on first use, as "
    "firmware 4.7 did (clears DF_1_NOW, removes DT_BIND_NOW, turns PT_GNU_RELRO into PT_NULL). "
    "Try libnickel and librmsdk first; use --check-elf. (kobopatch-extensions.md §4.1)",
) + "  - ElfLazyBinding: true\n"

LIBNICKEL_DOC = [
    meta("Disable sketch overlay timer on page turn", PERF,
         "Removes the two SketchNote::disableWithTimer calls in ReadingView::nextPage/prevPage: a 100 ms timer and an "
         "allocation on every page turn for the stylus overlay, which the Aura H2O can't use. Check afterwards that "
         "highlighting still works. (kobopatch-extensions.md §4.3)")
    + "  - NopCall: {Sym: _ZN11ReadingView8nextPageEv, Rel: 0xc, Target: _ZN10SketchNote16disableWithTimerEi}\n"
    "  - NopCall: {Sym: _ZN11ReadingView8prevPageEv, Rel: 0xc, Target: _ZN10SketchNote16disableWithTimerEi}\n",
    meta("Disable forced full refresh on PDF page turn", PERF,
         "Removes the forced full-screen (flashing) refresh on every PDF page turn. Faster, but more ghosting. "
         "(kobopatch-extensions.md §4.3)")
    + "  - NopCall: {Sym: _ZN11ReadingView8nextPageEv, Rel: 0x1e, Target: _ZN7QWidget12setAttributeEN2Qt15WidgetAttributeEb}\n"
    "  - NopCall: {Sym: _ZN11ReadingView8prevPageEv, Rel: 0x1e, Target: _ZN7QWidget12setAttributeEN2Qt15WidgetAttributeEb}\n",
    meta("Disable Kobo Plus promo check on Home", PERF,
         "HomePageController::maybeShowKoboPlusPromo returns at once: saves two database queries (with four sickel "
         "D-Bus calls) every time Home appears. (kobopatch-extensions.md §4.3)")
    + "  - StubFunction: {Sym: _ZN18HomePageController22maybeShowKoboPlusPromoEv, Return: void}\n",
    meta("Don't load the Japanese and Chinese input methods", PERF,
         "Stops nickel from loading the Japanese (iWnn) and Chinese (SunPinyin) input-method dictionaries at every "
         "start, about 13 MB of RAM and flash reads. Don't add a Japanese or Chinese keyboard while this is on. "
         "(firmware-performance-and-bugs.md §7.1)")
    + '  - FindReplaceString: {Find: ":/iwnn.rcc", Replace: ":/iwnn.rcX"}\n'
    '  - FindReplaceString: {Find: ":/sunpinyin.rcc", Replace: ":/sunpinyin.rcX"}\n',
    meta("Use a stylesheet pruned for the Aura H2O", PERF,
         "Loads /etc/kobo.qss instead of the built-in stylesheet: the same rules without the 535 that can only match "
         "other models (85 KB -> about 33 KB to parse at every start). The file is added to KoboRoot.tgz only when this "
         "patch is on. Check every screen afterwards. (firmware-performance-and-bugs.md §7.4)")
    + '  - FindReplaceString: {Find: ":/default.qss", Replace: "/etc/kobo.qss"}\n',
    meta("Log UI messages at warning level (binary patch)", PERF,
         "Starts the 'ui' log category at warning instead of debug level. The device setting 'quiet-ui-logging' "
         "(kobopatcher device settings) does the same and also covers 'usertrace'; prefer that. "
         "(firmware-performance-and-bugs.md §4.6)")
    + "  - BaseAddress: {Sym: _Z2uiv, Rel: 0x2e}\n  - ReplaceBytes: {FindH: 22 46 05 F5 B6 70, ReplaceH: 01 22 05 F5 B6 70}\n",
    meta("Fewer sickel calls during database queries", PERF,
         "Skips the two sickel watchdog D-Bus calls around every outermost database query on the GUI thread. "
         "Medium risk: very long database work (the first start after a firmware update, huge imports) could then "
         "trip the watchdog and reboot. Switch this off for the first start after an update. "
         "(firmware-performance-and-bugs.md §4.3)")
    + "  - BaseAddress: {Sym: _ZN19ScopedSickelDisableC1Ev, Rel: 0x2c}\n  - ReplaceBytes: {FindH: 86 42 04 D0, ReplaceH: 86 42 00 BF}\n"
    "  - BaseAddress: {Sym: _ZN19ScopedSickelDisableD1Ev, Rel: 0x26}\n  - ReplaceBytes: {FindH: 86 42 07 D0, ReplaceH: 86 42 00 BF}\n",
    meta("Start the Wi-Fi DHCP client without debug output", PERF,
         "dhcpcd is started with -d (debug) at every Wi-Fi start. (firmware-performance-and-bugs.md §4.9)")
    + '  - FindReplaceString: {Find: "dhcpcd -d -z %0 &", Replace: "dhcpcd    -z %0 &"}\n',
]

POLYFILL = meta(
    "Store pages without the JavaScript polyfill", STORE,
    "Raises the memory threshold above which the store pages load an extra JavaScript polyfill. Store pages may "
    "break; only useful with a store. (firmware-performance-and-bugs.md §4.9, S13)",
) + "  - BaseAddress: {Sym: _ZNK17N3WebStoreWebPage36deviceHasSufficientMemoryForPolyfillEv, Rel: 0x1c}\n" \
    "  - ReplaceBytes: {FindH: B2 F1 A0 5F, ReplaceH: B2 F1 80 4F}\n"

DOSFSCK_RCS = (
    "dosfsck -a -w $ONBOARD_PARTITION || dosfsck -a -w $ONBOARD_PARTITION || dosfsck -a -w $ONBOARD_PARTITION || "
    "dosfsck -a -w $ONBOARD_PARTITION || FS_CORRUPT=1\n"
)


def rt(find, replace):
    return "  - ReplaceText: {Find: %s, Replace: %s}\n" % (yq(find), yq(replace))


SKIP_FSCK = "Skip the disk check after a clean shutdown"
SKIP_FSCK_DESC = (
    "The user partition is checked with dosfsck up to four times at every start. With this, the shutdown script "
    "leaves a marker after its own check, and the next start skips the check when the marker is there; crashes, "
    "watchdog reboots and power loss leave none, so those starts still check. Measure first with "
    "'time dosfsck -n /dev/mmcblk0p3' on the device; if it takes well under a second, it isn't worth it. "
    "(firmware-performance-and-bugs.md S4)"
)

RCS = [
    meta(SKIP_FSCK, BOOT, SKIP_FSCK_DESC)
    + rt(DOSFSCK_RCS, "if [ -e /etc/.onboard_clean ]; then rm -f /etc/.onboard_clean; else " + DOSFSCK_RCS.rstrip("\n") + "; fi\n"),
    meta("Read the Qt libraries ahead in the background", BOOT,
         "rcS reads all Qt libraries (about 58 MB) into the cache before the boot animation starts. This lets it run in "
         "the background while the start continues. (firmware-performance-and-bugs.md S5)")
    + rt("find /usr/local/Trolltech/QtEmbedded-4.6.2-arm/lib/ -type f -exec readahead {} \\;\n",
         "find /usr/local/Trolltech/QtEmbedded-4.6.2-arm/lib/ -type f -exec readahead {} \\; &\n"),
    meta("Set the clock before nickel starts", BOOT,
         "The clock is read from the hardware clock in a background job, so nickel can start with a wrong date. This "
         "sets it just before nickel starts (it takes milliseconds). (firmware-performance-and-bugs.md S6)")
    + rt("\n\t/sbin/hwclock -s -u\n) &\n", "\n) &\n")
    + rt("/usr/local/Kobo/hindenburg &\n", "/sbin/hwclock -s -u\n/usr/local/Kobo/hindenburg &\n"),
    meta("Let the CPU speed adapt to the load", BOOT,
         "The CPU frequency is never managed on this device. 20 seconds after nickel starts, this switches to the "
         "'conservative' governor (down 20 %, up 60 %), if the kernel has it. Battery use may change. "
         "(firmware-performance-and-bugs.md §4.1, S7)")
    + rt("LIBC_FATAL_STDERR_=1 /usr/local/Kobo/nickel -platform kobo -skipFontLoad &\n",
         "LIBC_FATAL_STDERR_=1 /usr/local/Kobo/nickel -platform kobo -skipFontLoad &\n"
         "( sleep 20; CF=/sys/devices/system/cpu/cpu0/cpufreq; if grep -q conservative $CF/scaling_available_governors 2>/dev/null; "
         "then echo conservative > $CF/scaling_governor; echo 20 > $CF/conservative/down_threshold 2>/dev/null; "
         "echo 60 > $CF/conservative/up_threshold 2>/dev/null; fi ) &\n"),
    meta("Use the deadline I/O scheduler", BOOT,
         "Switches the internal storage from the CFQ to the deadline I/O scheduler at start. "
         "(firmware-performance-and-bugs.md §4.5)")
    + rt("FORCE_FACTORY_RESET=0\n", "echo deadline > /sys/block/mmcblk0/queue/scheduler 2>/dev/null\nFORCE_FACTORY_RESET=0\n"),
    meta("Fewer device events at start (measure first)", BOOT,
         "Replaces the full 'udevadm trigger' that runs alongside nickel's start with one limited to input, block, "
         "power_supply and usb devices. Medium risk: nickel may rely on other events; check USB, SD cards, battery and "
         "charging icons afterwards. Only worth it if measured. (firmware-performance-and-bugs.md S8)")
    + rt("[ $PLATFORM != freescale ] && rm -rf /dev/mmcblk1* && udevadm trigger &\n",
         "[ $PLATFORM != freescale ] && rm -rf /dev/mmcblk1* && udevadm trigger --subsystem-match=input "
         "--subsystem-match=block --subsystem-match=power_supply --subsystem-match=usb &\n"),
]

RCK = [meta(SKIP_FSCK, BOOT, SKIP_FSCK_DESC) + rt("umount -a -r\n", "touch /etc/.onboard_clean 2>/dev/null\numount -a -r\n")]

ANIMATOR = [
    meta("Fix the boot animation busy loop", BOOT,
         "The boot animation script only sleeps when it finds an image. The Aura H2O's images aren't in the update, "
         "so it spins at 100 % CPU while nickel starts. This sleeps on every round. Check on the device with "
         "'ls /etc/images | grep -c ^dahlia-on-': 0 means the bug is active. (firmware-performance-and-bugs.md §7.5)")
    + rt("                usleep 250000;\n        fi\n", "        fi\n        usleep 250000;\n"),
]


def tombstone(lib, what):
    return meta("Don't load %s" % what, PLUG,
                "Replaces %s with an empty file, the way Kobo itself disables plugins, so nickel no longer loads it "
                "at every start. %s stops working. (kobopatch-extensions.md §4.2)" % (lib, what[0].upper() + what[1:])) \
        + "  - Tombstone: usr/local/Kobo/%s\n" % lib


# --- writing the set -------------------------------------------------------------------------------


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def build(version, upstream, out):
    vinfo = VERSIONS[version]
    names_to_cat = {n: (cat, fn) for cat, fn, names in CATEGORIES for n in names}
    picked = {}  # (target short, file) -> [blocks]
    found = set()
    for d in sorted(os.listdir(upstream)):
        if d not in {k + ".yaml" for k in TARGETS}:
            continue
        tkey = d[: -len(".yaml")]
        short, _ = TARGETS[tkey]
        for f in sorted(os.listdir(os.path.join(upstream, d))):
            author = f[: -len(".yaml")]
            with open(os.path.join(upstream, d, f), encoding="utf-8") as fh:
                text = fh.read()
            for name, _, body in split_blocks(text):
                if name not in names_to_cat:
                    continue
                cat, fn = names_to_cat[name]
                found.add(name)
                picked.setdefault((tkey, fn), []).append(adapt(name, body, cat, author, version))
    missing = set(names_to_cat) - found
    if missing:
        sys.exit("not found upstream: %s" % ", ".join(sorted(missing)))

    hdr = ("# kobopatcher patch set for Kobo firmware %s. Generated by tools/make_patchset.py;\n"
           "# choose patches with kobopatcher --select instead of editing Enabled here.\n\n" % version)
    patches = {}
    order = [fn for _, fn, _ in CATEGORIES]
    for (tkey, fn), blocks in sorted(picked.items(), key=lambda kv: (order.index(kv[0][1]), kv[0][0])):
        short, target = TARGETS[tkey]
        rel = "src/%s/%s.yaml" % (short, fn)
        write(os.path.join(out, rel), hdr + "\n".join(blocks))
        patches[rel] = target

    doc_libnickel = LIBNICKEL_DOC + ([POLYFILL] if vinfo["polyfill"] else [])
    write(os.path.join(out, "src/libnickel/experimental.yaml"), hdr + "\n".join(doc_libnickel))
    patches["src/libnickel/experimental.yaml"] = TARGETS["libnickel.so.1.0.0"][1]
    for lib, target in LAZY_LIBS:
        rel = "src/lazy/%s.yaml" % lib.split(".so")[0]
        write(os.path.join(out, rel), hdr + LAZY)
        patches[rel] = target
    for lib, what in PLUGINS:
        rel = "src/plugins/%s.yaml" % lib[: -len(".so")]
        write(os.path.join(out, rel), hdr + tombstone(lib, what))
        patches[rel] = "usr/local/Kobo/" + lib
    for rel, target, blocks in (
        ("src/boot/rcS.yaml", "etc/init.d/rcS", RCS),
        ("src/boot/rcK.yaml", "etc/init.d/rcK", RCK),
        ("src/boot/on-animator.yaml", "etc/init.d/on-animator.sh", ANIMATOR),
    ):
        write(os.path.join(out, rel), hdr + "\n".join(blocks))
        patches[rel] = target

    cfg = [
        "# kobopatcher patch set for Kobo firmware %s (made for the Kobo Aura H2O, N250)." % version,
        "# Put the firmware zip next to this file (or pass it with -f), choose patches with",
        "#   kobopatcher --select",
        "# and run kobopatcher. See README.md.",
        "version: %s" % version,
        "in: kobo-update-%s.zip" % version,
        "out: out/KoboRoot.tgz",
        "log: out/log.txt",
        "patches:",
    ] + ["  %s: %s" % (k, v) for k, v in patches.items()] + [
        "files:",
        "  src/kobo.qss:",
        "    dest: etc/kobo.qss",
        '    if: "src/libnickel/experimental.yaml: Use a stylesheet pruned for the Aura H2O"',
        "    mode: 0644",
        "overrides: {}",
    ]
    write(os.path.join(out, "kobopatch.yaml"), "\n".join(cfg) + "\n")
    return patches


NOTICE = """# Notice

Most patches outside the "(experimental)" categories come from
[kobopatch-patches](https://github.com/pgaskin/kobopatch-patches) {up}, by
pgaskin (geek1011), GeoffR, jackie_w, oren64, sherman, jcn363, NiLuJe and the
contributors of the keyboard patches. Each patch names its author in a comment.
They were adapted for firmware {ver}: menu categories, default states, a few
renames (the two halves of a patch now share one name) and new anchors for
patches whose code moved.

kobopatch-patches is distributed under this license:

```
MIT License

Copyright (c) 2018-2023 Patrick Gaskin

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

The patches in the "(experimental)" categories were written for kobopatcher
from the analysis in [kobo_aura_h2o](https://github.com/kebasaa/kobo_aura_h2o)
and are under kobopatcher's MIT license.
"""

README = """# Patches for Kobo firmware {ver} (Kobo Aura H2O, N250)

Use this folder with [kobopatcher](../../../README.md): put
`kobo-update-{ver}.zip` here (or pass it with `-f`), choose patches, run
`kobopatcher`, and copy `out/KoboRoot.tgz` to the Kobo. The main README has
[the step-by-step guide](../../../README.md#patching-your-kobo-step-by-step),
including [installing it on the device](../../../README.md#installing-the-patched-firmware-on-the-device)
and [undoing it](../../../README.md#undoing-patches).

```bash
kobopatcher --select     # choose in a menu (Space switches, s saves, p saves and patches)
kobopatcher --list       # or list them...
kobopatcher --enable custom-font-sizes --disable unify-font-sizes --save   # ...and choose by id
kobopatcher              # build out/KoboRoot.tgz
```

## What's in it

- **Community patches** (categories without "(experimental)"): a selection from
  kobopatch-patches {up}, adapted to {ver}. Some are switched on by default;
  `--list` shows which. Every one of them applies to {ver} (checked with
  `kobopatcher -t` against the firmware); none was tried on a device for this
  version. A few are made for other models and say so in their description.
- **New patches** (the "(experimental)" categories, all off): built from the
  analysis of this device in [kobo_aura_h2o](https://github.com/kebasaa/kobo_aura_h2o)
  (docs `kobopatch-extensions.md`, `firmware-performance-and-bugs.md`). They
  apply to {ver} but **have not been run on a device**. Switch them on one at a
  time, in this order, and check the device after each:
  1. Performance: "Disable Kobo Plus promo check on Home", "Disable sketch
     overlay timer on page turn", "Start the Wi-Fi DHCP client without debug
     output". Check page turns, highlighting and Home.
  2. Plugins: the "Don't load …" entries (games, Sketch Pad, Wikipedia, .pdb).
  3. Performance: "Don't load the Japanese and Chinese input methods", then "Use
     a stylesheet pruned for the Aura H2O" (check every screen).
  4. Performance: "Lazy binding like firmware 4.7" (use `--check-elf`).
  5. Boot: one entry at a time, starting with "Fix the boot animation busy
     loop" and "Set the clock before nickel starts".
  6. Performance: "Fewer sickel calls during database queries" (not for the
     first start after a firmware update).

  Measure before and after (boot time, first PDF open, nickel's `VmRSS`), see
  `firmware-performance-and-bugs.md` §9.
- **Device settings and database maintenance** for the same device are not in
  this folder; use `kobopatcher device settings` and `kobopatcher device db`
  with the Kobo connected (see the main README).

## Not included

These items of the analysis have no exact bytes or script text yet, so they
aren't patches here (future work): e-ink refresh tweaks in libkobo.so, sickel's
watchdog thresholds and ping interval, a WAL-aware boot database check, the
SIGHUP handler, clamping `numPartialUpdatePageTurns`, `pickel
should-factory-reset`, nickel's RPATH, pruning the per-view stylesheets, a home
wallpaper stylesheet, restoring the boot animation images, caching
`kobo_config.sh`, the rcS fixes B6–B8 and udev fixes B11–B14, and SSH hardening.

The analysis also rejects some ideas on purpose (relocation neutering, skipping
static initialisers, removing `NEEDED` libraries, Qt modules, Adobe or DRM code,
the two-field lazy-binding patch, `oom_score_adj` patches, a REAGL binary patch,
stubs for features that are already off); they are deliberately not here.

## Generated

This folder is generated by `tools/make_patchset.py`; regenerate it instead of
editing the files by hand. The few firmware-specific offsets are listed there.
See [NOTICE.md](NOTICE.md) for the origin and license of the community patches.
"""


def write_docs(version, out):
    write(os.path.join(out, "NOTICE.md"), NOTICE.format(up=UPSTREAM_VERSION, ver=version))
    write(os.path.join(out, "README.md"), README.format(up=UPSTREAM_VERSION, ver=version))
    write(os.path.join(out, ".gitignore"), "/out/*\n!/out/.keep\n/*.zip\n")
    write(os.path.join(out, "out", ".keep"), "")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("version", choices=sorted(VERSIONS))
    ap.add_argument("--upstream", required=True, help="kobopatch-patches/src/versions/4.38.21908")
    ap.add_argument("--default-qss", required=True, help="the firmware's :/default.qss (from nickel's resources)")
    ap.add_argument("--out", help="default: src/versions/VERSION")
    a = ap.parse_args()
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = a.out or os.path.join(here, "src", "versions", a.version)
    patches = build(a.version, a.upstream, out)
    write_docs(a.version, out)
    subprocess.run(
        [sys.executable, os.path.join(here, "tools", "prune_qss.py"), a.default_qss, os.path.join(out, "src", "kobo.qss"),
         "--flags", "Dahlia,Dragon"],
        check=True,
    )
    print("wrote %s (%d patch files)" % (out, len(patches)))


if __name__ == "__main__":
    main()
