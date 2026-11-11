"""Imports of packages into the project, as git branches the project merges: ``import/<pkg>/<branch>``.

The history of the branches of a packaging repository, ``SPECS/<name>.spec`` and ``SOURCES/*``, or the files at its top,
like Fedora's, with the source archives stored with git LFS, is rewritten for the project: the files go in the directory
of the package, the archives in its ``sources`` manifest, by the SHA-256 of their LFS pointer, and nothing else. The
commits only changing what is left out are skipped, and each one gets an ``Imported-from`` trailer, with the repository
and the commit it comes from. The rewrite only depends on the imported commits and the first commit of the project,
empty, which the first ones get as parent: importing again gives the same commits, with the new ones on top, and the
branches sharing commits, like master merging XS-9.0, share them. Merging a branch into the project, where the package
is not yet, joins the histories. The archives of the imported branches which are neither in the source cache nor in the
remote storage come from git LFS into the source cache, for xcraft push.

Source RPMs, like XenServer's, are imported as new commits of these branches too, laid out the same way: their
archives go into the source cache.

Only git is used, and git-lfs for the archives, rather than the API of a forge like GitHub.
"""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from xcraft import container, process, remote
from xcraft.process import git
from xcraft.sources import cache_source, has_cached_source, parse_manifest, source_path, write_manifest

if TYPE_CHECKING:
    from collections.abc import Callable

    from xcraft.project import Project

DEFAULT_URL = "https://github.com/xcp-ng-rpms/{name}"
_ARCHIVE = re.compile(r"\.(tar(\.\w+)?|tgz|tbz2|txz|zip|gem|crate)$")

_LFS = re.compile(rb"version https://git-lfs\.github\.com/spec/v1\noid sha256:([0-9a-f]{64})\nsize \d+\n")
_LFS_SIZE = 1024
"""The pointers of git LFS are smaller."""
_TRAILER = re.compile(rb"^[\w-]+: ")
_NOT_PACKAGED = re.compile(rb"\.|README|(sources|Makefile|import\.log)$")
"""The files at the top of a packaging repository of a flat layout which are not of the package: those of git and of
GitHub, the README, and those of Fedora's dist-git, like its ``sources``, of archives in its lookaside cache, whose
SHA-256 are unknown."""
_DELETED = "File deleted from the original sources for trademark-related or copyright-related legal reasons.\n"


def _is_file_to_import(path: Path, top: Path) -> bool:
    """Return whether `path` is a file to import, or a directory, refusing anything else, like symbolic links."""
    if path.is_symlink() or not (path.is_file() or path.is_dir()):
        msg = f"{path.relative_to(top)}: not a regular file, not imported"
        raise RuntimeError(msg)
    return path.is_file()


def _unpack_srpm(project: Project, srpm: Path, dest: Path, *, on_line: Callable[[str], None]) -> None:
    """Unpack the source RPM `srpm` into `dest`, but its archives: they go into the source cache, and the ``sources``
    manifest of `dest`.
    """
    with tempfile.TemporaryDirectory(prefix="xcraft-srpm-") as content:
        # rpm2cpio is not on every host
        on_line(f"extracting {srpm.name}")
        container.run(
            project.on_host(),  # natively: the extraction does not depend on the architecture
            "import",
            f"cd /content && rpm2cpio /srpm/{shlex.quote(srpm.name)} | cpio -idm --quiet",
            volumes=[f"{srpm.parent}:/srpm:ro", f"{content}:/content"],
        )
        manifest = {}
        for path in sorted(Path(content).iterdir()):
            if not _is_file_to_import(path, Path(content)):
                continue
            if _ARCHIVE.search(path.name):
                manifest[path.name] = cache_source(project.source_cache, path)
            else:
                shutil.copy2(path, dest / path.name)
        if manifest:
            write_manifest(dest / "sources", manifest)


class PackageImportError(RuntimeError):
    """Raised when a package cannot be imported."""


@dataclass
class _Commit:
    mark: bytes
    oid: bytes
    header: bytes
    """The author and committer lines."""
    message: bytes
    parents: list[bytes]
    files: dict[bytes, tuple[bytes, bytes]] = field(default_factory=dict)
    """The mode and blob of each path, all of them, with --full-tree."""


def _unquote(path: bytes) -> bytes:
    """Return a path of git fast-export, which quotes those with special characters like C."""
    return path[1:-1].decode("unicode_escape").encode("latin-1") if path.startswith(b'"') else path


