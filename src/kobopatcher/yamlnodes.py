"""Strict, typed YAML decoding with kobopatch's rules.

kobopatch decoded its files with a 2019 fork of Go's gopkg.in/yaml.v3
(github.com/pgaskin/yaml "v3-node-decodestrict"). This module reproduces the
parts of its decoder that kobopatch relies on, on top of PyYAML's parser:

* plain scalars resolve like yaml.v3: true/false/null in their three
  spellings, integers with Go's ParseInt(s, 0, 64) (so 0x.., 0o.., 0b.. and
  C-style octal 010), floats, timestamps; everything else is a string;
* quoted and block scalars are always strings;
* typed bools also accept y/yes/on/n/no/off (and their capitalisations),
  even when quoted;
* duplicate mapping keys, duplicate struct fields and unknown struct fields
  are errors;
* type errors are collected and reported together as
  "yaml: unmarshal errors:\\n  line N: ..." with Go's type names.

Syntax errors come from PyYAML and are worded differently from kobopatch's.
"""

import datetime
import math
import re

import yaml
from yaml.composer import Composer
from yaml.constructor import BaseConstructor
from yaml.nodes import MappingNode, ScalarNode, SequenceNode
from yaml.parser import Parser
from yaml.reader import Reader
from yaml.resolver import BaseResolver
from yaml.scanner import Scanner

from .gofmt import quote

NULL, BOOL, STR, INT, FLOAT, TIMESTAMP, SEQ, MAP, BINARY, MERGE = (
    "!!null",
    "!!bool",
    "!!str",
    "!!int",
    "!!float",
    "!!timestamp",
    "!!seq",
    "!!map",
    "!!binary",
    "!!merge",
)
_LONG = "tag:yaml.org,2002:"


class YAMLError(Exception):
    """A decoding error (yaml.v3's returned error)."""


class _Fail(Exception):
    """yaml.v3's fail()/failf(): aborts decoding with this error."""

    def __init__(self, err):
        super().__init__(str(err))
        self.err = err


# --- parsing ------------------------------------------------------------------


class _Composer(Composer):
    def compose_scalar_node(self, anchor):
        event = self.get_event()
        node = ScalarNode(event.tag or "", event.value, event.start_mark, event.end_mark, style=event.style)
        node.explicit_tag = event.tag  # None, "!" or an explicit tag
        if anchor is not None:
            self.anchors[anchor] = node
        return node

    def compose_sequence_node(self, anchor):
        tag = self.peek_event().tag
        node = super().compose_sequence_node(anchor)
        node.explicit_tag = tag
        return node

    def compose_mapping_node(self, anchor):
        tag = self.peek_event().tag
        node = super().compose_mapping_node(anchor)
        node.explicit_tag = tag
        return node


class _Resolver(BaseResolver):
    pass


# no implicit resolvers: we resolve scalars ourselves
_Resolver.yaml_implicit_resolvers = {}


class _Loader(Reader, Scanner, Parser, _Composer, BaseConstructor, _Resolver):
    def __init__(self, stream):
        Reader.__init__(self, stream)
        Scanner.__init__(self)
        Parser.__init__(self)
        _Composer.__init__(self)
        BaseConstructor.__init__(self)
        _Resolver.__init__(self)


def parse(buf):
    """Parse a YAML document into a node tree (None for an empty document)."""
    if isinstance(buf, (bytes, bytearray)):
        buf = bytes(buf).decode("utf-8")
    loader = _Loader(buf)
    try:
        try:
            return loader.get_single_node()
        finally:
            loader.dispose()
    except yaml.YAMLError as e:
        raise YAMLError("yaml: %s" % _pyyaml_msg(e)) from None


def _pyyaml_msg(e) -> str:
    mark = getattr(e, "problem_mark", None)
    problem = getattr(e, "problem", None) or str(e)
    if mark is not None:
        return "line %d: %s" % (mark.line + 1, problem)
    return problem


