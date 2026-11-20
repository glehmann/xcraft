"""The tests, with the project they copy: project/, its xcraft.toml and packages."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from xcraft import buildid
from xcraft.__main__ import app
from xcraft.packages import Binary, Package
from xcraft.project import Config, Project
from xcraft.resolution import Resolved

PROJECT = Path(__file__).parent / "project"
UPSTREAM = Path(__file__).parent / "upstream"
TOYS = (
    UPSTREAM / "repos" / hashlib.sha256(b"".join(f.read_bytes() for f in sorted(UPSTREAM.glob("*.*")))).hexdigest()[:16]
)
"""The repository of the upstream packages of the tests, by architecture, made by build.sh from their specs: see
conftest.py."""
TOML = (PROJECT / "xcraft.toml").read_text().replace("@TOYS@", str(TOYS))
# the platform of the host, which the tests build for, natively, like x86_64_v2 on x86_64: none on the others, whose
# tests are skipped, see conftest.py
PLATFORM, ARCH = {"x86_64": ("linux/amd64/v2", "x86_64_v2"), "aarch64": ("linux/arm64", "aarch64")}.get(
    os.uname().machine, ("", "")
)
needs_gpg = pytest.mark.skipif(not shutil.which("gpg"), reason="needs gpg")
needs_git_lfs = pytest.mark.skipif(not shutil.which("git-lfs"), reason="needs git-lfs")


def config() -> Config:
    """Return the configuration of the test project."""
    return Config.model_validate(tomllib.loads(TOML))


def copy_project(dest: Path, template: Path = PROJECT) -> None:
    """Copy the project to `dest`: the `template` of the build_environment fixture has its lock and what xcraft
    resolved for it.
    """

    def ignored(directory: str, names: list[str]) -> list[str]:
        kept = {".xcraft": ["cache"], ".xcraft/cache": ["base", "resolved", "specs"]}.get(
            Path(directory).relative_to(template).as_posix()
        )
        return [name for name in names if kept is not None and name not in kept]

    shutil.copytree(template, dest, ignore=ignored)
    (dest / "xcraft.toml").write_text(TOML)


def ours(project: Project, monkeypatch: pytest.MonkeyPatch, deps: dict[str, set[str]]) -> None:
    """Give `project` packages of ours, as parsed, built against `deps`: each one with a binary package of its name,
    which those built against it require, in an image without packages of ours.
    """
    project.parsed = {
        name: Package(
            name=name,
            buildrequires=sorted(needed),
            binaries=[Binary(name=name, evr="1-1", arch="noarch", provides=[], requires=[])],
        )
        for name, needed in deps.items()
    }
    project.refresh()
    monkeypatch.setattr("xcraft.resolution.resolve_image", lambda _: Resolved())


runner = CliRunner()


def xcraft(*args: str) -> tuple[int, str]:
    """Run xcraft, returning its exit code and output, including the error main() would show."""
    result = runner.invoke(app, list(args))
    error = result.exception if result.exception and not isinstance(result.exception, SystemExit) else ""
    return result.exit_code, f"{result.output}{error}"


def results(output: str) -> list[str]:
    """Return the lines of the results of a build, without its errors and warnings."""
    return [line for line in output.splitlines() if line.startswith(("✓", "✗", "-", "↓")) or line[:1].isdigit()]


def write_spec(root: Path, name: str, requires: str = "", files: str = "/usr/share/doc/%{name}") -> None:
    """Write a minimal noarch package installing `files`, with the BuildRequires `requires`."""
    (root / "base" / name).mkdir()
    (root / "base" / name / f"{name}.spec").write_text(
        f"Name: {name}\nVersion: 1.0\nRelease: 1%{{?dist}}\nSummary: test\nLicense: MIT\nBuildArch: noarch\n"
        + (f"BuildRequires: {requires}\n" if requires else "")
        + f"%description\ntest\n%install\nmkdir -p %{{buildroot}}{files.rsplit('/', 1)[0]}\n"
        + f"touch %{{buildroot}}{files}\n%files\n{files}\n"
    )


def edit(root: Path, line: str, *, name: str = "trivial", top: bool = True) -> None:
    """Change the spec of `name` by a comment `line`, at its top or its end."""
    spec = root / f"base/{name}/{name}.spec"
    spec.write_text(f"# {line}\n{spec.read_text()}" if top else f"{spec.read_text()}# {line}\n")


def git(repo: Path, *args: str, input: str | None = None) -> str:  # noqa: A002, like subprocess.run
    """Run git in `repo`, as a test user, and return its output, stripped: CalledProcessError if it fails."""
    return subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        input=input,
    ).stdout.strip()


def commit_base(root: Path, branch: str = "master") -> None:
    """Make `root` a git repository, whose main branch, `branch`, has the build IDs."""
    git(root, "init", "-q", "-b", branch)
    buildid.update(Project.find())
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
