"""Patchsets: the commits of a git branch, exported as patches of a package.

The patches are listed in the spec in a block managed by xcraft, recording
where they come from, and applied by ``%autosetup`` or ``%autopatch``, in
the order of their numbers.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from xcraft.process import git
from xcraft.sources import archive_name

if TYPE_CHECKING:
    from xcraft.specs import Package

BEGIN = "# xcraft patchset: "
END = "# end of xcraft patchset"
_PATCH_RE = re.compile(r"^Patch(\d+)\s*:\s*(\S+)\s*$", re.MULTILINE)


class PatchsetError(RuntimeError):
    """Raised when a patchset cannot be exported."""


def _origin(repo: Path) -> str:
    """Return the URL of the origin remote of a git repository, or its path without one."""
    try:
        return git("-C", str(repo), "remote", "get-url", "origin")
    except subprocess.CalledProcessError:
        return str(repo.resolve())


def repo_name(repo: Path) -> str:
    """Return the name of a git repository: the one of its origin remote, or of its directory."""
    return _origin(repo).rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1].removesuffix(".git")


def guess_package(name: str, packages: dict[str, Package]) -> str | None:
    """Return the package of the project `name`: same directory or spec name, or a Source0 named after it."""
    for package, metadata in sorted(packages.items()):
        if name in (package, metadata.binaries[0].name if metadata.binaries else None) or re.fullmatch(
            rf"{re.escape(name)}-\d.*", archive_name(metadata.source0)
        ):
            return package
    return None


def guess_base(repo: Path, version: str) -> str:
    """Return the tag of `version` in the repository, which must be an ancestor of HEAD."""
    for tag in (f"v{version}", version):
        try:
            git("-C", str(repo), "merge-base", "--is-ancestor", tag, "HEAD")
        except subprocess.CalledProcessError:
            continue
        return tag
    msg = f"no tag v{version} or {version} under HEAD, give the base of the patchset with --base"
    raise PatchsetError(msg)


def export(repo: Path, base: str, dest: Path) -> list[Path]:
    """Export the commits of `base`..HEAD in `repo` as patches in `dest`, in order."""
    # --zero-commit: the patches stay the same when the commits are only rebased
    output = git(
        "-C", str(repo), "format-patch", "--no-numbered", "--no-signature", "--zero-commit",
        "-o", str(dest), f"{base}..HEAD",
    )  # fmt: skip
    return [Path(line) for line in output.splitlines()]


def _subject(patch: str) -> str:
    m = re.search(r"^Subject: (?:\[PATCH[^]]*\] )?(.*(?:\n .*)*)", patch, re.MULTILINE)
    return " ".join(m.group(1).split()) if m else ""


@dataclass
class Update:
    """The changes of a patchset update."""

    added: list[str]
    removed: list[str]
    replaced: list[str]
    """Patches of the spec outside of a patchset block, replaced as previous exports of the same commits."""


def _place(spec: Path, pkg_dir: Path, subjects: set[str]) -> tuple[list[str], int, int, list[str], list[str]]:
    """Return the lines of `spec`, where its patchset block goes, from its start to its end, the patches it replaces,
    and those of them outside of a block, previous exports of the commits of the `subjects`.
    """  # noqa: D205
    lines = spec.read_text().splitlines(keepends=True)
    if (start := next((i for i, line in enumerate(lines) if line.startswith(BEGIN)), None)) is not None:
        if (end := next((i + 1 for i in range(start, len(lines)) if lines[i].startswith(END)), None)) is None:
            msg = f"{spec.name} has no {END.removeprefix('# ')!r} after its {BEGIN.strip()!r}"
            raise PatchsetError(msg)
        return lines, start, end, [m.group(2) for line in lines[start:end] if (m := _PATCH_RE.match(line))], []
    # previous exports of the same commits, by subject
    matching = {
        i: m.group(2) for i, line in enumerate(lines)
        if (m := _PATCH_RE.match(line)) and (pkg_dir / m.group(2)).is_file()
        and _subject((pkg_dir / m.group(2)).read_text(errors="replace")) in subjects
    }  # fmt: skip
    if matching:
        start = min(matching)
        lines = [line for i, line in enumerate(lines) if i == start or i not in matching]
        return lines, start, start + 1, list(matching.values()), list(matching.values())
    # after the last patch, or the last source
    last = [i for i, line in enumerate(lines) if re.match(r"^(Patch|Source)\d*\s*:", line)]
    start = (max(last) + 1) if last else 0
    return lines, start, start, [], []


def update_patchset(pkg_dir: Path, spec: Path, repo: Path, base: str, first: int) -> Update:
    """Replace the patchset of the package in `pkg_dir` with the commits of `base`..HEAD in `repo`."""
    text = spec.read_text()
    if not re.search(r"^%(autosetup|autopatch)\b", text, re.MULTILINE):
        msg = f"{spec.name} applies its patches explicitly: a patchset needs %autosetup or %autopatch"
        raise PatchsetError(msg)
    with tempfile.TemporaryDirectory(prefix="xcraft-patchset-") as tmp:
        patches = export(repo, base, Path(tmp))
        if not patches:
            msg = f"no commit in {base}..HEAD"
            raise PatchsetError(msg)
        lines, start, end, old, replaced = _place(spec, pkg_dir, {_subject(p.read_text()) for p in patches})
        others = {int(m.group(1)) for m in _PATCH_RE.finditer("".join(lines[:start] + lines[end:]))}
        numbers = range(first, first + len(patches))
        if clashes := sorted(others & set(numbers)):
            msg = f"patch numbers {first}-{numbers[-1]} are used by other patches ({clashes[0]}...), see --first"
            raise PatchsetError(msg)
        names = [p.name for p in patches]
        if clobbered := sorted(name for name in set(names) - set(old) if (pkg_dir / name).exists()):
            msg = f"the patches would overwrite files of the package outside of the patchset: {', '.join(clobbered)}"
            raise PatchsetError(msg)
        head = git("-C", str(repo), "rev-parse", "--short=12", "HEAD")
        branch = git("-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD")
        block = [
            f"{BEGIN}{_origin(repo)} {branch} {base}..{head}\n",
            *(f"Patch{number}: {patch.name}\n" for number, patch in zip(numbers, patches, strict=True)),
            f"{END}\n",
        ]
        spec.write_text("".join(lines[:start] + block + lines[end:]))
        for name in set(old) - set(names):
            (pkg_dir / name).unlink(missing_ok=True)
        for patch in patches:
            (pkg_dir / patch.name).write_text(patch.read_text())
    return Update(added=sorted(set(names) - set(old)), removed=sorted(set(old) - set(names)), replaced=replaced)