def line(node) -> int:
    return node.start_mark.line + 1


# --- tags and scalar resolution --------------------------------------------


def short_tag(tag: str) -> str:
    if tag.startswith(_LONG):
        return "!!" + tag[len(_LONG) :]
    return tag


def node_tag(node) -> str:
    """The node's tag as yaml.v3 would report it (ShortTag())."""
    t = getattr(node, "explicit_tag", None)
    if t and t != "!":
        return short_tag(t)
    if isinstance(node, MappingNode):
        return MAP
    if isinstance(node, SequenceNode):
        return SEQ
    if indicated_string(node):
        return STR
    return resolve("", node.value)[0]


def indicated_string(node) -> bool:
    if not isinstance(node, ScalarNode):
        return False
    t = getattr(node, "explicit_tag", None)
    if t and t != "!":
        return short_tag(t) == STR
    return node.style in ("'", '"', "|", ">")


_RESOLVE_MAP = {}
for _v, _t, _l in [
    (True, BOOL, ["true", "True", "TRUE"]),
    (False, BOOL, ["false", "False", "FALSE"]),
    (None, NULL, ["", "~", "null", "Null", "NULL"]),
    (math.nan, FLOAT, [".nan", ".NaN", ".NAN"]),
    (math.inf, FLOAT, [".inf", ".Inf", ".INF", "+.inf", "+.Inf", "+.INF"]),
    (-math.inf, FLOAT, ["-.inf", "-.Inf", "-.INF"]),
    ("<<", MERGE, ["<<"]),
]:
    for _s in _l:
        _RESOLVE_MAP[_s] = (_t, _v)

_YAML_FLOAT = re.compile(r"^[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?$")
_RESOLVABLE = ("", STR, BOOL, INT, FLOAT, NULL, TIMESTAMP)


def go_parse_int(s: str, base: int, bits: int = 64, unsigned: bool = False):
    """strconv.ParseInt / ParseUint. Returns None on any error."""
    if s == "":
        return None
    neg = False
    if not unsigned and s[0] in "+-":
        neg = s[0] == "-"
        s = s[1:]
    if base == 0:
        base = 10
        if s.startswith("0"):
            if len(s) >= 3 and s[1] in "xX":
                base, s = 16, s[2:]
            elif len(s) >= 3 and s[1] in "oO":
                base, s = 8, s[2:]
            elif len(s) >= 3 and s[1] in "bB":
                base, s = 2, s[2:]
            else:
                base, s = 8, s[1:]
                if s == "":
                    return 0
        # (underscores were removed by the caller)
    if s == "" or not all(_digit(c) < base for c in s):
        return None
    v = int(s, base)
    if unsigned:
        return v if v < (1 << bits) else None
    if neg:
        v = -v
        return v if v >= -(1 << (bits - 1)) else None
    return v if v < (1 << (bits - 1)) else None


def _digit(c: str) -> int:
    if "0" <= c <= "9":
        return ord(c) - 48
    if "a" <= c.lower() <= "z":
        return ord(c.lower()) - 87
    return 99


_GO_FLOAT = re.compile(
    r"^[+-]?(?:(?:[0-9](?:_?[0-9])*)?\.?(?:[0-9](?:_?[0-9])*)?(?:[eE][+-]?[0-9](?:_?[0-9])*)?|inf(?:inity)?|nan)$",
    re.IGNORECASE,
)


def go_parse_float(s: str):
    """strconv.ParseFloat(s, 64) for decimal input. Returns None on error."""
    if not s or not _GO_FLOAT.match(s) or not any(c.isdigit() for c in s) and s.lstrip("+-").lower() not in ("inf", "infinity", "nan"):
        return None
    if "_" in s:
        return None  # only allowed with a base prefix
    try:
        v = float(s)
    except ValueError:
        return None
    if math.isinf(v) and s.lstrip("+-").lower() not in ("inf", "infinity"):
        return None  # out of range
    return v