def _parse(stream: bytes) -> list[_Commit]:
    """Return the commits of a stream of git fast-export --no-data --full-tree --show-original-ids, parents first."""
    commits: list[_Commit] = []
    pos = 0

    def line() -> bytes:
        nonlocal pos
        end = stream.find(b"\n", pos)
        start, pos = pos, len(stream) if end < 0 else end + 1
        return stream[start : pos - 1] if end >= 0 else stream[start:]

    while pos < len(stream):
        if not (command := line()).startswith(b"commit "):
            continue  # reset, from of a reset, blank
        mark, oid, header = line()[5:], line()[13:], b""
        while not (command := line()).startswith(b"data "):
            header += command + b"\n"
        size = int(command[5:])
        commit, pos = _Commit(mark, oid, header, stream[pos : pos + size], []), pos + size
        pos += stream[pos : pos + 1] == b"\n"  # the optional LF after the data, before deleteall
        while pos < len(stream) and (command := line()):
            if command.startswith((b"from ", b"merge ")):
                commit.parents.append(command.split()[1])
            elif command.startswith(b"M "):
                mode, blob, path = command[2:].split(b" ", 2)
                commit.files[_unquote(path)] = (mode, blob)
        commits.append(commit)
    return commits


def _lfs_pointers(repo: str, blobs: set[bytes]) -> dict[bytes, str]:
    """Return the SHA-256 of the archive of each of the `blobs` which is a pointer of git LFS."""
    if not blobs:  # git cat-file would read an empty line
        return {}
    small = [
        oid for oid, _, size in (line.split() for line in process.run_bytes(
            ["git", "-C", repo, "cat-file", "--batch-check"], b"\n".join(sorted(blobs)) + b"\n"
        ).splitlines()) if int(size) < _LFS_SIZE
    ]  # fmt: skip
    output, pointers, pos = (
        process.run_bytes(["git", "-C", repo, "cat-file", "--batch"], b"\n".join(small) + b"\n"),
        {},
        0,
    )
    for oid in small:
        end = output.index(b"\n", pos)
        size = int(output[pos:end].split()[2])
        if m := _LFS.fullmatch(output[end + 1 : end + 1 + size]):
            pointers[oid] = m.group(1).decode()
        pos = end + 1 + size + 1
    return pointers


def _layout(
    commit: _Commit, prefix: bytes, pointers: dict[bytes, str]
) -> tuple[dict[bytes, tuple[bytes, bytes]], bytes]:
    """Return the files of `commit` in the package directory `prefix`, and its ``sources`` manifest: those of
    ``SPECS/`` and ``SOURCES/``, or else those at its top, but those which are not of the package.
    """
    files, manifest = {}, {}
    flat = not any(path.startswith((b"SPECS/", b"SOURCES/")) for path in commit.files)
    for path, (mode, blob) in commit.files.items():
        top, _, rel = path.partition(b"/")
        if flat:
            if rel or _NOT_PACKAGED.match(path):
                continue
            rel = path
        elif top not in (b"SPECS", b"SOURCES") or not rel:
            continue
        if mode not in (b"100644", b"100755"):  # like a link to a file of the host
            msg = f"{path.decode(errors='replace')} of {commit.oid.decode()}: not a regular file, not imported"
            raise PackageImportError(msg)
        if blob in pointers:
            manifest[rel.rsplit(b"/", 1)[-1].decode()] = pointers[blob]
        else:
            files[prefix + rel] = (mode, blob)
    return files, "".join(f"{digest}  {name}\n" for name, digest in sorted(manifest.items())).encode()


def _with_trailer(message: bytes, trailer: bytes) -> bytes:
    message = message.rstrip(b"\n")
    paragraphs = message.split(b"\n\n")
    in_trailers = len(paragraphs) > 1 and all(_TRAILER.match(line) for line in paragraphs[-1].splitlines())
    return message + (b"\n" if in_trailers else b"\n\n") + trailer + b"\n"


def _repository(root: Path) -> tuple[str, str]:
    """Return the top of the git repository of the project in `root`, and its first commit, which must be empty."""
    repo = git("-C", str(root), "rev-parse", "--show-toplevel")
    start = git("-C", repo, "rev-list", "--max-parents=0", "HEAD").split()
    if len(start) != 1 or git("-C", repo, "ls-tree", start[0]):
        msg = "the history of the project must start with an empty commit, the only one without parent, for the imports"
        raise PackageImportError(msg)
    return repo, start[0]


def _commit(repo: str, ref: str) -> str | None:
    """Return the commit of `ref`, if it exists."""
    found = process.run(["git", "-C", repo, "rev-parse", "--verify", "--quiet", ref], check=False, capture=True)
    return found.stdout.strip() if found.returncode == 0 else None


