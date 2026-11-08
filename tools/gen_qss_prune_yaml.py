#!/usr/bin/env python3
"""Generate kobopatcher (kobopatch) YAML that prunes nickel's built-in stylesheets for one device.

nickel embeds its Qt stylesheets as Qt resources (rcc). Most per-view
stylesheets (`:/qss/<View>.qss`) are zlib-compressed, the rest are stored raw.
Many of their rules carry `[qApp_deviceIs<X>=true]` selectors for other Kobo
models, which can never match. This script removes exactly the selectors that
prune_qss.py removes, but as kobopatch instructions that edit nickel in place:

  * zlib stylesheets: FindZlibHash + ReplaceZlibGroup. Each replacement
    removes or rewrites one run of changed rules, anchored on the `}` of the
    rule before it, so every Find starts at a rule boundary.
  * raw stylesheets: FindReplaceString of the whole text with a same-length
    replacement (the pruned text padded with spaces), MustMatchLength: true.
    A shorter kobopatch ReplaceString would leave a NUL and the old tail.

Identical stylesheets stored more than once get one instruction pair per copy:
FindZlibHash / FindReplaceString always pick the first unpatched copy.

Every replacement is checked here before it is written:
  * applying the Finds in order (replace-all, like kobopatch) gives exactly
    the expected text, which has the same rules, selectors and declarations
    as prune_qss.py's output;
  * a zlib stylesheet's stored stream is exactly zlib.compress(text) (the
    condition for kobopatch's FindZlib sanity check) and the recompressed
    pruned text is not longer than the stored stream (ReplaceZlib's size rule);
  * a raw stylesheet occurs in nickel only at its resource payload(s).
Stylesheets that fail a check are reported and left out.

kobopatch does not rewrite the rcc 4-byte uncompressed-size prefix. Qt's
qUncompress() treats it as an allocation hint and keeps the real length, which
is what the existing ReplaceZlib patches that shrink stylesheets rely on.

Inputs:
  NICKEL       usr/local/Kobo/nickel from the firmware (read only).
  --qrc DIR    the tree written by notes/tools/rcc_extract.py (optional). With
               it, stylesheets are named and raw ones are included. Without
               it, nickel is scanned for zlib streams in rcc layout that hold
               a stylesheet; they are named by offset, raw ones are skipped.

Leave alone the stylesheets other patches edit, or those patches break:
  --exclude-used-by PATCHFILE...   e.g. kobopatcher's src/versions/<ver>/src/nickel/*.yaml
                                   (zlib sheets selected by FindZlib/FindZlibHash, raw
                                   sheets containing a FindReplaceString/ReplaceString Find)
  --exclude NAME,...               by file name, e.g. HomePageView.qss

Usage:
  gen_qss_prune_yaml.py NICKEL OUT.yaml [--qrc DIR] [--flags Dahlia,Dragon]
      [--drop-dark-mode] [--include-default] [--per-file] [--report OUT.tsv]
      [--exclude-used-by PATCHFILE...] [--exclude NAMES] [--firmware VERSION]

The output is a patch file for usr/local/Kobo/nickel (kobopatcher adds
Category/Experimental fields; --no-kobopatcher-fields omits them).

Run it with the same Python (classic zlib) as kobopatcher. Generate the YAML
anew for every firmware version: the hashes and texts are per build.
"""
import argparse
import hashlib
import json
import os
import re
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import prune_qss  # noqa: E402  (same directory)

COMMENT = re.compile(r'/\*.*?\*/', re.S)


# --- zlib --------------------------------------------------------------------

def check_classic_zlib():
    """kobopatch compares against classic zlib's compress2(level 6)."""
    if getattr(zlib, 'ZLIBNG_VERSION', None):
        sys.exit('error: Python uses zlib-ng %s; use a Python with classic zlib' % zlib.ZLIBNG_VERSION)
    if zlib.compress(b'a').hex() != '789c4b040000620062':
        sys.exit('error: zlib %s does not compress like classic zlib' % zlib.ZLIB_VERSION)