def _parse_timestamp(s: str):
    i = 0
    while i < len(s) and s[i].isdigit():
        i += 1
    if i != 4 or i == len(s) or s[i] != "-":
        return None
    for rx in (
        r"^\d{4}-\d{1,2}-\d{1,2}[Tt]\d{1,2}:\d{1,2}:\d{1,2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$",
        r"^\d{4}-\d{1,2}-\d{1,2} \d{1,2}:\d{1,2}:\d{1,2}(\.\d+)?$",
        r"^\d{4}-\d{1,2}-\d{1,2}$",
    ):
        if re.match(rx, s):
            try:
                y, m, d = (int(x) for x in re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s).groups())
                return datetime.date(y, m, d)
            except ValueError:
                return None
    return None


def resolve(tag: str, value: str):
    """yaml.v3 resolve(): returns (tag, value)."""
    tag = short_tag(tag)
    if tag not in _RESOLVABLE:
        return tag, value
    rtag, out = _resolve(tag, value)
    if tag in ("", rtag, STR, BINARY):
        return rtag, out
    if tag == FLOAT and rtag == INT:
        return FLOAT, float(out)
    raise _Fail(YAMLError("yaml: cannot decode %s `%s` as a %s" % (rtag, value, tag)))


def _resolve(tag, s):
    hint = "N" if s == "" else _hint(s[0])
    if hint and tag not in (STR, BINARY):
        if s in _RESOLVE_MAP:
            return _RESOLVE_MAP[s]
        if hint == ".":
            v = go_parse_float(s)
            if v is not None:
                return FLOAT, v
        elif hint in "DS":
            if tag in ("", TIMESTAMP):
                ts = _parse_timestamp(s)
                if ts is not None:
                    return TIMESTAMP, ts
            plain = s.replace("_", "")
            v = go_parse_int(plain, 0)
            if v is not None:
                return INT, v
            v = go_parse_int(plain, 0, unsigned=True)
            if v is not None:
                return INT, v
            if _YAML_FLOAT.match(plain):
                v = go_parse_float(plain)
                if v is not None:
                    return FLOAT, v
            for pfx, base in (("0b", 2), ("0o", 8)):
                if plain.startswith(pfx):
                    v = go_parse_int(plain[2:], base)
                    if v is None:
                        v = go_parse_int(plain[2:], base, unsigned=True)
                    if v is not None:
                        return INT, v
                elif plain.startswith("-" + pfx):
                    v = go_parse_int("-" + plain[3:], base)
                    if v is not None:
                        return INT, v
    return STR, s


def _hint(c):
    if c in "+-":
        return "S"
    if c.isdigit() and c.isascii():
        return "D"
    if c in "yYnNtTfFoO~":
        return "M"
    if c == ".":
        return "."
    return ""


# --- types ------------------------------------------------------------------


class T:
    """A Go type the decoder can decode into."""

    go_name = "?"
    kind = "?"

    def zero(self):
        return None


class String(T):
    kind = "string"

    def __init__(self, go_name="string"):
        self.go_name = go_name

    def zero(self):
        return ""


class Bool(T):
    kind = "bool"

    def __init__(self, go_name="bool"):
        self.go_name = go_name

    def zero(self):
        return False


class Int(T):
    kind = "int"

    def __init__(self, bits, unsigned=False, go_name=None):
        self.bits, self.unsigned = bits, unsigned
        self.go_name = go_name or ("uint%d" % bits if unsigned else "int%d" % bits)

    def zero(self):
        return 0

    def fits(self, v):
        if self.unsigned:
            return 0 <= v < (1 << self.bits)
        return -(1 << (self.bits - 1)) <= v < (1 << (self.bits - 1))


class Float(T):
    kind = "float"
    go_name = "float64"

    def zero(self):
        return 0.0


class Slice(T):
    kind = "slice"

    def __init__(self, elem, go_name=None):
        self.elem = elem
        self.go_name = go_name or ("[]" + elem.go_name)


