"""Interactive selection of patches (or device settings) in the terminal.

`run(selection, context, actions)` shows a checkbox menu grouped by category
and returns the chosen action ("save", "patch", ...) or "quit". It uses curses
when stdin and stdout are a terminal, and a numbered text menu otherwise (or
when KOBOPATCHER_NO_TUI=1, or curses isn't available).

The menu logic is in `MenuState`, which works without a terminal (tests drive
it with key names). Only the in-memory selection is changed.
"""

import os
import sys
import textwrap

DEFAULT_ACTIONS = [("s", "save", "Save"), ("p", "patch", "Save and patch")]

HELP = [
    "Up/Down, j/k, PgUp/PgDn, Home/End   move",
    "Space                               switch the entry on/off",
    "Enter or c (on a category)          collapse/expand it; C: all",
    "a / n                               all / none in this category",
    "e                                   edit the entry's value (if it has one)",
    "/                                   filter (Esc clears)",
    "r                                   reset to the defaults",
    "q or Esc                            quit",
]


class Row:
    __slots__ = ("kind", "category", "entry")

    def __init__(self, kind, category, entry=None):
        self.kind, self.category, self.entry = kind, category, entry


class MenuState:
    """The menu, independent of how it is drawn."""

    def __init__(self, selection, actions=None):
        self.sel = selection
        self.actions = actions or DEFAULT_ACTIONS
        self.collapsed = set()
        self.filter = ""
        self.cursor = 0
        self.message = ""
        self.confirm_quit = False
        self.result = None  # set when the menu is done
        self.editing = None  # (entry, buffer) while editing a value
        self.filtering = None  # buffer while typing a filter
        self.show_help = False
        self.rows = []
        self.rebuild()
        self._skip_to_entry(1)

    # --- rows ------------------------------------------------------------------

    def _visible(self, e):
        if not self.filter:
            return True
        f = self.filter.lower()
        return f in e.name.lower() or f in e.id or f in (e.description or "").lower()

    def rebuild(self):
        rows = []
        for cat in self.sel.categories():
            es = [e for e in self.sel.in_category(cat) if self._visible(e)]
            if not es:
                continue
            rows.append(Row("cat", cat))
            if cat in self.collapsed and not self.filter:
                continue
            rows.extend(Row("entry", cat, e) for e in es)
        self.rows = rows
        self.cursor = max(0, min(self.cursor, len(rows) - 1))

    def current(self):
        return self.rows[self.cursor] if self.rows else None

    def current_entry(self):
        r = self.current()
        return r.entry if r is not None and r.kind == "entry" else None

    def _skip_to_entry(self, step):
        if self.rows and self.rows[self.cursor].kind == "cat":
            i = self.cursor
            while 0 <= i < len(self.rows) and self.rows[i].kind == "cat":
                i += step
            if 0 <= i < len(self.rows):
                self.cursor = i

    def move(self, n):
        if self.rows:
            self.cursor = max(0, min(len(self.rows) - 1, self.cursor + n))

    # --- keys --------------------------------------------------------------------

    def key(self, k, page=10):
        """Handle one key: a name ("up", "down", "pgup", "pgdn", "home", "end",
        "space", "enter", "esc", "backspace") or a single character.
        Returns self.result (None while the menu continues)."""
        if self.editing is None and self.filtering is None:
            self.message = ""
        if self.show_help:
            self.show_help = False
            return None
        if self.editing is not None:
            return self._key_edit(k)
        if self.filtering is not None:
            return self._key_filter(k)
        if self.confirm_quit:
            self.confirm_quit = False
            if k in ("y", "Y"):
                self.result = "quit"
            else:
                self.message = "Not quitting."
            return self.result
        if k in ("up", "k"):
            self.move(-1)
        elif k in ("down", "j"):
            self.move(1)
        elif k == "pgup":
            self.move(-page)
        elif k == "pgdn":
            self.move(page)
        elif k == "home":
            self.cursor = 0
        elif k == "end":
            self.cursor = max(0, len(self.rows) - 1)
        elif k == "space":
            e = self.current_entry()
            if e is not None:
                off = self.sel.toggle(e)
                if off:
                    self.message = "Switched off (same group): " + ", ".join(o.name for o in off)
            elif self.current() is not None:
                self._toggle_collapse(self.current().category)
        elif k in ("enter", "c"):
            r = self.current()
            if r is not None and (r.kind == "cat" or k == "c"):
                self._toggle_collapse(r.category)
        elif k == "C":
            cats = set(self.sel.categories())
            self.collapsed = set() if self.collapsed >= cats else cats
            self.rebuild()
        elif k in ("a", "n"):
            r = self.current()
            if r is not None:
                skipped = False
                for e in self.sel.in_category(r.category):
                    if k == "n":
                        e.enabled = False
                    elif e.groups:
                        skipped = True
                    else:
                        e.enabled = True
                if skipped:
                    self.message = "Entries in a group were left alone (only one per group can be on)."
        elif k == "r":
            for e in self.sel.entries:
                e.enabled = e.default
            self.message = "Reset to the defaults."
        elif k == "e":
            e = self.current_entry()
            if e is not None and getattr(e, "value", None) is not None:
                self.editing = (e, str(e.value))
            elif e is not None:
                self.message = "This entry has no value to edit."
        elif k == "/":
            self.filtering = self.filter
        elif k == "?":
            self.show_help = True
        elif k in ("q", "esc"):
            if self.sel.changed():
                self.confirm_quit = True
                self.message = "Discard your changes? (y/n)"
            else:
                self.result = "quit"
        else:
            for key, action, _ in self.actions:
                if k == key:
                    self.result = action
        return self.result

    def _toggle_collapse(self, cat):
        if cat in self.collapsed:
            self.collapsed.discard(cat)
        else:
            self.collapsed.add(cat)
        self.rebuild()
        for i, r in enumerate(self.rows):
            if r.kind == "cat" and r.category == cat:
                self.cursor = i
                break

    def _key_edit(self, k):
        e, buf = self.editing
        if k == "enter":
            e.value = buf
            e.enabled = True
            self.editing = None
            self.message = "Set %s to %r." % (e.name, buf)
        elif k == "esc":
            self.editing = None
        elif k == "backspace":
            self.editing = (e, buf[:-1])
        elif k == "space":
            self.editing = (e, buf + " ")
        elif len(k) == 1:
            self.editing = (e, buf + k)
        return None

    def _key_filter(self, k):
        buf = self.filtering
        if k == "enter":
            self.filtering = None
            return None
        if k == "esc":
            self.filtering = None
            self.filter = ""
        else:
            if k == "backspace":
                buf = buf[:-1]
            elif k == "space":
                buf += " "
            elif len(k) == 1:
                buf += k
            self.filtering = buf
            self.filter = buf
        self.rebuild()
        self._skip_to_entry(1)
        return None

    # --- texts ------------------------------------------------------------------------

    def row_text(self, r):
        if r.kind == "cat":
            es = self.sel.in_category(r.category)
            arrow = "+" if r.category in self.collapsed and not self.filter else "-"
            return "%s %s (%d of %d on)" % (arrow, r.category, sum(e.enabled for e in es), len(es))
        e = r.entry
        tags = []
        if e.experimental:
            tags.append("NEW")
        if e.groups:
            tags.append("group")
        if getattr(e, "value", None) is not None:
            tags.append("= %s" % e.value)
        changed = "*" if e.enabled != e.default else " "
        return "  [%s]%s %s%s" % ("x" if e.enabled else " ", changed, e.name, "  (" + ", ".join(tags) + ")" if tags else "")

    def detail_lines(self, width):
        e = self.current_entry()
        if e is None:
            r = self.current()
            if r is None:
                return ["No entries match the filter."]
            return ["%s: Space or Enter collapses/expands it; a/n switches all of it on/off." % r.category]
        out = []
        if e.experimental:
            out.append("NEW/EXPERIMENTAL: not yet run on a device.")
        for para in (e.description or "No description.").split("\n"):
            out.extend(textwrap.wrap(para, max(10, width)) or [""])
        tg = e.target_names() if hasattr(e, "target_names") else []
        if tg:
            out.append("Patches: " + (", ".join(tg) if len(tg) <= 4 else "%d files" % len(tg)))
        if e.groups:
            out.append("Only one of: " + ", ".join([e.name] + [o.name for o in self.sel.conflicts(e)]))
        if getattr(e, "value", None) is not None:
            out.append("Value: %s  (e to edit)" % e.value)
        out.append("Id: %s" % e.id)
        return out

    def hint(self):
        acts = "  ".join("%s %s" % (k, label) for k, _, label in self.actions)
        return "Space on/off  / filter  ? help  %s  q quit" % acts