def zcompress(b):
    return zlib.compress(b, 6)


# --- locating stylesheets ----------------------------------------------------

class Sheet:
    def __init__(self, names, text, encoding, offsets):
        self.names = names          # qrc paths (or labels)
        self.text = text            # bytes
        self.encoding = encoding    # 'zlib' or 'raw'
        self.offsets = offsets      # zlib: stream offsets; raw: payload offsets
        self.status = 'ok'
        self.repl = []
        self.new = None
        self.stats = {}


def find_all(buf, needle):
    out, i = [], buf.find(needle)
    while i >= 0:
        out.append(i)
        i = buf.find(needle, i + 1)
    return out


def qrc_name(rel):
    """styles/qss/X.qss -> :/qss/X.qss; resources/default.qss -> :/default.qss."""
    parts = rel.split(os.sep)
    return ':/' + '/'.join(parts[1:]) if len(parts) > 1 else ':/' + rel


def sheets_from_qrc(buf, qrc):
    found = {}
    for root, _dirs, files in os.walk(qrc):
        for fn in sorted(files):
            if not fn.endswith('.qss'):
                continue
            path = os.path.join(root, fn)
            rel = os.path.relpath(path, qrc)
            if '.rcc.d' in rel:
                continue  # nested device bundles: none hold stylesheets on 4.38
            text = open(path, 'rb').read()
            name = qrc_name(rel)
            key = text
            if key in found:
                found[key].names.append(name)
                continue
            c = zcompress(text)
            zpre = struct.pack('>II', len(c) + 4, len(text))
            zoffs = [o + 8 for o in find_all(buf, zpre + c)]
            roffs = [o + 4 for o in find_all(buf, struct.pack('>I', len(text)) + text)]
            if zoffs:
                s = Sheet([name], text, 'zlib', zoffs)
            elif roffs:
                s = Sheet([name], text, 'raw', roffs)
            else:
                s = Sheet([name], text, '?', [])
                s.status = 'not found in nickel as raw or reproducible zlib (FindZlib would refuse)'
            found[key] = s
    return list(found.values())


def sheets_from_scan(buf):
    found = {}
    i = buf.find(b'\x78\x9c')
    while i >= 0:
        if i >= 8:
            stored, size = struct.unpack('>II', buf[i - 8:i])
            if 8 < stored < 1 << 24 and 0 < size < 1 << 24:
                d = zlib.decompressobj()
                try:
                    data = d.decompress(buf[i:i + stored - 4], size + 1)
                except zlib.error:
                    data = None
                if (data is not None and d.eof and len(data) == size
                        and len(buf[i:i + stored - 4]) - len(d.unused_data) == stored - 4
                        and b'{' in data and b'qApp_deviceIs' in data):
                    try:
                        data.decode('utf-8')
                    except UnicodeDecodeError:
                        data = None
                    if data is not None:
                        if data in found:
                            found[data].offsets.append(i)
                            found[data].names.append('zlib@0x%x' % i)
                        else:
                            s = Sheet(['zlib@0x%x' % i], data, 'zlib', [i])
                            if zcompress(data) != buf[i:i + stored - 4]:
                                s.status = 'stream does not recompress identically (FindZlib would refuse)'
                            found[data] = s
        i = buf.find(b'\x78\x9c', i + 1)
    return list(found.values())


# --- in-place pruning ----------------------------------------------------------

