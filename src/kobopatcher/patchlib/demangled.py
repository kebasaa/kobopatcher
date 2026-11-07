"""Cached C++ demangling (the symbol tables are demangled repeatedly)."""

_cache = {}


def demangle(name: str):
    """The demangled name, or None if it can't be demangled."""
    try:
        return _cache[name]
    except KeyError:
        pass
    from ._demangle import DemangleError, to_string

    try:
        v = to_string(name)
    except DemangleError:
        v = None
    if len(_cache) > 2_000_000:
        _cache.clear()
    _cache[name] = v
    return v