class Map(T):
    kind = "map"

    def __init__(self, key, val, go_name=None):
        self.key, self.val = key, val
        self.go_name = go_name or "map[%s]%s" % (key.go_name, val.go_name)


class Ptr(T):
    kind = "ptr"

    def __init__(self, elem):
        self.elem = elem
        self.go_name = "*" + elem.go_name


class Struct(T):
    """fields: list of (yaml name, attribute name, type)."""

    kind = "struct"

    def __init__(self, go_name, fields, factory=dict):
        self.go_name, self.fields, self.factory = go_name, fields, factory

    def zero(self):
        obj = self.factory()
        for _, attr, t in self.fields:
            _set(obj, attr, t.zero())
        return obj


class Node(T):
    """yaml.Node: keep the node as is."""

    kind = "node"
    go_name = "yaml.Node"


class Custom(T):
    """A type with an UnmarshalYAML(node) method: fn(node) -> value, raising
    YAMLError to fail."""

    kind = "custom"

    def __init__(self, go_name, fn, zero=None):
        self.go_name, self.fn, self._zero = go_name, fn, zero

    def zero(self):
        return self._zero() if self._zero else None


def _set(obj, attr, v):
    if isinstance(obj, dict):
        obj[attr] = v
    else:
        setattr(obj, attr, v)


# --- decoder -------------------------------------------------------------------


