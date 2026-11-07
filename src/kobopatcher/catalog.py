"""The patch catalog: every patch of a patch set as a selectable menu entry.

A patch set is a kobopatch.yaml with its patch files. Each patch name is one
entry; the same name in several patch files (e.g. lazy binding for 22
libraries) is one entry with several targets. Entries are grouped by their
`Category`; PatchGroups make entries mutually exclusive (radio buttons).

The same Selection model (entries with an on/off state) is used for device
settings, so the TUI and the command-line flags work for both.
"""

import fnmatch
import json
import os
import re

from . import config as configmod
from . import patchfile
from .errors import KobopatcherError

OTHER = "Other"


class CatalogError(KobopatcherError):
    pass


class Entry:
    """One selectable item."""

    def __init__(self, id, name, category="", description="", experimental=False):
        self.id = id
        self.name = name
        self.category = category or OTHER
        self.description = description
        self.experimental = experimental
        self.targets = []  # [(patch file, target path)]
        self.groups = set()  # {(patch file, PatchGroup)}
        self.default = False  # the state written in the patch file(s)
        self.enabled = False  # the current state

    def target_names(self):
        return sorted({os.path.basename(t) for _, t in self.targets})

    def to_json(self):
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "experimental": self.experimental,
            "enabled": self.enabled,
            "default": self.default,
            "targets": [t for _, t in self.targets],
            "groups": sorted(g for _, g in self.groups),
        }


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "patch"


class Selection:
    """A list of entries with on/off states and mutually exclusive groups."""

    def __init__(self, entries, title=""):
        self.entries = entries
        self.title = title
        self._by_id = {e.id: e for e in entries}

    def categories(self):
        """Category names in the order of their first entry ("Other" last)."""
        seen = []
        for e in self.entries:
            if e.category not in seen:
                seen.append(e.category)
        if OTHER in seen:
            seen.remove(OTHER)
            seen.append(OTHER)
        return seen

    def in_category(self, cat):
        return [e for e in self.entries if e.category == cat]

    def get(self, key):
        return self._by_id.get(key)

    def conflicts(self, entry):
        """Other entries that share a PatchGroup with entry."""
        if not entry.groups:
            return []
        return [e for e in self.entries if e is not entry and e.groups & entry.groups]

    def set(self, entry, on: bool):
        """Switch an entry on or off; switching on turns off its group mates.

        Returns the entries that were switched off because of a group.
        """
        off = []
        if on:
            for e in self.conflicts(entry):
                if e.enabled:
                    e.enabled = False
                    off.append(e)
        entry.enabled = on
        return off

    def toggle(self, entry):
        return self.set(entry, not entry.enabled)

    def match(self, pattern):
        """Entries whose id or name matches (case-insensitive, * and ? globs)."""
        p = pattern.lower()
        return [e for e in self.entries if fnmatch.fnmatchcase(e.id, p) or fnmatch.fnmatchcase(e.name.lower(), p)]

    def apply_flags(self, enable=(), disable=(), only=()):
        """--only, --enable and --disable, in that order."""
        if only:
            keep = set()
            for pat in only:
                m = self.match(pat)
                if not m:
                    raise CatalogError("--only %r matches no patch (see --list)" % pat)
                keep.update(id(e) for e in m)
            for e in self.entries:
                e.enabled = False
            for e in self.entries:
                if id(e) in keep:
                    self.set(e, True)
        for pat in enable:
            m = self.match(pat)
            if not m:
                raise CatalogError("--enable %r matches no patch (see --list)" % pat)
            for e in m:
                self.set(e, True)
        for pat in disable:
            m = self.match(pat)
            if not m:
                raise CatalogError("--disable %r matches no patch (see --list)" % pat)
            for e in m:
                e.enabled = False

    def changed(self):
        return [e for e in self.entries if e.enabled != e.default]

    def format_list(self, color=False) -> str:
        """The --list output."""
        lines = []
        if self.title:
            lines.append(self.title)
        width = max((len(e.id) for e in self.entries), default=10)
        for cat in self.categories():
            lines.append("")
            lines.append(cat)
            for e in self.in_category(cat):
                mark = "[x]" if e.enabled else "[ ]"
                tags = []
                if e.experimental:
                    tags.append("NEW/EXPERIMENTAL")
                if e.groups:
                    tags.append("group: " + ", ".join(sorted(g for _, g in e.groups)))
                tgt = ", ".join(e.target_names())
                if len(tgt) > 40:
                    tgt = "%d files" % len(e.targets)
                lines.append(
                    "  %s %-*s  %s%s%s"
                    % (mark, width, e.id, e.name, "  (%s)" % tgt if tgt else "", "  [" + "; ".join(tags) + "]" if tags else "")
                )
        if any(e.experimental for e in self.entries):
            lines.append("")
            lines.append("NEW/EXPERIMENTAL entries have not been run on a device yet. Enable them one at a time.")
        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps([e.to_json() for e in self.entries], indent=2, ensure_ascii=False)