def parse_rules(t):
    """[(start, lb, rb)] of each rule in t; start = first selector char.

    Same rule model as prune_qss.split_rules (no nested braces), but in the
    coordinates of the original text, skipping comments."""
    rules, i, n, start = [], 0, len(t), None
    lb = None
    while i < n:
        if t.startswith('/*', i):
            j = t.find('*/', i + 2)
            if j < 0:
                raise ValueError('unterminated comment')
            i = j + 2
            continue
        ch = t[i]
        if lb is None:
            if ch == '{':
                if start is None:
                    raise ValueError('rule without selector at %d' % i)
                lb = i
            elif ch == '}':
                raise ValueError('unbalanced } at %d' % i)
            elif start is None and not ch.isspace():
                start = i
        elif ch == '}':
            rules.append((start, lb, i))
            start = lb = None
        i += 1
    if lb is not None or start is not None:
        raise ValueError('trailing text without rule')
    return rules


def plan(text, flags, drop_dark):
    """Return (replacements, new_text) for one stylesheet (str)."""
    rules = parse_rules(text)
    new_rule = []  # None = unchanged, '' = removed, str = rewritten rule
    for start, lb, rb in rules:
        sel = COMMENT.sub('', text[start:lb]).strip()
        sels = prune_qss.split_selectors(sel)
        keep = [s for s in sels if prune_qss.selector_can_match(s, flags, drop_dark)]
        if len(keep) == len(sels):
            new_rule.append(None)
        elif not keep:
            new_rule.append('')
        else:
            new_rule.append(',\n'.join(keep) + ' ' + text[lb:rb + 1])
    repl, out, pos = [], [], 0
    k = 0
    while k < len(rules):
        if new_rule[k] is None:
            k += 1
            continue
        # a run of changed rules k..m-1, anchored on the } before it (or text start)
        m = k
        while m < len(rules) and new_rule[m] is not None:
            m += 1
        a = rules[k - 1][2] if k > 0 else 0
        b = rules[m - 1][2] + 1
        rep = '}' if k > 0 else ''
        for r in range(k, m):
            if new_rule[r]:
                gap_from = rules[r - 1][2] + 1 if r > 0 else 0
                rep += text[gap_from:rules[r][0]] + new_rule[r]
        if k == 0 and not rep:
            # removing the leading rules: kobopatch refuses an empty Replace, so
            # extend the Find to the next (unchanged) rule and keep that rule;
            # its closing } stays outside the Find to anchor a following run
            if m < len(rules):
                b = rules[m][2]
                rep = text[rules[m][0]:b]
                m += 1
            else:
                rep = ' '  # every rule removed: a blank stylesheet
        repl.append((text[a:b], rep))
        out.append(text[pos:a])
        out.append(rep)
        pos = b
        k = m
    out.append(text[pos:])
    return repl, ''.join(out)


def apply_like_kobopatch(text, repl):
    for f, r in repl:
        if f not in text:
            raise ValueError('find not found: %r' % f[:60])
        text = text.replace(f, r)
    return text


def semantic(text):
    return [(tuple(prune_qss.split_selectors(s)), b) for s, b in prune_qss.split_rules(text)]


def count_dev(text):
    return len(re.findall(r'\[qApp_deviceIs', COMMENT.sub('', text)))


def minify_like_kobopatcher(b):
    """The fallbacks ReplaceZlib applies when the new stream is too long."""
    yield b
    b = b.replace(b'\n    ', b'\n').replace(b'\n  ', b'\n').replace(b'\n ', b'\n')
    yield b
    b = b.replace(b': ', b':').replace(b' {', b'{')
    yield b
    yield b.replace(b'\n', b'').replace(b'; ', b';').replace(b'{ ', b'{')


