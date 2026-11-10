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
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

import xcraft
from xcraft import buildid, buildkey, process
from xcraft.display import activity
from xcraft.hosts import Host, connected, ssh_host

if TYPE_CHECKING:
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
        wheel = f"wheels/{hashlib.sha256(source.read_bytes()).hexdigest()}/{source.name}"
        process.run(host.ssh(f"mkdir -p {ROOT}/{Path(wheel).parent}"), capture=True)
        _rsync(host, str(source), f"{host.destination}:{ROOT}/{wheel}")
        source = f"../{wheel}"  # from the copy of the checkout, where it runs
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
    script += "; command -v podman >/dev/null && podman info --format 'rootless {{.Host.Security.Rootless}}'"
    found = process.run(host.ssh(script), check=False, capture=True).stdout.split()
    if missing := [word for word in found if word in tools]:
        msg = f"the builder {host.name} lacks {', '.join(missing)}: install them there"
        raise BuilderError(msg)
    if "false" in found:
        msg = f"podman of the builder {host.name} runs as root, while xcraft runs it rootless: build as another user"
        raise BuilderError(msg)


def build(project: Project, name: str, args: list[str], keys: list[str]) -> tuple[int, dict[str, str]]:
    """Build with xcraft build and its `args` on the builder `name` of the architecture of `project`, from a copy of
    the checkout, with the counters of its development builds and the developer's key, and copy back its builds of the
    architecture, the logs of the builds, the counters and xcraft.builds, as they are then: return its exit code, and
    once built, the cache keys of the builds of the packages `keys` there, the `args` of xcraft keys.

    The builder must run the same xcraft, the one of the same recipe of the builds, see xcraft.buildkey.recipe(),
    checked before building: another one would make builds of other keys.

    The copy is the working tree, the files git has and those it does not ignore, with .git, for the build IDs, but not
    .xcraft/, which the builder keeps, with its caches. The builds are numbered against the branch of this checkout,
    when one is named, see xcraft.buildid.named(), which the copy may not tell, like a bookmark of jj.
    """
    remote = f"{ROOT}/{project.id}"
    with tempfile.TemporaryDirectory(prefix="xcraft-builder-") as tmp, connected(ssh_host(name)) as host:
        with activity(f"checking {name}"):
            check(host)
        destination = f"{host.destination}:{remote}"
        with activity(f"copying the checkout to {name}"):
            # what the builds are copied back from, even before the first one
            process.run(host.ssh(f"mkdir -p {remote}/.xcraft/cache/builds {remote}/.xcraft/logs"), capture=True)
            filters = ["--delete", "--exclude=/.xcraft/", "--exclude=/.jj/", "--filter=:- .gitignore"]
            _rsync(host, *filters, f"{project.root}/", f"{destination}/")
            if state := [p for p in ("dev-builds.json", "key") if (project.state / p).exists()]:
                _rsync(host, "--relative", *(f"{project.root}/./.xcraft/{p}" for p in state), f"{destination}/")
            xcraft_command = _xcraft(host, Path(tmp))
        branch = buildid.named(project)
        prefix = [*xcraft_command, *(["--maintenance", branch] if branch else [])]

        def script(*command: str) -> str:
            # one command at a time in the copy, held with flock, which builders have, with util-linux
            locked = f"cd {remote} && PATH=$HOME/.local/bin:$PATH XCRAFT_PROJECT=$PWD flock ../{project.id}.lock "
            return locked + shlex.join([*prefix, *command])

        def keys_there(*options: str) -> dict:
            return json.loads(process.run(host.ssh(script("keys", *options)), capture=True).stdout)

        with activity(f"checking the xcraft of {name}"):
            if (there := keys_there("--recipe")["recipe"]) != (here := buildkey.recipe(project)):
                msg = f"{name} runs another xcraft, of the recipe {there[:12]}, rather than {here[:12]}: the same one"
                raise BuilderError(msg)
        tty = ["-t", "-q"] if sys.stdout.isatty() else []
        code = process.run(["ssh", *tty, *host.options, host.destination, script("build", *args)], check=False)
        with activity(f"copying the builds back from {name}"):
            (project.state / "cache/builds").mkdir(parents=True, exist_ok=True)
            arch = ["--include=/*/", f"--include=/*/{project.arch}/***", "--exclude=*"]
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
            _rsync(host, f"{destination}/xcraft.builds", f"{project.root}/")
        built = keys_there(*keys)["keys"] if code.returncode == 0 and keys else {}
    return code.returncode, built
