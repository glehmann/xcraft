"""The builders: the builds of an architecture made on a remote host of it, natively, over SSH, by xcraft running there
on a copy of the checkout, see docs/remote-builders-design.md.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shlex
import sys
import tempfile
import tomllib
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from pydantic import BaseModel, ConfigDict

import xcraft
from xcraft import buildid, buildkey, develop, process
from xcraft.display import activity
from xcraft.hosts import Host, connected, ssh_host

if TYPE_CHECKING:
    from collections.abc import Generator

    from xcraft.project import Project

ROOT = ".cache/xcraft/builders"
"""Where the copies of the checkouts are on the builders, by project id, relative to the home of their user."""


class UserConfig(BaseModel):
    """The configuration of the user, out of the checkouts, for what is the developer's rather than the project's:
    $XDG_CONFIG_HOME/xcraft/config.toml.
    """

    model_config = ConfigDict(extra="forbid")

    builders: dict[str, str] = {}
    """The builders, by RPM architecture: a host, as given to ssh, or as ssh://[user@]host[:port]."""


def user_config_path() -> Path:
    """Return the configuration of the user, see UserConfig."""
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "xcraft/config.toml"


def builders() -> dict[str, str]:
    """Return the builders, by architecture: those of the configuration of the user, overridden by $XCRAFT_BUILDERS,
    like aarch64=ssh://arm64.example,x86_64_v2=, an empty host for none.
    """
    path = user_config_path()
    found = UserConfig.model_validate(tomllib.loads(path.read_text())).builders if path.exists() else {}
    for item in filter(None, os.environ.get("XCRAFT_BUILDERS", "").split(",")):
        arch, separator, host = item.partition("=")
        if not separator:
            msg = f"$XCRAFT_BUILDERS: not <arch>=<host>: {item}"
            raise ValueError(msg)
        found[arch.strip()] = host.strip()
    return {arch: host for arch, host in found.items() if host}


def _source(work: Path) -> Path | str:
    """Return what uv tool run installs the xcraft of this process from: the commit of its git repository, a wheel of
    it, built in `work`, for a version under development, or else its version.
    """
    dist = importlib.metadata.distribution("xcraft")
    direct = json.loads(dist.read_text("direct_url.json") or "{}")
    if vcs := direct.get("vcs_info"):
        return f"git+{direct['url']}@{vcs['commit_id']}"
    tree = Path(xcraft.__file__).resolve().parents[1]
    if not direct.get("dir_info") and not (tree / "pyproject.toml").exists():
        return f"xcraft=={dist.version}"
    process.run(["uv", "build", "--wheel", "--quiet", "--out-dir", str(work), str(tree)], capture=True)
    return next(work.glob("*.whl"))


def _rsync(host: Host, *args: str) -> None:
    process.run(["rsync", "-a", "-e", shlex.join(["ssh", *host.options]), *args], capture=True)


def _xcraft(host: Host, work: Path) -> list[str]:
    """Return the command running on `host` the same xcraft as this process, by uv tool run, copying a wheel of it to
    the host first, for a version under development; or the command of $XCRAFT_BUILDER_XCRAFT, of an xcraft installed
    there otherwise.
    """
    if command := os.environ.get("XCRAFT_BUILDER_XCRAFT"):
        return shlex.split(command)
    if isinstance(source := _source(work), Path):
        wheel = f"{ROOT}/wheels/{hashlib.sha256(source.read_bytes()).hexdigest()}/{source.name}"
        # by ssh, rather than rsync, which a builder to provision lacks, see provision()
        copy = f"mkdir -p {Path(wheel).parent} && cat > {wheel}.partial && mv {wheel}.partial {wheel} && pwd"
        source = f"{process.run_bytes(host.ssh(copy), source.read_bytes()).decode().strip()}/{wheel}"
    return ["uv", "--quiet", "tool", "run", "--from", str(source), "xcraft"]


class BuilderError(RuntimeError):
    """Raised when a builder lacks what the builds need, or runs another xcraft than this one, of another recipe of the
    builds.
    """


TOOLS = ["podman", "gpg", "git", "rsync", "flock"]
"""What a builder needs, with uv, unless $XCRAFT_BUILDER_XCRAFT: git above all, without which the builds would be final
ones, silently, with nothing to number them against."""