def _fast_forward(repo: str, ref: str, commit: str) -> None:
    """Move the branch `ref` forward to `commit`, refusing to drop its commits."""
    old = _commit(repo, ref)
    if old and process.run(["git", "-C", repo, "merge-base", "--is-ancestor", old, commit], check=False).returncode:
        msg = f"{ref.removeprefix('refs/heads/')} has commits that are not imported, like those of import-srpm"
        raise PackageImportError(msg)
    git("-C", repo, "update-ref", ref, commit, old or "")


def _cache_lfs_archives(project: Project, repo: str, url: str, manifests: dict[str, dict[str, str]]) -> list[str]:
    """Put into the source cache the archives of the `manifests` of the refs of `repo`, fetched from `url`, which
    neither the source cache nor the remote storage have, from git LFS, and return those found nowhere.
    """
    wanted = {(n, d) for m in manifests.values() for n, d in m.items() if not has_cached_source(project, n, d)}
    paths = {source_path(n, d): (n, d) for n, d in wanted}
    for storage in remote.storages(project) if wanted else []:
        with contextlib.suppress(remote.RemoteError):  # then from git LFS
            wanted -= {paths[p] for p in remote.existing(storage, list(paths))}
    if not wanted or not shutil.which("git-lfs"):
        return sorted(n for n, _ in wanted)
    env = git("-C", repo, "lfs", "env").splitlines()
    media = Path(next(line.split("=", 1)[1] for line in env if line.startswith("LocalMediaDir=")))
    objects = {d: media / d[:2] / d[2:4] / d for _, d in wanted}
    had = {d for d, obj in objects.items() if obj.exists()}
    for ref, manifest in manifests.items():
        if names := sorted(n for n, d in manifest.items() if (n, d) in wanted):
            process.run(
                ["git", "-C", repo, "lfs", "fetch", url, ref, f"--include={','.join(names)}"],
                check=False, capture=True, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )  # fmt: skip
    for name, digest in sorted(wanted):
        if objects[digest].exists():
            (project.source_cache / digest).mkdir(parents=True, exist_ok=True)
            shutil.copyfile(objects[digest], project.source_cache / digest / name)
            wanted.discard((name, digest))
    for digest in objects.keys() - had:  # in the source cache only
        objects[digest].unlink(missing_ok=True)
    return sorted(n for n, _ in wanted)