def process(s, buf, flags, drop_dark):
    t = s.text.decode('utf-8')
    rin = len(list(prune_qss.split_rules(t)))
    sin = sum(len(prune_qss.split_selectors(x)) for x, _ in prune_qss.split_rules(t))
    ref, st = prune_qss.prune(t, flags, drop_dark)
    s.stats = {'rules_in': st['rules_in'], 'rules_out': st['rules_out'], 'sel_in': sin,
               'sel_out': st['sel_out'], 'dev_in': count_dev(t), 'dev_out': count_dev(ref),
               'bytes_in': len(s.text), 'bytes_prune_qss': len(ref.encode())}
    assert rin == st['rules_in']
    if s.status != 'ok':
        return
    if st['sel_out'] == sin:
        s.status = 'nothing to prune'
        s.stats['bytes_out'] = len(s.text)
        return
    repl, new = plan(t, flags, drop_dark)
    if any(not f or not r for f, r in repl):
        s.status = 'empty Find or Replace (kobopatch refuses it); not emitted'
        return
    if apply_like_kobopatch(t, repl) != new:
        s.status = 'replacements interact; not emitted'
        return
    if semantic(new) != semantic(ref):
        s.status = 'result differs from prune_qss.py; not emitted'
        return
    nb = new.encode('utf-8')
    s.stats['bytes_out'] = len(nb)
    if s.encoding == 'zlib':
        old_c = len(zcompress(s.text))
        s.stats['stored_in'] = old_c
        for step, cand in enumerate(minify_like_kobopatcher(nb)):
            if len(zcompress(cand)) <= old_c:
                s.stats['stored_out'] = len(zcompress(cand))
                s.stats['minify_step'] = step
                break
        else:
            s.status = 'recompressed text longer than stored stream'
            return
        s.repl = repl
    else:
        if len(find_all(buf, s.text)) != len(s.offsets):
            s.status = 'raw text also occurs outside its resources; not emitted'
            return
        s.stats['stored_in'] = s.stats['stored_out'] = len(s.text)
        s.repl = [(t, new + ' ' * (len(s.text) - len(nb)))]
    s.new = new


# --- stylesheets other patches edit ----------------------------------------------

def _yaml_scalar(v):
    """The value of a one-line YAML scalar as used in patch files."""
    v = v.strip()
    m = re.match(r'"((?:[^"\\]|\\.)*)"', v)
    if m:
        return json.loads('"%s"' % m.group(1))
    m = re.match(r"'((?:[^']|'')*)'", v)
    if m:
        return m.group(1).replace("''", "'")
    return re.split(r"\s+#", v, maxsplit=1)[0].strip()


def used_by(patchfiles, sheets):
    """{id(sheet): 'file: patch'} for every sheet another patch file edits.

    A zlib sheet counts when a FindZlib/FindZlibHash selects it; a raw sheet
    when the Find text of a FindReplaceString/ReplaceString (or a
    FindBaseAddressString) occurs in it. Pruning such a sheet would break that
    patch: its hash no longer matches, or its Find texts for other models are
    gone."""
    ws = lambda x: re.sub(r'[ \t\r\n]', '', x)  # noqa: E731
    texts = [(s, s.text.decode('utf-8', 'replace')) for s in sheets]
    hits = {}
    for pf in patchfiles:
        patch = inst = None
        for line in open(pf, encoding='utf-8'):
            m = re.match(r'^([^\s#].*):\s*$', line)
            if m:
                patch = m.group(1)
                continue
            who = '%s: %s' % (os.path.basename(pf), patch)
            m = re.match(r'^\s*- ([A-Z]\w*):', line)
            if m and m.group(1) not in ('Find', 'Replace'):
                inst = m.group(1)
            m = re.match(r'^\s*- FindZlib(Hash)?:\s*(.+)$', line)
            if m:
                val = _yaml_scalar(m.group(2))
                for s, t in texts:
                    if s.encoding != 'zlib':
                        continue
                    if m.group(1):
                        ok = hashlib.sha1(s.text).hexdigest() == val
                    else:
                        ok = val in t or ws(val) in ws(t)
                    if ok:
                        hits.setdefault(id(s), who)
                continue
            if inst not in ('FindReplaceString', 'ReplaceString', 'FindBaseAddressString'):
                continue
            for m in re.finditer(r'(?:\bFind|FindBaseAddressString):\s*("(?:[^"\\]|\\.)*"|\'(?:[^\']|\'\')*\')', line):
                val = _yaml_scalar(m.group(1))
                for s, t in texts:
                    if s.encoding == 'raw' and val and val in t:
                        hits.setdefault(id(s), who)
    return hits


