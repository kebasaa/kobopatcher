#!/usr/bin/env python3
"""Prune a Kobo nickel Qt stylesheet (QSS) for one device.

Drops every selector that can never match on the target device:
  * [qApp_deviceIs<X>=true]  where X is not one of the device's flags
  * [qApp_deviceIs<X>=false] where X is one of the device's flags
  * optionally [darkMode=true] (dark mode never used)
A rule whose selector list becomes empty is removed entirely. Comments are
stripped. Everything else is kept verbatim, so the result is safe to diff.

Usage:
  prune_qss.py IN.qss OUT.qss [--flags Dahlia,Dragon] [--drop-dark-mode]

Device flags for the Kobo Aura H2O (N250, codename "dahlia", base "dragon",
see docs/firmware-performance-and-bugs.md): Dahlia, Dragon.
"""
import argparse
import re
import sys

ATTR = re.compile(r'\[(qApp_deviceIs[A-Za-z0-9]+|darkMode)\s*=\s*"?(true|false)"?\]')


def split_rules(text):
    """Yield (selector_text, body_text) pairs; QSS has no nested braces."""
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    pos = 0
    while True:
        lb = text.find('{', pos)
        if lb < 0:
            if text[pos:].strip():
                raise ValueError('trailing text without rule: %r' % text[pos:pos + 80])
            return
        rb = text.find('}', lb)
        if rb < 0:
            raise ValueError('unbalanced brace near offset %d' % lb)
        yield text[pos:lb].strip(), text[lb + 1:rb].strip()
        pos = rb + 1


def split_selectors(sel):
    """Split on top-level commas (commas inside [] or () are kept)."""
    out, depth, cur = [], 0, ''
    for ch in sel:
        if ch in '[(':
            depth += 1
        elif ch in '])':
            depth -= 1
        if ch == ',' and depth == 0:
            out.append(cur.strip())
            cur = ''
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def selector_can_match(sel, flags, drop_dark):
    for name, val in ATTR.findall(sel):
        want = val == 'true'
        if name == 'darkMode':
            if drop_dark and want:
                return False
            continue
        dev = name[len('qApp_deviceIs'):]
        if (dev in flags) != want:
            return False
    return True


def prune(text, flags, drop_dark):
    out, stats = [], {'rules_in': 0, 'rules_out': 0, 'sel_in': 0, 'sel_out': 0}
    for sel, body in split_rules(text):
        stats['rules_in'] += 1
        sels = split_selectors(sel)
        stats['sel_in'] += len(sels)
        keep = [s for s in sels if selector_can_match(s, flags, drop_dark)]
        if keep:
            stats['rules_out'] += 1
            stats['sel_out'] += len(keep)
            out.append(',\n'.join(keep) + ' {\n' + body + '\n}\n')
    return ''.join(out), stats


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('infile')
    ap.add_argument('outfile')
    ap.add_argument('--flags', default='Dahlia,Dragon',
                    help='comma-separated qApp_deviceIs<X> flags that are true on the device')
    ap.add_argument('--drop-dark-mode', action='store_true',
                    help='also drop [darkMode=true] selectors')
    a = ap.parse_args()
    flags = {f.strip() for f in a.flags.split(',') if f.strip()}
    text = open(a.infile, encoding='utf-8').read()
    pruned, st = prune(text, flags, a.drop_dark_mode)
    open(a.outfile, 'w', encoding='utf-8').write(pruned)
    print('rules %(rules_in)d -> %(rules_out)d, selectors %(sel_in)d -> %(sel_out)d' % st, file=sys.stderr)


if __name__ == '__main__':
    main()
