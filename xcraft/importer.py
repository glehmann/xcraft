"""Import packages from their packaging git repositories into ``specs/``.

Two layouts of packaging repositories are supported:

- the rpmbuild one, ``SPECS/<name>.spec`` and ``SOURCES/*``, with the source
  archives stored with git LFS;
- the dist-git one of Fedora and AlmaLinux, with everything at the top, and the
  source archives listed in a ``sources`` file as ``SHA512 (<file>) = <hash>``,
  to be downloaded from a lookaside cache.

Packages are also imported from source RPMs, for those only published this
way, like CentOS 7's.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import shlex
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

from xcraft.container import run
from xcraft.display import download_progress
from xcraft.sources import cache_source, sha256_of, write_manifest

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from xcraft.project import Project

DEFAULT_URL = "https://github.com/xcp-ng-rpms/{name}"
LOOKASIDE = "https://sources.almalinux.org/{checksum}"

_DIST_GIT_SOURCE = re.compile(r"^(\w+) \((.+)\) = ([0-9a-f]+)$")
_ARCHIVE = re.compile(r"\.(tar(\.\w+)?|tgz|tbz2|txz|zip|gem|crate)$")


def git(*args: str) -> str:
    """Run git and return its stripped output."""
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def read_origin(pkg_dir: Path) -> tuple[str, str, str] | None:
    """Return the (url, branch, commit) a package was last imported from, if any."""
    path = pkg_dir / "origin"
    if not path.exists():
        return None
    url, branch, commit = path.read_text().split()
    return url, branch, commit


def _fetch_lookaside(  # noqa: PLR0913
    cache_dir: Path, filename: str, algorithm: str, checksum: str, lookaside: str, *, on_line: Callable[[str], None]
) -> str:
    """Download an archive from a lookaside cache into the source cache, and return its SHA-256."""
    if Path(filename).name != filename or filename in {".", ".."}:
        msg = f"not a file name, in the sources file: {filename!r}"
        raise RuntimeError(msg)
    with tempfile.TemporaryDirectory(prefix="xcraft-lookaside-") as tmp:
        path = Path(tmp) / filename
        urllib.request.urlretrieve(  # noqa: S310
            lookaside.format(checksum=checksum),
            path,
            reporthook=download_progress(on_line, f"downloading {filename}: "),
        )
        with path.open("rb") as f:
            if hashlib.file_digest(f, algorithm.lower()).hexdigest() != checksum:
                msg = f"{filename} from {lookaside.format(checksum=checksum)} does not match its {algorithm}"
                raise RuntimeError(msg)
        return cache_source(cache_dir, path)


def _is_file_to_import(path: Path, top: Path) -> bool:
    """Return whether `path` is a file to import, or a directory, refusing anything else, like symbolic links."""
    if path.is_symlink() or not (path.is_file() or path.is_dir()):
        msg = f"{path.relative_to(top)}: not a regular file, not imported"
        raise RuntimeError(msg)
    return path.is_file()


@contextlib.contextmanager
def _replacing(pkg_dir: Path) -> Generator[Path]:
    """Return a new directory, which replaces `pkg_dir` once done, to leave it as it was on failure."""
    new = pkg_dir.with_name(f".{pkg_dir.name}.import")
    shutil.rmtree(new, ignore_errors=True)
    new.mkdir(parents=True)
    try:
        yield new
    except BaseException:
        shutil.rmtree(new, ignore_errors=True)
        raise
    shutil.rmtree(pkg_dir, ignore_errors=True)
    new.rename(pkg_dir)


def import_package(  # noqa: PLR0913
    project: Project,
    pkg_dir: Path,
    url: str,
    branch: str,
    lookaside: str = LOOKASIDE,
    *,
    on_line: Callable[[str], None] = lambda _: None,
) -> str:
    """Replace `pkg_dir` with the packaging files of `url` at `branch`, and return the imported commit.

    With the url of a source RPM, ending with ``.src.rpm``, `branch` is not
    used, the commit is the SHA-256 of the source RPM, and the build environment
    of `project` extracts it. Source archives go
    into the source cache and the ``sources`` manifest instead. The ``origin``
    file records where the package comes from. The steps are given to `on_line`.
    """
    cache_dir = project.source_cache
    if url.endswith(".src.rpm"):
        return _import_srpm(project, pkg_dir, url, on_line=on_line)
    with tempfile.TemporaryDirectory(prefix="xcraft-import-") as tmp, _replacing(pkg_dir) as new:
        repo = Path(tmp)
        on_line(f"cloning {url}")
        git("clone", "--quiet", "--depth=1", f"--branch={branch}", url, tmp)
        on_line("fetching the LFS files")
        # fetch LFS files even when git-lfs is not set up to do it on checkout
        git("-C", tmp, "lfs", "pull")
        lfs = set(git("-C", tmp, "lfs", "ls-files", "--name-only").splitlines())
        if (repo / "SPECS").is_dir():
            files = [(path, top) for top in (repo / "SPECS", repo / "SOURCES") for path in top.rglob("*")]
            dist_git_sources = []
        else:
            files = [(path, repo) for top in repo.iterdir() if not top.name.startswith(".") and top.name != "sources"
                     for path in [top, *top.rglob("*")]]  # fmt: skip
            dist_git_sources = (repo / "sources").read_text().splitlines() if (repo / "sources").exists() else []
        manifest = {}
        for path, top in sorted(files):
            if not _is_file_to_import(path, repo):
                continue
            if path.relative_to(repo).as_posix() in lfs:
                manifest[path.name] = cache_source(cache_dir, path)
            else:
                dest = new / path.relative_to(top)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
        for line in dist_git_sources:
            if not (m := _DIST_GIT_SOURCE.match(line)):
                msg = f"unsupported line in the sources file: {line!r}"
                raise RuntimeError(msg)
            algorithm, filename, checksum = m.groups()
            manifest[filename] = _fetch_lookaside(cache_dir, filename, algorithm, checksum, lookaside, on_line=on_line)
        if manifest:
            write_manifest(new / "sources", manifest)
        commit = git("-C", tmp, "rev-parse", "HEAD")
        (new / "origin").write_text(f"{url} {branch} {commit}\n")
    return commit


def _import_srpm(project: Project, pkg_dir: Path, url: str, *, on_line: Callable[[str], None]) -> str:
    """Replace `pkg_dir` with the content of the source RPM at `url`, and return its SHA-256."""
    with tempfile.TemporaryDirectory(prefix="xcraft-import-") as tmp, _replacing(pkg_dir) as new:
        srpm = Path(tmp) / url.rsplit("/", 1)[-1]
        urllib.request.urlretrieve(url, srpm, reporthook=download_progress(on_line, f"downloading {srpm.name}: "))  # noqa: S310
        content = Path(tmp) / "content"
        content.mkdir()
        # rpm2cpio is not on every host
        on_line(f"extracting {srpm.name}")
        run(
            project,
            "import",
            f"cd /content && rpm2cpio /srpm/{shlex.quote(srpm.name)} | cpio -idm --quiet",
            volumes=[f"{srpm.parent}:/srpm:ro", f"{content}:/content"],
            network=False,
        )
        manifest = {}
        for path in sorted(content.iterdir()):
            if not _is_file_to_import(path, content):
                continue
            if _ARCHIVE.search(path.name):
                manifest[path.name] = cache_source(project.source_cache, path)
            else:
                shutil.copy2(path, new / path.name)
        if manifest:
            write_manifest(new / "sources", manifest)
        checksum = sha256_of(srpm)
        (new / "origin").write_text(f"{url} srpm {checksum}\n")
    return checksum
