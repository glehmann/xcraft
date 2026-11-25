"""Synchronize the directories of the sources of a package developed from a tree with its archive, before rpmbuild.

Run in the build environment, out of the root, with the rpm python API, before each rpmbuild of a build tree:
``develop.py <spec> <target>``, the target like ``aarch64-linux``. The directories are those of the %setup of the %prep
xcraft.develop rewrote, of the branches of its %if taken, which its MARK lines name, with the archive: nothing to do for
a spec without them, not developed from a tree. The files of the archive a directory has with other contents, or other
modes, or misses, are written; the others keep their times, so that the build in the directory, kept from the previous
one, makes only what changed, like a build in the tree. The files of the previous archive it does not have anymore are
removed, as listed next to the directory: what the builds made stays.
"""

import json
import os
import shutil
import sys
import tarfile
from pathlib import Path

import rpm  # ty: ignore[unresolved-import]  # pyright: ignore[reportMissingImports]

MARK = ": xcraft develop "
"""The start of the line of %prep naming the archive and the directory to synchronize with it, see xcraft.develop."""


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.is_symlink() or path.exists():
        path.unlink()


def _synced(archive: tarfile.TarFile, member: tarfile.TarInfo, path: Path) -> bool:
    """Make `path` the file or link `member` of `archive`, unless it is already, and return whether it was not."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if member.issym():
        if path.is_symlink() and path.readlink() == Path(member.linkname):
            return False
        _remove(path)
        path.symlink_to(member.linkname)
        return True
    if not (content := archive.extractfile(member)):
        return False
    data, mode = content.read(), 0o755 if member.mode & 0o100 else 0o644
    written = path.is_symlink() or not path.is_file() or path.stat().st_size != len(data) or path.read_bytes() != data
    if written:
        _remove(path)
        path.write_bytes(data)
    if path.stat().st_mode & 0o777 != mode:
        path.chmod(mode)
    return written


def _sync(archive_path: str) -> None:
    """Synchronize the current directory with the archive `archive_path`."""
    listing = Path("..") / f".xcraft-develop-{Path.cwd().name}.json"
    previous = set(json.loads(listing.read_text())) if listing.exists() else set()
    files, written = set(), 0
    with tarfile.open(archive_path) as archive:
        for member in archive.getmembers():
            if (name := os.path.normpath(member.name)) in (".", "pax_global_header") or name.startswith(("/", "..")):
                continue
            if member.isdir():
                if (path := Path(name)).is_symlink() or (path.exists() and not path.is_dir()):
                    _remove(path)
                path.mkdir(parents=True, exist_ok=True)
            elif member.isfile() or member.issym():
                files.add(name)
                written += _synced(archive, member, Path(name))
    for name in sorted(previous - files):
        _remove(Path(name))
    listing.write_text(json.dumps(sorted(files)))
    sys.stdout.write(f"xcraft develop: {written} files written, {len(previous - files)} removed, of {len(files)}\n")


def main() -> None:
    """Synchronize the directories of the spec of the command line, parsed for its target, with their archive."""
    spec, target = sys.argv[1:]
    if MARK not in Path(spec).read_text():
        return
    rpm.reloadConfig(target)
    for macro in Path("/etc/xcraft/undefined.list").read_text().split():
        rpm.delMacro(macro)
    rpm.addMacro("_topdir", str(Path(spec).resolve().parent.parent))
    for line in dict.fromkeys(line for line in rpm.spec(spec).prep.splitlines() if line.startswith(MARK)):
        archive, directory = line.removeprefix(MARK).split(" ", 1)
        Path(directory).mkdir(parents=True, exist_ok=True)
        os.chdir(directory)
        _sync(archive)


main()