def check(host: Host) -> None:
    """Check that `host` has the tools of the builds, and podman rootless, as xcraft runs it, before copying."""
    tools = [*TOOLS, *([] if os.environ.get("XCRAFT_BUILDER_XCRAFT") else ["uv"])]
    script = f"PATH=$HOME/.local/bin:$PATH; for tool in {' '.join(tools)}; do command -v $tool >/dev/null || echo $tool"
    script += "; done"
    rootless = "podman info --format 'rootless {{.Host.Security.Rootless}}' 2>/dev/null || echo rootless failing"
    script += f"; command -v podman >/dev/null && {{ {rootless}; }}"
    found = process.run(host.ssh(script), check=False, capture=True).stdout.split()
    if missing := [word for word in found if word in tools]:
        msg = f"the builder {host.name} lacks {', '.join(missing)}: install them there, or see xcraft doctor --fix"
        raise BuilderError(msg)
    if "false" in found:
        msg = f"podman of the builder {host.name} runs as root, while xcraft runs it rootless: build as another user"
        raise BuilderError(msg)
    if "failing" in found:
        msg = f"podman of the builder {host.name} does not work rootless: see xcraft doctor --fix"
        raise BuilderError(msg)


UV_INSTALLER = "https://astral.sh/uv/install.sh"
"""The installer of uv, in ~/.local/bin, for the user, which a builder without uv gets, see provision()."""


def provision(name: str) -> None:
    """Make the builder `name` able to build, from an SSH account with sudo only: install uv for the user, which runs
    xcraft there, if missing, then run the same xcraft as this process there, xcraft doctor --fix --builder, which
    installs the rest with the package manager of the builder, asking for the password of sudo, and configures podman.
    """
    with tempfile.TemporaryDirectory(prefix="xcraft-builder-") as tmp, connected(ssh_host(name)) as host:
        install = f"curl -LsSf {UV_INSTALLER} | sh || wget -qO- {UV_INSTALLER} | sh"
        with activity(f"installing uv on {name}, if missing"):
            process.run(host.ssh(f"PATH=$HOME/.local/bin:$PATH; command -v uv >/dev/null || {{ {install}; }}"))
            xcraft_command = _xcraft(host, Path(tmp))
        doctor = shlex.join([*xcraft_command, "doctor", "--fix", "--builder"])
        # with a terminal, for the confirmation and sudo; what it could not fix, xcraft doctor tells after
        process.run(
            ["ssh", "-t", *host.options, host.destination, f"PATH=$HOME/.local/bin:$PATH {doctor}"], check=False
        )


def _copy_trees(project: Project, host: Host, remote: str) -> None:
    """Copy the trees the packages of `project` are developed from, see xcraft.develop, to `host`, in the state of its
    copy of the checkout, `remote`, which it keeps, and point its develop.json to them, or remove it without any: with
    the same files and HEAD, the trees are the same commits there, and their builds have the same keys.
    """
    if not (developed := develop.trees(project)):
        process.run(host.ssh(f"rm -f {remote}/.xcraft/develop.json"), capture=True)
        return
    there = process.run(host.ssh(f"cd {remote} && pwd"), capture=True).stdout.strip()
    paths = {name: f"{there}/.xcraft/trees/{name}" for name in developed}
    process.run(host.ssh(shlex.join(["mkdir", "-p", *paths.values()])), capture=True)
    for name, tree in developed.items():
        filters = ["--delete", "--exclude=/.jj/", "--filter=:- .gitignore"]
        _rsync(host, *filters, f"{tree}/", f"{host.destination}:{paths[name]}/")
    with tempfile.TemporaryDirectory(prefix="xcraft-develop-") as tmp:
        (Path(tmp) / "develop.json").write_text(json.dumps(paths, indent=1, sort_keys=True))
        _rsync(host, f"{tmp}/develop.json", f"{host.destination}:{there}/.xcraft/develop.json")


class _Copy(NamedTuple):
    """A copy of a checkout on a builder, see _copied()."""

    host: Host
    remote: str
    """Where the copy is, relative to the home of the user of the builder."""
    prefix: list[str]
    """The command of the xcraft of the builder, with the options of the checkout."""

    @property
    def destination(self) -> str:
        """Return the copy, for rsync."""
        return f"{self.host.destination}:{self.remote}"

    def script(self, *command: str) -> str:
        """Return the shell script running the xcraft `command` in the copy, one command at a time, held with flock,
        which builders have, with util-linux.
        """
        lock = f"../{Path(self.remote).name}.lock"
        locked = f"cd {self.remote} && PATH=$HOME/.local/bin:$PATH XCRAFT_PROJECT=$PWD flock {lock} "
        return locked + shlex.join([*self.prefix, *command])

    def keys(self, *options: str) -> dict:
        """Return what xcraft keys with `options` says in the copy."""
        return json.loads(process.run(self.host.ssh(self.script("keys", *options)), capture=True).stdout)

    def run(self, *command: str) -> int:
        """Run the xcraft `command` in the copy, with a terminal when this process has one, and return its exit
        code.
        """
        tty = ["-t", "-q"] if sys.stdout.isatty() else []
        return process.run(
            ["ssh", *tty, *self.host.options, self.host.destination, self.script(*command)], check=False
        ).returncode


