"""kobopatch.yaml: the configuration of a patch run."""

import json

from . import yamlnodes as Y
from .compat import go_compat
from .errors import KobopatcherError


class ConfigError(KobopatcherError):
    pass


class Config:
    """The configuration (all paths are relative to the config file)."""

    def __init__(self):
        self.version = ""  # firmware version the patches are for (informational)
        self.in_ = ""  # firmware update zip (or a kobopatch-testdata .tar.xz)
        self.out = ""  # output KoboRoot.tgz
        self.log = ""  # log file
        self.patch_format = ""  # deprecated, ignored (the format comes from the extension)
        self.patches = {}  # patch file -> path of the binary inside KoboRoot.tgz
        self.overrides = {}  # patch file -> {patch name: enabled}
        self.lrelease = ""  # path to Qt's lrelease (default: found in PATH)
        self.translations = {}  # .ts file -> path of the .qm inside KoboRoot.tgz
        self.symlinks = {}  # symlink target -> symlink path (inside KoboRoot.tgz)
        self.files = {}  # local file -> path(s) inside KoboRoot.tgz

    def to_json(self) -> str:
        """The config as kobopatch logged it (Go's json.MarshalIndent)."""

        def m(d):
            return None if not d else dict(sorted(d.items()))

        v = {
            "Version": self.version,
            "In": self.in_,
            "Out": self.out,
            "Log": self.log,
            "PatchFormat": self.patch_format,
            "Patches": m(self.patches),
            "Overrides": None if not self.overrides else {k: m(v) for k, v in sorted(self.overrides.items())},
            "Lrelease": self.lrelease,
            "Translations": m(self.translations),
            "Symlinks": m(self.symlinks),
            "Files": m({k: [d.dest for d in v] for k, v in self.files.items()}),
        }
        return json.dumps(v, indent=4, ensure_ascii=False)


class FileDest:
    """Where an extra file goes in KoboRoot.tgz, and when."""

    __slots__ = ("dest", "cond", "mode")

    def __init__(self, dest, cond=None, mode=0o777):
        self.dest, self.cond, self.mode = dest, cond, mode

    def __repr__(self):
        return "FileDest(%r, cond=%r, mode=%o)" % (self.dest, self.cond, self.mode)


_FILE_RULE = Y.Struct(
    "main.fileDest",
    [("dest", "dest", Y.String()), ("if", "cond", Y.Ptr(Y.String())), ("mode", "mode", Y.Ptr(Y.Int(32)))],
)


def _string_slice(node):
    """files: values. A path, a list of paths, or (kobopatcher extension) a
    mapping {dest, if, mode} or a list mixing paths and such mappings."""
    from yaml.nodes import MappingNode, SequenceNode

    def one(n):
        if isinstance(n, MappingNode):
            r = Y.decode_strict(n, _FILE_RULE)
            if not r["dest"]:
                raise Y.YAMLError("line %d: files: dest must be set" % Y.line(n))
            return FileDest(r["dest"], r["cond"], 0o777 if r["mode"] is None else r["mode"])
        return FileDest(Y.decode_strict(n, Y.String()))

    if isinstance(node, SequenceNode):
        return [one(n) for n in node.value]
    return [one(node)]


_CONFIG = Y.Struct(
    "main.Config",
    [
        ("version", "version", Y.String()),
        ("in", "in_", Y.String()),
        ("out", "out", Y.String()),
        ("log", "log", Y.String()),
        ("patchFormat", "patch_format", Y.String()),
        ("patches", "patches", Y.Map(Y.String(), Y.String())),
        ("overrides", "overrides", Y.Map(Y.String(), Y.Map(Y.String(), Y.Bool()))),
        ("lrelease", "lrelease", Y.String()),
        ("translations", "translations", Y.Map(Y.String(), Y.String())),
        ("symlinks", "symlinks", Y.Map(Y.String(), Y.String())),
        ("files", "files", Y.Map(Y.String(), Y.Custom("main.stringSlice", _string_slice))),
    ],
    Config,
)


def load(buf: bytes) -> Config:
    """Parse and check a kobopatch.yaml."""
    from .patchfile import get_formats

    try:
        node = Y.parse(buf)
        if node is None:
            raise Y.YAMLError("EOF")
        cfg = Y.decode_strict(node, Y.Ptr(_CONFIG))
    except (Y.YAMLError, UnicodeDecodeError) as e:
        raise ConfigError("error reading kobopatch.yaml: %s" % e) from None
    if cfg is None:
        cfg = Config()
    for attr in ("patches", "overrides", "translations", "symlinks", "files"):
        if getattr(cfg, attr) is None:
            setattr(cfg, attr, {})
    cfg.overrides = {k: (v or {}) for k, v in cfg.overrides.items()}
    if not (cfg.version and cfg.in_ and cfg.out and cfg.log):
        raise ConfigError("invalid kobopatch.yaml: version, in, out, and log are required")
    # patchFormat is deprecated (the format comes from each file's extension);
    # kobopatch still insisted on a valid value
    formats = get_formats()
    if (go_compat() or cfg.patch_format) and cfg.patch_format not in formats:
        raise ConfigError(
            "invalid patch format '%s', expected one of %s" % (cfg.patch_format, ", ".join(sorted(formats)))
        )
    return cfg