def import_history(project: Project, pkg_dir: Path, url: str, branches: list[str]) -> tuple[dict[str, str], list[str]]:
    """Import the history of the `branches` of `url` into the branches import/<pkg>/<branch> of the git repository of
    the project, for the package in `pkg_dir`, and return their commits, and the archives of their ``sources`` which
    are neither in the source cache, the remote storage, nor git LFS. Missing branches are skipped, and those having
    commits that are not imported refused.
    """
    repo, start = _repository(project.root)
    first = start.encode()
    prefix = pkg_dir.resolve().relative_to(repo).as_posix().encode() + b"/"
    source = "/".join(url.removesuffix(".git").rstrip("/").split("/")[-2:]).encode()
    remote = {
        line.split()[1].removeprefix("refs/heads/")
        for line in process.run(
            ["git", "ls-remote", "--heads", url], capture=True, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        ).stdout.splitlines()
    }
    if not (branches := [b for b in branches if b in remote]):
        msg = f"none of the branches to import in {url}"
        raise PackageImportError(msg)
    fetched = {b: f"refs/xcraft/history/fetched/{b}" for b in branches}
    git(
        "-C", repo, "fetch", "--quiet", "--no-tags", "--no-auto-maintenance", url,
        *(f"+refs/heads/{b}:{ref}" for b, ref in fetched.items()),
    )  # fmt: skip
    try:
        tips = {b: git("-C", repo, "rev-parse", ref).encode() for b, ref in fetched.items()}
        # Commit signatures are stripped by default; --signed-commits needs Git 2.50.
        commits = _parse(process.run_bytes([
            "git", "-C", repo, "fast-export", "--no-data", "--full-tree", "--show-original-ids", "--reencode=yes",
            "--signed-tags=strip", *fetched.values(),
        ]))  # fmt: skip
        pointers = _lfs_pointers(repo, {blob for c in commits for _, blob in c.files.values()})
        # the rewritten commits: their trees and ancestors, and the one each commit maps to
        stream, trees, ancestors, kept = [], {first: (frozenset(), b"")}, {first: frozenset()}, {}
        for c in commits:
            files, manifest = _layout(c, prefix, pointers)
            parents = list(dict.fromkeys(kept[p] for p in c.parents)) or [first]
            # but those already merged in another one, by the commits skipped
            parents = [p for p in parents if not any(p in ancestors[q] for q in parents)]
            tree = (frozenset(files.items()), manifest)
            if len(parents) == 1 and tree == trees[parents[0]]:
                kept[c.mark] = parents[0]
                continue
            kept[c.mark], trees[c.mark] = c.mark, tree
            ancestors[c.mark] = frozenset(parents).union(*(ancestors[p] for p in parents))
            message = _with_trailer(c.message, b"Imported-from: " + source + b"@" + c.oid)
            stream += [b"commit refs/xcraft/history/new\nmark ", c.mark, b"\n", c.header, b"data %d\n" % len(message)]
            stream += [message, *(b"%s %s\n" % (b"merge" if i else b"from", p) for i, p in enumerate(parents))]
            stream += [b"deleteall\n", *(b"M %s %s %s\n" % (*files[p], p) for p in sorted(files))]
            if manifest:
                stream += [b"M 100644 inline %ssources\ndata %d\n" % (prefix, len(manifest)), manifest]
            stream.append(b"\n")
        marks = {c.oid: c.mark for c in commits}
        imported = {b: f"refs/xcraft/history/imported/{b}" for b, tip in tips.items() if kept[marks[tip]] != first}
        stream += [b"reset %s\nfrom %s\n\n" % (ref.encode(), kept[marks[tips[b]]]) for b, ref in imported.items()]
        process.run_bytes(["git", "-C", repo, "fast-import", "--quiet", "--force"], b"".join(stream))
        heads = {b: git("-C", repo, "rev-parse", ref) for b, ref in imported.items()}
        for branch, commit in heads.items():
            _fast_forward(repo, f"refs/heads/import/{pkg_dir.name}/{branch}", commit)
        manifests = {fetched[b]: parse_manifest(trees[kept[marks[tips[b]]]][1].decode()) for b in imported}
        missing = _cache_lfs_archives(project, repo, url, manifests)
    finally:
        for ref in [
            *fetched.values(),
            "refs/xcraft/history/new",
            *(f"refs/xcraft/history/imported/{b}" for b in branches),
        ]:
            process.run(["git", "-C", repo, "update-ref", "-d", ref], check=False, capture=True)
    return heads, missing


def import_srpm(
    project: Project, srpm: Path, pkg_dir: Path, *, on_line: Callable[[str], None] = lambda _: None
) -> str | None:
    """Commit the content of the source RPM `srpm`, of the package in `pkg_dir`, on import/<pkg>/<branch>, the
    srpm_branch of [import] in xcraft.toml,
    started from the first commit of the project for a new one, and return the commit, or None when it is the same.
    """
    repo, first = _repository(project.root)
    ref = f"refs/heads/import/{pkg_dir.name}/{project.config.import_.srpm_branch}"
    parent = _commit(repo, ref) or first
    with project.tmp_dir("import-srpm") as work:
        dest = work / "tree" / pkg_dir.resolve().relative_to(repo)
        dest.mkdir(parents=True)
        _unpack_srpm(project, srpm, dest, on_line=on_line)
        removed = [f for f in project.config.import_.removed if (dest / f).exists()]
        for f in removed:
            (dest / f).unlink()
            (dest / f"{f}.deleted-by-XCP-ng.txt").write_text(_DELETED)
        git_dir = git("-C", repo, "rev-parse", "--absolute-git-dir")
        index = {**os.environ, "GIT_INDEX_FILE": str(work / "index")}
        process.run(
            ["git", "-C", str(work / "tree"), f"--git-dir={git_dir}", "--work-tree=.", "add", "-A", "."], env=index
        )
        tree = process.run(["git", "-C", repo, "write-tree"], capture=True, env=index).stdout.strip()
    if tree == git("-C", repo, "rev-parse", f"{parent}^{{tree}}"):
        return None
    ident = git("-C", repo, "var", "GIT_COMMITTER_IDENT").rsplit(" ", 2)[0]
    message = f"Import {srpm.name}\n"
    if removed:
        message += "\nFiles deleted for legal reasons:\n" + "".join(f" - {f}\n" for f in removed)
    message += f"\nSigned-off-by: {ident}\n"
    commit = process.run(
        ["git", "-C", repo, "commit-tree", tree, "-p", parent], capture=True, input=message
    ).stdout.strip()
    _fast_forward(repo, ref, commit)
    return commit
