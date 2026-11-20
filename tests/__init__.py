"""The tests, with the project they copy: project/, its xcraft.toml and packages."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
import subprocess
import tomllib
import urllib.request
from pathlib import Path

import pytest
from typer.testing import CliRunner

from xcraft import buildid
from xcraft.__main__ import app
from xcraft.iso import Image, cached_iso, iso_key
from xcraft.packages import Binary, Package
from xcraft.project import Config, Project
from xcraft.resolution import Resolved
from xcraft.signing import Signer, release_signer
from xcraft.sources import sha256_of

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

# the Garage server of the remote storage, see conftest.py: one for each worker of pytest-xdist, with its name and ports
WORKER = int(os.environ.get("PYTEST_XDIST_WORKER", "gw0").removeprefix("gw"))
GARAGE = f"xcraft-test-garage-{WORKER}"
S3_PORT, WEB_PORT = 13900 + 10 * WORKER, 13902 + 10 * WORKER
WEB = f"http://xcraft.web.garage.localhost:{WEB_PORT}"
REMOTE = (
    # only the host's: publish fetches the builds of the others
    TOML.replace('platforms = ["linux/amd64/v2", "linux/arm64"]', f'platforms = ["{PLATFORM}"]')
    + f'[remote.main]\nurl = "{WEB}"\ns3_endpoint = "http://localhost:{S3_PORT}"\nbucket = "xcraft"\n'
    + f'[remote.dev]\nurl = "http://xcraft-dev.web.garage.localhost:{WEB_PORT}"\n'
    + f's3_endpoint = "http://localhost:{S3_PORT}"\nbucket = "xcraft-dev"\n'
    # not the build repository, with the hundreds of source packages of their build environment to download
    '[publish]\nrelease = "9.0"\nbuild = false\n'
)
"""The configuration of the tests with the remote storage: storage_root(), in conftest.py, gives each test its own,
with its stage, STAGE, and the key of its builds, in the REMOTE and STAGE of its module."""
STAGE = ""


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
    buildid.target = None  # of its --maintenance, for this command only, not the functions the next tests call
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


def garage(*args: str) -> str:
    """Run the command of the Garage server of the remote storage, and return its output."""
    return subprocess.run(
        ["podman", "exec", GARAGE, "/garage", *args], check=True, capture_output=True, text=True
    ).stdout


def page(directory: str) -> str:
    """Return the page of a directory of the storage, through its public endpoint."""
    with urllib.request.urlopen(f"{WEB}/{directory}") as response:  # noqa: S310, the test garage
        assert response.headers["Content-Type"].startswith("text/html")
        assert response.headers["Cache-Control"] == "no-cache"
        return response.read().decode()


def unsigned(remote: str) -> str:
    """Return the configuration `remote` of the tests, without the [sign] section of the key of their builds."""
    return re.sub(r"\[sign\]\n.*?(?=\[remote.main\])", "", remote, flags=re.DOTALL)


def cache_image(
    project: Project, image: Path, *, signer: Signer | None = None, netinstall: bool = False, keep: bool = False
) -> Path:
    """Seed a cached image to exercise lookups and transfers independently of distribution assembly: of dev mode with
    `keep`, like those of xcraft iso without --final.
    """
    tree = cached_iso(project, iso_key(project, netinstall=netinstall, volid=None, signer=signer, keep=keep))
    tree.mkdir(parents=True)
    shutil.copy2(image, tree / "image.iso")
    (tree / "image.json").write_text(Image(sha256=sha256_of(image)).model_dump_json())
    return tree


def pushed_iso(project: Project, tmp_path: Path) -> tuple[Signer, Path, Path]:
    """Push the ISO of a cached image, signed with the key of the builds of the storage, like those of CI, see
    storage_root(), and return that key, the image, and the tree of its cache.
    """
    signer = release_signer(project)
    assert signer
    image = tmp_path / "test.iso"
    image.write_bytes(secrets.token_bytes(2048))
    tree = cache_image(project, image, signer=signer)
    code, output = xcraft("push", "isos")
    assert code == 0, output
    assert "1 ISOs pushed" in output
    return signer, image, tree