# --- YAML ----------------------------------------------------------------------

def q(s):
    """A YAML double-quoted scalar (JSON string syntax is valid YAML)."""
    return json.dumps(s, ensure_ascii=True)


def emit_instructions(s):
    lines = []
    label = ', '.join(s.names)
    for _copy in s.offsets:
        if s.encoding == 'zlib':
            lines.append('  - FindZlibHash: %s # %s' % (hashlib.sha1(s.text).hexdigest(), label))
            lines.append('  - ReplaceZlibGroup:')
            lines.append('      Replacements:')
            for f, r in s.repl:
                lines.append('      - {Find: %s, Replace: %s}' % (q(f), q(r)))
        else:
            f, r = s.repl[0]
            lines.append('  # %s (raw, %d bytes, pruned text padded with spaces)' % (label, len(s.text)))
            lines.append('  - FindReplaceString: {Find: %s, Replace: %s, MustMatchLength: true}' % (q(f), q(r)))
    return lines


def patch_block(title, desc, body, kp_fields):
    out = ['%s:' % title, '  - Enabled: no']
    if kp_fields:
        out += ['  - Category: "Performance (experimental)"', '  - Experimental: yes']
    out.append('  - Description: |')
    out += ['      ' + l if l else '' for l in desc.split('\n')]
    return out + body + ['']


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('nickel')
    ap.add_argument('outfile')
    ap.add_argument('--qrc', help='tree written by rcc_extract.py (names + raw stylesheets)')
    ap.add_argument('--flags', default='Dahlia,Dragon',
                    help='comma-separated qApp_deviceIs<X> flags that are true on the device')
    ap.add_argument('--drop-dark-mode', action='store_true', help='also drop [darkMode=true] selectors')
    ap.add_argument('--include-default', action='store_true',
                    help='also emit a patch that prunes :/default.qss in place')
    ap.add_argument('--per-file', action='store_true', help='one patch per stylesheet')
    ap.add_argument('--no-kobopatcher-fields', action='store_true',
                    help='omit Category/Experimental (plain kobopatch)')
    ap.add_argument('--firmware', default='', help='firmware version for the header comment')
    ap.add_argument('--report', help='write a TSV with one row per stylesheet')
    ap.add_argument('--exclude', default='', help='comma-separated stylesheet names to leave alone')
    ap.add_argument('--exclude-used-by', nargs='+', default=[], metavar='PATCHFILE',
                    help='leave alone the stylesheets that these patch files select with FindZlib/FindZlibHash')
    a = ap.parse_args()
    check_classic_zlib()
    flags = {f.strip() for f in a.flags.split(',') if f.strip()}
    buf = open(a.nickel, 'rb').read()
    sheets = sheets_from_qrc(buf, a.qrc) if a.qrc else sheets_from_scan(buf)
    excl = {x.strip() for x in a.exclude.split(',') if x.strip()}
    users = used_by(a.exclude_used_by, sheets)
    for s in sheets:
        if any(n.rsplit('/', 1)[-1] in excl for n in s.names):
            s.status = 'excluded (--exclude)'
        elif id(s) in users:
            s.status = 'excluded: edited by %s' % users[id(s)]
        try:
            process(s, buf, flags, a.drop_dark_mode)
        except ValueError as e:
            s.status = 'parse error: %s' % e
    is_default = lambda s: ':/default.qss' in s.names or len(s.text) > 50000  # noqa: E731
    per_view = [s for s in sheets if not is_default(s)]
    kp = not a.no_kobopatcher_fields
    flagtxt = ', '.join(sorted(flags)) + (' (and no dark mode)' if a.drop_dark_mode else '')
    out = ['# Generated by tools/gen_qss_prune_yaml.py from %s%s.' % (
        os.path.basename(a.nickel), (' (firmware %s)' % a.firmware) if a.firmware else ''),
        '# Removes stylesheet selectors that can only match other Kobo models; device flags: %s.' % flagtxt,
        '# Patch file for usr/local/Kobo/nickel. Regenerate for every firmware version.']
    for s in sorted(sheets, key=lambda s: s.names[0]):
        if s.status.startswith('excluded'):
            out.append('# Left alone: %s (%s)' % (', '.join(s.names), s.status))
    out.append('')
    groups = []
    for enc in ('zlib', 'raw'):
        ok = [s for s in per_view if s.encoding == enc and s.status == 'ok']
        if ok:
            groups.append((enc, ok))
    for enc, ok in groups:
        st = lambda k: sum(s.stats[k] * len(s.offsets) for s in ok)  # noqa: E731
        if a.per_file:
            for s in ok:
                title = 'Prune other models from %s' % s.names[0].replace(':/qss/', '')
                desc = ('Removes %d of %d selectors (%d of %d rules) that only match other models.\n'
                        '(firmware-performance-and-bugs.md section 7.4)') % (
                    s.stats['sel_in'] - s.stats['sel_out'], s.stats['sel_in'],
                    s.stats['rules_in'] - s.stats['rules_out'], s.stats['rules_in'])
                out += patch_block(title, desc, emit_instructions(s), kp)
        else:
            title = 'Prune other models from the %s per-view stylesheets' % ('compressed' if enc == 'zlib' else 'uncompressed')
            desc = ('Removes the selectors that can only match other Kobo models from %d %s :/qss/ stylesheets\n'
                    '(%d copies): rules %d -> %d, device selectors %d -> %d, text %d -> %d bytes.\n'
                    'Check every screen afterwards. (firmware-performance-and-bugs.md section 7.4)') % (
                len(ok), enc, sum(len(s.offsets) for s in ok), st('rules_in'), st('rules_out'),
                st('dev_in'), st('dev_out'), st('bytes_in'), st('bytes_out'))
            body = []
            for s in ok:
                body += emit_instructions(s)
            out += patch_block(title, desc, body, kp)
    if a.include_default:
        for s in sheets:
            if is_default(s) and s.status == 'ok' and s.encoding == 'zlib':
                desc = ('Prunes the global stylesheet inside nickel instead of loading /etc/kobo.qss:\n'
                        'rules %d -> %d, %d -> %d bytes. Not needed when the /etc/kobo.qss patch is on.') % (
                    s.stats['rules_in'], s.stats['rules_out'], s.stats['bytes_in'], s.stats['bytes_out'])
                out += patch_block('Prune other models from default.qss in place', desc, emit_instructions(s), kp)
    open(a.outfile, 'w', encoding='utf-8').write('\n'.join(out))
    if a.report:
        cols = ['rules_in', 'rules_out', 'sel_in', 'sel_out', 'dev_in', 'dev_out', 'bytes_in',
                'bytes_out', 'stored_in', 'stored_out', 'minify_step']
        with open(a.report, 'w', encoding='utf-8') as r:
            r.write('\t'.join(['names', 'encoding', 'copies', 'offsets', 'status', 'replacements'] + cols) + '\n')
            for s in sorted(sheets, key=lambda s: s.names[0]):
                r.write('\t'.join([','.join(s.names), s.encoding, str(len(s.offsets)),
                                   ','.join('0x%x' % o for o in s.offsets), s.status, str(len(s.repl))]
                                  + [str(s.stats.get(c, '')) for c in cols]) + '\n')
    tot = {}
    for s in per_view:
        tot.setdefault((s.encoding, s.status), [0, 0])
        tot[(s.encoding, s.status)][0] += 1
        tot[(s.encoding, s.status)][1] += len(s.offsets)
    for (enc, status), (n, c) in sorted(tot.items()):
        print('%-4s %-60s %4d stylesheets, %4d copies' % (enc, status, n, c), file=sys.stderr)


if __name__ == '__main__':
    main()
