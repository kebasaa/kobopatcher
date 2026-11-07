"""kobopatcher-cssextract: extract the zlib-compressed CSS streams of a binary.

Writes cssextract.out.css in the current directory, each stream preceded by a
comment with its offset, length and SHA1 (for FindZlib / FindZlibHash).
"""

import hashlib
import sys

from .patchlib.patcher import Patcher


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        sys.stderr.write("cssextract extracts zlib-compressed from a binary file\n")
        sys.stderr.write("Usage: kobopatcher-cssextract BINARY_FILE\n")
        return 1
    try:
        with open(argv[0], "rb") as f:
            buf = f.read()
        z = Patcher(buf).extract_zlib()
    except Exception as e:  # noqa: BLE001
        sys.stderr.write("Error: %s\n" % e)
        return 1
    with open("cssextract.out.css", "wb") as f:
        for zi in z:
            css = zi.css.encode("utf-8")
            f.write(
                b"/* zlib stream: offset_hex(0x%X) offset_int32(%d) len_int32(%d) sha1(%s) */\n%s\n\n"
                % (zi.offset, zi.offset, len(css), hashlib.sha1(css).hexdigest().encode(), css)
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