class PatchCatalog(Selection):
    """The entries of a patch set (kobopatch.yaml)."""

    def __init__(self, cfg, version=""):
        self.cfg = cfg
        entries = []
        by_name = {}
        used_ids = set()
        for pf, target in cfg.patches.items():
            fmt = patchfile.format_for_file(pf)
            try:
                ps = patchfile.read_from_file(fmt, pf)
            except KobopatcherError as e:
                raise CatalogError("could not load patch file '%s': %s" % (pf, e)) from None
            parsed = getattr(ps, "parsed", None)
            for name in ps.names():
                p = parsed[name] if parsed is not None else None
                e = by_name.get(name)
                if e is None:
                    eid = slug(name)
                    n = 2
                    while eid in used_ids:
                        eid = "%s-%d" % (slug(name), n)
                        n += 1
                    used_ids.add(eid)
                    e = Entry(
                        eid,
                        name,
                        category=getattr(p, "category", ""),
                        description=getattr(p, "description", "").strip(),
                        experimental=getattr(p, "experimental", False),
                    )
                    by_name[name] = e
                    entries.append(e)
                    e.default = bool(getattr(p, "enabled", False))
                else:
                    e.default = e.default or bool(getattr(p, "enabled", False))
                    if not e.category or e.category == OTHER:
                        e.category = getattr(p, "category", "") or e.category
                e.targets.append((pf, target))
                for g in getattr(p, "patch_groups", []) or []:
                    e.groups.add((pf, g))
        for e in entries:
            e.enabled = e.default
        # the overrides of kobopatch.yaml
        for pf, ov in (cfg.overrides or {}).items():
            for name, on in (ov or {}).items():
                e = by_name.get(name)
                if e is not None and any(t[0] == pf for t in e.targets):
                    e.enabled = bool(on)
        # categories keep the order of the patch files; experimental last
        entries.sort(key=lambda e: e.experimental)
        title = "Patches for firmware %s" % (version or cfg.version)
        super().__init__(entries, title)

    def overrides(self):
        """The overrides that turn the patch files' defaults into the current
        selection: {patch file: {patch name: enabled}}, only for differences."""
        out = {}
        for e in self.entries:
            if e.enabled == e.default:
                continue
            for pf, _ in e.targets:
                out.setdefault(pf, {})[e.name] = e.enabled
        return out

    def apply_to_config(self):
        """Put the current selection into cfg.overrides (for this run)."""
        new = {}
        for pf in self.cfg.patches:
            new[pf] = {}
        for e in self.entries:
            for pf, _ in e.targets:
                new[pf][e.name] = e.enabled
        self.cfg.overrides = {pf: v for pf, v in new.items() if v}


def load(conf_path: str):
    """Load the patch set of a kobopatch.yaml (paths relative to its folder).

    Changes into the config's folder, like the patching run does.
    """
    with open(conf_path, "rb") as f:
        buf = f.read()
    d = os.path.dirname(conf_path)
    if d:
        os.chdir(d)
    cfg = configmod.load(buf)
    return cfg, PatchCatalog(cfg)


# --- saving the selection to kobopatch.yaml ---------------------------------------------

_TOP_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*:")


def _q(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def render_overrides(ov) -> str:
    lines = ["overrides:"]
    if not ov:
        lines[0] = "overrides: {}"
    for pf in sorted(ov):
        lines.append("  %s:" % _q(pf))
        for name in sorted(ov[pf]):
            lines.append("    %s: %s" % (_q(name), "yes" if ov[pf][name] else "no"))
    return "\n".join(lines) + "\n"


def save_overrides(conf_path: str, ov) -> str:
    """Rewrite only the overrides: block of kobopatch.yaml (keeping the rest,
    comments included) and return the path of the backup."""
    with open(conf_path, encoding="utf-8") as f:
        text = f.read()
    lines = text.splitlines(keepends=True)
    start = None
    for i, ln in enumerate(lines):
        if re.match(r"^overrides\s*:", ln):
            start = i
            break
    block = "# Written by kobopatcher (--select / --save); edit freely.\n" + render_overrides(ov)
    if start is None:
        new = text + ("" if text.endswith("\n") or not text else "\n") + "\n" + block
    else:
        end = start + 1
        while end < len(lines):
            ln = lines[end]
            if _TOP_KEY.match(ln):
                break
            end += 1
        # keep trailing comment lines that belong to the next section
        while end - 1 > start and lines[end - 1].lstrip().startswith("#") and not lines[end - 1].startswith(" "):
            end -= 1
        prev = start
        if prev > 0 and lines[prev - 1].startswith("# Written by kobopatcher"):
            prev -= 1
        new = "".join(lines[:prev]) + block + "".join(lines[end:])
    configmod.load(new.encode("utf-8"))  # must still be a valid config
    bak = conf_path + ".bak"
    with open(bak, "w", encoding="utf-8") as f:
        f.write(text)
    with open(conf_path, "w", encoding="utf-8") as f:
        f.write(new)
    return bak