class Decoder:
    def __init__(self, unique_keys=True, known_fields=True):
        self.unique_keys = unique_keys
        self.known_fields = known_fields
        self.terrors = []
        self._aliases = set()

    def terror(self, node, tag, t):
        if getattr(node, "explicit_tag", None) and node.explicit_tag != "!":
            tag = short_tag(node.explicit_tag)
        value = ""
        if tag not in (SEQ, MAP):
            v = node.value if isinstance(node, ScalarNode) else ""
            value = " `" + v[:7] + "...`" if len(v) > 10 else " `" + v + "`"
        self.terrors.append("line %d: cannot unmarshal %s%s into %s" % (line(node), tag, value, t.go_name))

    def decode(self, node, t):
        """Returns (good, value)."""
        if isinstance(t, Node):
            return True, node
        # null: pointers, maps and slices become nil; everything else is left
        # unchanged (no error)
        if isinstance(node, ScalarNode) and node_tag(node) == NULL:
            if t.kind in ("ptr", "map", "slice", "custom"):
                return True, None
            return False, t.zero()
        if t.kind == "ptr":
            good, v = self.decode(node, t.elem)
            return good, v
        if t.kind == "custom":
            try:
                return True, t.fn(node)
            except YAMLError as e:
                raise _Fail(e) from None
        if isinstance(node, ScalarNode):
            return self._scalar(node, t)
        if isinstance(node, SequenceNode):
            return self._sequence(node, t)
        return self._mapping(node, t)

    def _scalar(self, node, t):
        if indicated_string(node):
            tag, resolved = STR, node.value
        else:
            tag, resolved = resolve(getattr(node, "explicit_tag", None) or "", node.value)
            if tag == BINARY:
                import base64

                try:
                    resolved = base64.b64decode(resolved, validate=True).decode("latin-1")
                except Exception:  # noqa: BLE001
                    raise _Fail(YAMLError("yaml: !!binary value contains invalid base64 data")) from None
        k = t.kind
        if k == "string":
            return True, (resolved if tag == BINARY else node.value)
        if k == "int":
            if isinstance(resolved, bool):
                pass
            elif isinstance(resolved, int):
                if t.fits(resolved):
                    return True, resolved
            elif isinstance(resolved, float) and tag == FLOAT:
                if not math.isnan(resolved) and not math.isinf(resolved):
                    iv = int(resolved)
                    if (t.unsigned or resolved <= 2**63 - 1) and t.fits(iv):
                        return True, iv
        elif k == "bool":
            if isinstance(resolved, bool):
                return True, resolved
            if isinstance(resolved, str):
                if resolved in ("y", "Y", "yes", "Yes", "YES", "on", "On", "ON"):
                    return True, True
                if resolved in ("n", "N", "no", "No", "NO", "off", "Off", "OFF"):
                    return True, False
        elif k == "float":
            if isinstance(resolved, (int, float)) and not isinstance(resolved, bool):
                return True, float(resolved)
        self.terror(node, tag, t)
        return False, t.zero()

    def _sequence(self, node, t):
        if t.kind != "slice":
            self.terror(node, SEQ, t)
            return False, t.zero()
        out = []
        for item in node.value:
            good, v = self.decode(item, t.elem)
            if good:
                out.append(v)
        return True, out

    def _check_unique(self, node):
        if not self.unique_keys:
            return True
        n0 = len(self.terrors)
        items = node.value
        for i, (ki, _) in enumerate(items):
            for kj, _ in items[i + 1 :]:
                if type(ki) is type(kj) and getattr(ki, "value", None) == getattr(kj, "value", None) and isinstance(ki, ScalarNode):
                    self.terrors.append(
                        "line %d: mapping key %s already defined at line %d" % (line(kj), quote(kj.value), line(ki))
                    )
        return len(self.terrors) == n0

    def _merge_items(self, node):
        """Expand `<<` merge keys (rarely used in patch files)."""
        out = []
        for k, v in node.value:
            if isinstance(k, ScalarNode) and k.value == "<<" and not indicated_string(k):
                srcs = v.value if isinstance(v, SequenceNode) else [v]
                if not all(isinstance(s, MappingNode) for s in srcs):
                    raise _Fail(YAMLError("yaml: map merge requires map or sequence of maps as the value"))
                for s in reversed(srcs) if isinstance(v, SequenceNode) else srcs:
                    out.extend(self._merge_items(s))
                continue
            out.append((k, v))
        return out

    def _mapping(self, node, t):
        if not self._check_unique(node):
            return False, t.zero()
        if t.kind == "struct":
            return self._struct(node, t)
        if t.kind != "map":
            self.terror(node, MAP, t)
            return False, t.zero()
        out = {}
        for k, v in self._merge_items(node):
            gk, kv = self.decode(k, t.key)
            if not gk:
                continue
            if isinstance(kv, (list, dict)):
                raise _Fail(YAMLError("yaml: invalid map key: %r" % (kv,)))
            gv, vv = self.decode(v, t.val)
            if gv:
                out[kv] = vv
        return True, out

    def _struct(self, node, t):
        obj = t.zero()
        byname = {f[0]: f for f in t.fields}
        done = set()
        for k, v in self._merge_items(node):
            gk, name = self.decode(k, String())
            if not gk:
                continue
            f = byname.get(name)
            if f is not None:
                if self.unique_keys:
                    if name in done:
                        self.terrors.append("line %d: field %s already set in type %s" % (line(k), name, t.go_name))
                        continue
                    done.add(name)
                good, val = self.decode(v, f[2])
                if good or f[2].kind in ("ptr", "map", "slice", "custom"):
                    _set(obj, f[1], val)
            elif self.known_fields:
                self.terrors.append("line %d: field %s not found in type %s" % (line(k), name, t.go_name))
        return True, obj


def decode_strict(node, t):
    """node.DecodeStrict(&v): returns the value or raises YAMLError."""
    d = Decoder()
    try:
        _, v = d.decode(node, t)
    except _Fail as f:
        raise f.err from None
    if d.terrors:
        raise YAMLError("yaml: unmarshal errors:\n  " + "\n  ".join(d.terrors))
    return v


def unmarshal(buf, t):
    """yaml.Unmarshal(buf, &v) with unique keys (kobopatch's patch files)."""
    node = parse(buf)
    if node is None:
        return t.zero()
    return decode_strict(node, t)