# --- curses ---------------------------------------------------------------------------------


def _curses_key(curses, ch):
    if isinstance(ch, str):
        return {"\n": "enter", "\r": "enter", " ": "space", "\x1b": "esc", "\x7f": "backspace", "\b": "backspace"}.get(ch, ch)
    return {
        curses.KEY_UP: "up",
        curses.KEY_DOWN: "down",
        curses.KEY_PPAGE: "pgup",
        curses.KEY_NPAGE: "pgdn",
        curses.KEY_HOME: "home",
        curses.KEY_END: "end",
        curses.KEY_ENTER: "enter",
        curses.KEY_BACKSPACE: "backspace",
        curses.KEY_RESIZE: "resize",
    }.get(ch, "")


def _run_curses(state, context):
    import curses

    def put(scr, y, x, text, n, attr=0):
        try:
            scr.addnstr(y, x, text, max(0, n), attr)
        except curses.error:
            pass

    def draw(scr):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        color = False
        if curses.has_colors():
            try:
                curses.start_color()
                curses.use_default_colors()
                curses.init_pair(1, curses.COLOR_YELLOW, -1)
                curses.init_pair(2, curses.COLOR_CYAN, -1)
                color = True
            except curses.error:
                color = False
        top = 0
        while state.result is None:
            h, w = scr.getmaxyx()
            scr.erase()
            if h < 12 or w < 40:
                put(scr, 0, 0, "Terminal too small (need 40x12). q quits.", w - 1)
                scr.refresh()
                k = _curses_key(curses, scr.get_wch())
                if k in ("q", "esc"):
                    state.result = "quit"
                continue
            side = w >= 120
            list_w = w // 2 if side else w
            detail_h = 0 if side else max(5, h // 3)
            list_h = h - 3 - detail_h
            title = "%s  -  %s" % (state.sel.title, context) if context else state.sel.title
            put(scr, 0, 0, title, w - 1, curses.A_BOLD)
            if state.cursor < top:
                top = state.cursor
            if state.cursor >= top + list_h:
                top = state.cursor - list_h + 1
            for i, r in enumerate(state.rows[top : top + list_h]):
                attr = curses.A_REVERSE if top + i == state.cursor else 0
                if r.kind == "cat":
                    attr |= curses.A_BOLD | (curses.color_pair(2) if color else 0)
                elif r.entry.experimental and color:
                    attr |= curses.color_pair(1)
                put(scr, 1 + i, 0, state.row_text(r).ljust(list_w - 1), list_w - 1, attr)
            if side:
                dw = w - list_w - 2
                for i, line in enumerate(state.detail_lines(dw)[: h - 3]):
                    put(scr, 1 + i, list_w + 1, line, dw)
            else:
                y0 = 1 + list_h
                try:
                    scr.hline(y0, 0, curses.ACS_HLINE, w - 1)
                except curses.error:
                    pass
                for i, line in enumerate(state.detail_lines(w - 2)[: detail_h - 1]):
                    put(scr, y0 + 1 + i, 0, line, w - 1)
            if state.editing is not None:
                status = "%s = %s_   (Enter sets it, Esc cancels)" % (state.editing[0].name, state.editing[1])
            elif state.filtering is not None:
                status = "Filter: %s_   (Enter keeps it, Esc clears it)" % state.filtering
            else:
                status = state.message or state.hint()
            put(scr, h - 2, 0, status, w - 1, curses.A_BOLD if state.message else 0)
            put(scr, h - 1, 0, "%d of %d entries on" % (sum(e.enabled for e in state.sel.entries), len(state.sel.entries)), w - 1)
            if state.show_help:
                for i, line in enumerate(HELP[: h - 2]):
                    put(scr, 1 + i, 2, line.ljust(min(w - 4, 70)), w - 4, curses.A_REVERSE)
            scr.refresh()
            try:
                k = _curses_key(curses, scr.get_wch())
            except KeyboardInterrupt:
                k = "esc"
            if k and k != "resize":
                state.key(k, page=max(1, list_h - 1))

    curses.wrapper(draw)
    return state.result


# --- plain text fallback ------------------------------------------------------------------------


def _run_plain(state, context, inp=input, out=print):
    numbered = []

    def show():
        numbered.clear()
        out("")
        out(state.sel.title + ("  -  " + context if context else ""))
        for cat in state.sel.categories():
            out("")
            out(cat)
            for e in state.sel.in_category(cat):
                numbered.append(e)
                tags = (" [NEW]" if e.experimental else "") + (" [group]" if e.groups else "")
                val = " = %s" % e.value if getattr(e, "value", None) is not None else ""
                out("  %3d [%s] %s%s%s" % (len(numbered), "x" if e.enabled else " ", e.name, val, tags))
        acts = ", ".join("%s = %s" % (k, label) for k, _, label in state.actions)
        out("")
        out("Type numbers or ranges to switch entries (e.g. 3 5-7), d N for details, e N to edit a value, %s, q to quit." % acts)

    show()
    while True:
        try:
            line = inp("> ").strip()
        except EOFError:
            return "quit"
        if not line:
            continue
        for key, action, _ in state.actions:
            if line == key:
                return action
        if line == "q":
            if state.sel.changed():
                try:
                    if inp("Discard your changes? (y/n) ").strip().lower() != "y":
                        continue
                except EOFError:
                    pass
            return "quit"
        parts = line.split()
        if parts[0] in ("d", "e") and len(parts) == 2 and parts[1].isdigit():
            i = int(parts[1]) - 1
            if not 0 <= i < len(numbered):
                out("No entry %s." % parts[1])
                continue
            e = numbered[i]
            if parts[0] == "d":
                for n, r in enumerate(state.rows):
                    if r.entry is e:
                        state.cursor = n
                for ln in state.detail_lines(76):
                    out("  " + ln)
            elif getattr(e, "value", None) is None:
                out("That entry has no value to edit.")
            else:
                try:
                    e.value = inp("%s [%s]: " % (e.name, e.value)).strip() or e.value
                    e.enabled = True
                except EOFError:
                    return "quit"
                show()
            continue
        nums, ok = [], True
        for p in parts:
            a, sep, b = p.partition("-")
            if a.isdigit() and (not sep or b.isdigit()):
                nums.extend(range(int(a), (int(b) if sep else int(a)) + 1))
            else:
                ok = False
        if not ok or not nums or any(not 1 <= n <= len(numbered) for n in nums):
            out("Not understood: %s" % line)
            continue
        for n in nums:
            off = state.sel.toggle(numbered[n - 1])
            if off:
                out("Switched off (same group): " + ", ".join(o.name for o in off))
        show()


def _use_curses():
    if os.environ.get("KOBOPATCHER_NO_TUI") == "1":
        return False
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    try:
        import curses  # noqa: F401
    except ImportError:
        return False
    return True


def run(selection, context="", actions=None):
    """Let the user choose; returns the action name or "quit"."""
    state = MenuState(selection, actions)
    if _use_curses():
        try:
            return _run_curses(state, context)
        except Exception as e:  # noqa: BLE001 - e.g. a terminal curses can't drive
            if "curses" not in type(e).__module__ and type(e).__name__ != "error":
                raise
            return _run_plain(state, context)
    return _run_plain(state, context)