@contextmanager
def _copied(project: Project, name: str) -> Generator[_Copy]:
    """Yield a copy of the checkout of `project` on its builder `name`, with the counters of its development builds
    and the developer's key, once checked that it runs the same xcraft, the one of the same recipe of the builds, see
    xcraft.buildkey.recipe(): another one would make builds of other keys.

    The copy is the working tree, the files git has and those it does not ignore, with .git, for the build IDs, but not
    .xcraft/, which the builder keeps, with its caches, and the trees the packages are developed from, see
    _copy_trees(). Its builds are numbered against the branch of this checkout, when one is named, see
    xcraft.buildid.named(), which the copy may not tell, like a bookmark of jj.
    """
    remote = f"{ROOT}/{project.id}"
    with tempfile.TemporaryDirectory(prefix="xcraft-builder-") as tmp, connected(ssh_host(name)) as host:
        with activity(f"checking {name}"):
            check(host)
        with activity(f"copying the checkout to {name}"):
            # what the builds are copied back from, even before the first one
            process.run(host.ssh(f"mkdir -p {remote}/.xcraft/cache/builds {remote}/.xcraft/logs"), capture=True)
            destination = f"{host.destination}:{remote}"
            filters = ["--delete", "--exclude=/.xcraft/", "--exclude=/.jj/", "--filter=:- .gitignore"]
            _rsync(host, *filters, f"{project.root}/", f"{destination}/")
            if state := [p for p in ("dev-builds.json", "key") if (project.state / p).exists()]:
                _rsync(host, "--relative", *(f"{project.root}/./.xcraft/{p}" for p in state), f"{destination}/")
            _copy_trees(project, host, remote)
            xcraft_command = _xcraft(host, Path(tmp))
        branch = buildid.named(project)
        copy = _Copy(host, remote, [*xcraft_command, *(["--maintenance", branch] if branch else [])])
        with activity(f"checking the xcraft of {name}"):
            if (there := copy.keys("--recipe")["recipe"]) != (here := buildkey.recipe(project)):
                msg = f"{name} runs another xcraft, of the recipe {there[:12]}, rather than {here[:12]}: the same one"
                raise BuilderError(msg)
        yield copy


def build(project: Project, name: str, args: list[str], keys: list[str]) -> tuple[int, dict[str, str]]:
    """Build with xcraft build and its `args` on the builder `name` of the architecture of `project`, from a copy of
    the checkout, see _copied(), and copy back its builds of the architecture, the logs of the builds, the counters
    and xcraft.builds, as they are then: return its exit code, and once built, the cache keys of the builds of the
    packages `keys` there, the `args` of xcraft keys.
    """
    with _copied(project, name) as copy:
        code = copy.run("build", *args)
        with activity(f"copying the builds back from {name}"):
            (project.state / "cache/builds").mkdir(parents=True, exist_ok=True)
            arch = ["--include=/*/", f"--include=/*/{project.arch}/***", "--exclude=*"]
            host, destination = copy.host, copy.destination
            _rsync(
                host,
                "--ignore-missing-args",
                *arch,
                f"{destination}/.xcraft/cache/builds/",
                f"{project.state}/cache/builds/",
            )
            logs = ["--include=[!_]*.log", "--exclude=*"]
            _rsync(host, "--ignore-missing-args", *logs, f"{destination}/.xcraft/logs/", f"{project.state}/logs/")
            _rsync(host, "--ignore-missing-args", f"{destination}/.xcraft/dev-builds.json", f"{project.state}/")
            _rsync(host, "--ignore-missing-args", f"{destination}/xcraft.builds", f"{project.root}/")
        built = copy.keys(*keys)["keys"] if code == 0 and keys else {}
    return code, built


def shell(project: Project, name: str, package: str) -> int:
    """Open xcraft shell for `package` on the builder `name` of the architecture of `project`, in a copy of the
    checkout, see _copied(), and return its exit code: its build tree is the one of the builder, which keeps it.
    """
    with _copied(project, name) as copy:
        return copy.run("shell", package, "--arch", project.arch, "--local")
