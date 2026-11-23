"""Rewrite the member headers of the static archives of a build root as ar's deterministic mode, its D modifier, writes
them: time, owner and group 0, mode 644. The same objects then make the same archive, whatever the time and the user
of the build.

Run by rpmbuild at the end of %__spec_install_post, after brp-strip-static-archive, whose strip -g writes the headers
again with the time and the user of the build: ``archives.py <build root>``. It edits the headers itself, as
objcopy -D and strip -D fail on the archives of objects that are not ELF, like those of dev86.
"""

import os
import sys
from pathlib import Path

MAGIC = b"!<arch>\n"
HEADER = 60
"""The size of the header of a member: its name, time, owner, group, mode and size, padded with spaces, and a magic."""


def deterministic(data: bytes) -> bytes:
    """Return the archive `data` with the headers of its members as ar D writes them.

    Raise ValueError when a header is not one of an ar archive.
    """
    out, pos = bytearray(data), len(MAGIC)
    while pos < len(data):
        header = data[pos : pos + HEADER]
        if len(header) < HEADER or header[58:] != b"`\n":
            msg = f"no header of a member at {pos}"
            raise ValueError(msg)
        name = header[:16].rstrip()
        if name in (b"/", b"/SYM64/"):  # the symbol table: owner, group and mode already 0
            out[pos + 16 : pos + 28] = b"0".ljust(12)
        elif name != b"//":  # but the table of the long names, without time, owner, group nor mode
            out[pos + 16 : pos + 48] = b"0".ljust(12) + b"0".ljust(6) + b"0".ljust(6) + b"644".ljust(8)
        size = int(header[48:58])
        pos += HEADER + size + size % 2
    return bytes(out)


def main() -> None:
    """Rewrite the archives of the build root of the command line."""
    for directory, _, names in Path(sys.argv[1]).walk():
        for path in (directory / name for name in names):
            if path.is_symlink() or not path.is_file():
                continue
            with path.open("rb") as f:
                if f.read(len(MAGIC)) != MAGIC:
                    continue
                data = MAGIC + f.read()
            try:
                new = deterministic(data)
            except ValueError as e:
                sys.stderr.write(f"warning: {path} not made deterministic: {e}\n")
                continue
            if new == data:
                continue
            # in place, for its hard links, even when read-only, with its times
            st = path.stat()
            path.chmod(st.st_mode | 0o200)
            with path.open("r+b") as f:
                f.write(new)
            path.chmod(st.st_mode)
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


if __name__ == "__main__":
    main()
