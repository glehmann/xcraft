"""The builders: the builds of an architecture made on a remote host of it, natively, over SSH, by xcraft running there
on a copy of the checkout, see docs/remote-builders-design.md.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.metadata
import json
import os
import shlex
import subprocess
import sys
import tempfile
import tomllib
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread
from typing import TYPE_CHECKING, NamedTuple

import watchfiles
from pydantic import BaseModel, ConfigDict

import xcraft
from xcraft import buildid, buildkey, develop, process, remote
from xcraft.cache import cached_builds, release_suffix
from xcraft.display import activity, console
from xcraft.hosts import Host, connected, ssh_host
from xcraft.iso import cached_iso
from xcraft.project import INTERNAL
from xcraft.sources import read_manifest

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator

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
    it, built in `work`, for a version under development, or of a commit of a local repository, which the builder
    cannot reach, or else its version.
    """
    dist = importlib.metadata.distribution("xcraft")
    direct = json.loads(dist.read_text("direct_url.json") or "{}")
    if (vcs := direct.get("vcs_info")) and not direct["url"].startswith("file:"):
        return f"git+{direct['url']}@{vcs['commit_id']}"
    tree = Path(xcraft.__file__).resolve().parents[1]
    if vcs:  # its commit, from the repository
        repo, tree = Path(direct["url"].removeprefix("file://")), work / "tree"
        tree.mkdir()
        archive = process.run_bytes(["git", "-C", str(repo), "archive", vcs["commit_id"]])
        process.run_bytes(["tar", "-x", "-C", str(tree)], archive)
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


def _copy_tree(host: Host, tree: Path, path: str) -> None:
    """Copy the tree `tree` to `path` on `host`, like the checkout: its files git has and those it does not ignore,
    with .git, for the commit of its files, see xcraft.develop.tree().
    """
    _rsync(host, "--delete", "--exclude=/.jj/", "--filter=:- .gitignore", f"{tree}/", f"{host.destination}:{path}/")


def _copy_trees(project: Project, host: Host, remote: str) -> dict[str, str]:
    """Copy the trees the packages of `project` are developed from, see xcraft.develop, to `host`, in the state of its
    copy of the checkout, `remote`, which it keeps, and point its develop.json to them, or remove it without any, and
    return where they are there, by package: with the same files and HEAD, the trees are the same commits there, and
    their builds have the same keys.
    """
    if not (developed := develop.trees(project)):
        process.run(host.ssh(f"rm -f {remote}/.xcraft/develop.json"), capture=True)
        return {}
    there = process.run(host.ssh(f"cd {remote} && pwd"), capture=True).stdout.strip()
    paths = {name: f"{there}/.xcraft/trees/{name}" for name in developed}
    process.run(host.ssh(shlex.join(["mkdir", "-p", *paths.values()])), capture=True)
    for name, tree in developed.items():
        _copy_tree(host, tree, paths[name])
    with tempfile.TemporaryDirectory(prefix="xcraft-develop-") as tmp:
        (Path(tmp) / "develop.json").write_text(json.dumps(paths, indent=1, sort_keys=True))
        _rsync(host, f"{tmp}/develop.json", f"{host.destination}:{there}/.xcraft/develop.json")
    return paths


def _changes(trees: list[Path], stop: Event, *, polling: bool | None) -> Iterator[set[tuple[watchfiles.Change, str]]]:
    """Return the changes of the files of `trees` until `stop`, by batches, polled, or else watched with inotify, unless
    `polling` says: .git too, which the default filter of watchfiles ignores, as a commit changes the archive.
    """
    return watchfiles.watch(
        *trees, watch_filter=None, debounce=300, stop_event=stop, raise_interrupt=False, force_polling=polling
    )


def _copy_changed(
    host: Host, trees: dict[Path, str], paths: dict[str, str], changes: set[tuple[watchfiles.Change, str]]
) -> None:
    """Copy the trees of `changes` to `host`, see _syncing(), warning about those that cannot be."""
    for tree in {t for _, path in changes for t in trees if Path(path).is_relative_to(t)}:
        try:
            _copy_tree(host, tree, paths[trees[tree]])
        except (OSError, subprocess.CalledProcessError) as e:
            console.print(f"[yellow]warning[/]: {tree} not copied to the builder: {e}")


@contextmanager
def _syncing(project: Project, host: Host, paths: dict[str, str]) -> Generator[None]:
    """Copy each tree a package is developed from to its copy on `host`, at `paths`, see _copy_tree(), whenever its
    files change, meanwhile: for the shell of the builder, which archives it again for each rpmbuild, see
    xcraft.rpmbuild.REFRESH. The changes are polled when inotify cannot watch them, like with too many of its
    instances.
    """
    trees, stop = {Path(t).resolve(): name for name, t in develop.trees(project).items() if name in paths}, Event()

    def sync() -> None:
        try:
            for changes in _changes([*trees], stop, polling=None):
                _copy_changed(host, trees, paths, changes)
        except (OSError, RuntimeError):  # RuntimeError: what watchfiles raises without inotify instances left
            for changes in _changes([*trees], stop, polling=True):
                _copy_changed(host, trees, paths, changes)

    thread = Thread(target=sync, daemon=True)
    if trees:
        thread.start()
    try:
        yield
    finally:
        stop.set()
        if trees:
            thread.join()


def _copy_git(project: Project, host: Host, remote: str) -> list[str]:
    """Copy the repository of git of the checkout of `project` to its copy `remote` on `host`, when it is a worktree,
    like a workspace of jj, and return the filters of the copy of the checkout: its .git is then a file naming the
    directory of the worktree, in the repository, which the copy would not have, nor the history the build IDs are
    numbered against. The copy gets the repository, but its worktrees, as its .git, with the HEAD and the index of the
    worktree.
    """
    found = process.run(
        ["git", "-C", str(project.root), "rev-parse", "--absolute-git-dir", "--git-common-dir"],
        check=False,
        capture=True,
    )
    if found.returncode or not (project.root / ".git").is_file():
        return []
    git_dir, common = (Path(project.root, line) for line in found.stdout.splitlines())
    process.run(host.ssh(f"cd {remote} && if [ -f .git ]; then rm .git; fi"), capture=True)
    _rsync(host, "--delete", "--exclude=/worktrees/", f"{common}/", f"{host.destination}:{remote}/.git/")
    _rsync(host, f"{git_dir}/HEAD", f"{git_dir}/index", f"{host.destination}:{remote}/.git/")
    return ["--exclude=/.git"]


class _Copy(NamedTuple):
    """A copy of a checkout on a builder, see _copied()."""

    host: Host
    remote: str
    """Where the copy is, relative to the home of the user of the builder."""
    prefix: list[str]
    """The command of the xcraft of the builder, with the options of the checkout."""
    trees: dict[str, str]
    """Where the trees the packages are developed from are copied there, by package, see _copy_trees()."""

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

    The copy is the working tree, the files git has and those it does not ignore, with .git, for the build IDs, its
    repository for a worktree, see _copy_git(), but not .xcraft/, which the builder keeps, with its caches, and the
    trees the packages are developed from, see _copy_trees(). Its builds are numbered against the branch of this
    checkout, when one is named, see xcraft.buildid.named(), which the copy may not tell, like a bookmark of jj.
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
            filters += _copy_git(project, host, remote)
            _rsync(host, *filters, f"{project.root}/", f"{destination}/")
            if state := [p for p in ("dev-builds.json", "key") if (project.state / p).exists()]:
                _rsync(host, "--relative", *(f"{project.root}/./.xcraft/{p}" for p in state), f"{destination}/")
            trees = _copy_trees(project, host, remote)
            xcraft_command = _xcraft(host, Path(tmp))
        branch = buildid.named(project)
        copy = _Copy(host, remote, [*xcraft_command, *(["--maintenance", branch] if branch else [])], trees)
        with activity(f"checking the xcraft of {name}"):
            if (there := copy.keys("--recipe")["recipe"]) != (here := buildkey.recipe(project)):
                msg = f"{name} runs another xcraft, of the recipe {there[:12]}, rather than {here[:12]}: the same one"
                raise BuilderError(msg)
        yield copy


_THERE = """cd {copy} 2>/dev/null || exit 0
for marker in .xcraft/build/*.turn .xcraft/build/.queue; do
    [ -e "$marker" ] && ! flock -n -s "$marker" true 2>/dev/null &&
        printf '%s\\0%s\\0' "${{marker##*/}}" "$(cat "$marker")"
done
while IFS= read -r build; do
    [ -d "$build" ] && printf 'built\\0%s\\0' "$build"
done
exit 0
"""
"""What a copy has of the builds: the markers of those running, see xcraft.build.building() and queued(), those locked,
read by flock, which builders have, with util-linux, and the builds given on its input, one path by line, which it has,
named built."""


class OnBuilder(NamedTuple):
    """The builds of a copy of the checkout on a builder, see on_builder()."""

    building: set[str]
    """The packages being built."""
    queued: list[str]
    """The packages its running build is still to build, in order."""
    built: set[str]
    """The packages built there, of those asked about."""


def on_builder(project: Project, name: str, builds: dict[str, Path]) -> OnBuilder:
    """Return the builds of the copy of the checkout of `project` on its builder `name`: those running, and those of
    `builds`, the cached builds of packages, which it has, like those of its running build, which come back once all
    are done. From their markers and directories there, without copying the checkout, which would change it under the
    build, nor running xcraft, which would wait for the build to hold the copy.
    """
    copy = f"{ROOT}/{project.id}"
    paths = {str(path.relative_to(project.root)): package for package, path in builds.items()}
    found = process.run(
        ssh_host(name).ssh(_THERE.format(copy=shlex.quote(copy))), capture=True, input="".join(f"{p}\n" for p in paths)
    ).stdout.split("\0")
    pairs = list(zip(found[0:-1:2], found[1::2], strict=True))
    markers = dict(pairs)
    return OnBuilder(
        {m.removesuffix(".turn") for m in markers if m.endswith(".turn")},
        markers.get(".queue", "").split(),
        {paths[path] for key, path in pairs if key == "built" and path in paths},
    )


def _copy_back(project: Project, name: str, copy: _Copy, logs_there: str) -> None:
    """Copy back from the copy of the checkout on the builder `name` its builds of the architecture, the logs of the
    builds it made, the counters and xcraft.builds, as they are then.

    The logs are those written since the start of the command, by their times there: not those of earlier builds
    there, which would replace newer ones here, like those of another architecture.
    """
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
        written = f"cd {logs_there} && find . -maxdepth 1 -name '[!_.]*.log' -newer .run -printf '%f\\n'"
        logs = [f"--include=/{log}" for log in process.run(host.ssh(written), capture=True).stdout.split()]
        _rsync(host, *logs, "--exclude=*", f"{destination}/.xcraft/logs/", f"{project.state}/logs/")
        _rsync(host, "--ignore-missing-args", f"{destination}/.xcraft/dev-builds.json", f"{project.state}/")
        _rsync(host, "--ignore-missing-args", f"{destination}/xcraft.builds", f"{project.root}/")


_MISSING = """mkdir -p {cache} {incoming} && cd {cache} || exit 1
while IFS= read -r path; do
    [ -e "$path" ] || printf '%s\\n' "$path"
done
"""
"""Which of the paths given on its input, one by line, relative to a cache of a copy, it lacks, with the incoming
directory of the cache made."""

_PLACE = """incoming=$PWD/{incoming}
cd {cache} || exit 1
while IFS= read -r path; do
    [ -e "$path" ] || {{ mkdir -p "${{path%/*}}" && mv "$incoming/$path" "$path"; }}
done
rm -rf "$incoming"
"""
"""Move the paths given on its input, sent to the incoming directory of a cache of a copy, into the cache, whole, once
all came: a directory in the cache is a finished build, or an archive, by its SHA-256."""


def _lacking(copy: _Copy, cache: str, paths: list[str]) -> list[str]:
    """Return those of the `paths` of the cache `cache` the copy `copy` lacks, with its incoming directory made, for
    _send().
    """
    there = f"{copy.remote}/.xcraft/cache"
    script = _MISSING.format(cache=shlex.quote(f"{there}/{cache}"), incoming=shlex.quote(f"{there}/incoming/{cache}"))
    return process.run(copy.host.ssh(script), capture=True, input="".join(f"{p}\n" for p in paths)).stdout.split()


def _send(project: Project, name: str, copy: _Copy, cache: str, paths: list[str], what: str) -> None:
    """Send to the copy of the checkout on the builder `name` the `paths` of its cache `cache` here, which it lacks,
    `what` they are, like builds: to an incoming directory, then moved into the cache once all came.
    """
    with activity(f"sending {len(paths)} {what} to {name}"):
        here = project.state / "cache" / cache
        _rsync(
            copy.host,
            "--relative",
            *(f"{here}/./{p}" for p in paths),
            f"{copy.destination}/.xcraft/cache/incoming/{cache}/",
        )
        there = f"{copy.remote}/.xcraft/cache"
        script = _PLACE.format(cache=shlex.quote(f"{there}/{cache}"), incoming=shlex.quote(f"{there}/incoming/{cache}"))
        process.run(copy.host.ssh(script), capture=True, input="".join(f"{p}\n" for p in paths))


def _send_builds(project: Project, name: str, copy: _Copy, dev: str) -> None:
    """Send to the copy of the checkout on the builder `name` the development builds of `dev` here, of the keys of the
    packages there, which it lacks, like those of the dependencies made here, or brought back from another copy,
    rather than make them again there: the remote storage never has them.
    """
    keys = copy.keys("--dev", dev, "--arch", project.arch)["keys"]
    cache = project.state / "cache/builds"
    builds = [
        str(path.relative_to(cache))
        for package, key in keys.items()
        if "~" in release_suffix(key)
        for path in cached_builds(project, package, key)
    ]
    if builds and (lacking := _lacking(copy, "builds", builds)):
        _send(project, name, copy, "builds", lacking, "builds")


def _send_sources(project: Project, name: str, copy: _Copy) -> None:
    """Send to the copy of the checkout on the builder `name` the source archives of the packages here which it lacks
    and the remote storage does not have, like those of the packages imported here, from git LFS, not pushed yet: it
    downloads the others.
    """
    archives = sorted(
        {
            f"{digest}/{filename}"
            for directory in project.package_dirs.values()
            if (manifest := directory / "sources").exists()
            for filename, digest in read_manifest(manifest).items()
            if (project.source_cache / digest / filename).exists()
        }
    )
    if not archives or not (lacking := _lacking(copy, "sources", archives)):
        return
    stored: set[str] = set()
    for storage in remote.storages(project):
        with contextlib.suppress(remote.RemoteError, OSError):  # unreachable: sent
            stored |= remote.existing(storage, [f"{INTERNAL}/sources/{a}" for a in lacking])
    if unstored := [a for a in lacking if f"{INTERNAL}/sources/{a}" not in stored]:
        _send(project, name, copy, "sources", unstored, "source archives")


def _started(copy: _Copy) -> str:
    """Mark the start of a command in the copy `copy`, for _copy_back(), and return the directory of its logs."""
    logs_there = f"{copy.remote}/.xcraft/logs"
    process.run(copy.host.ssh(f"touch {logs_there}/.run"), capture=True)
    return logs_there


def build(project: Project, name: str, args: list[str], keys: list[str], dev: str) -> tuple[int, dict[str, str]]:
    """Build with xcraft build and its `args` on the builder `name` of the architecture of `project`, from a copy of
    the checkout, see _copied(), with the development builds of `dev` here, see _send_builds(), and copy back what it
    made, see _copy_back(): return its exit code, and once built, the cache keys of the builds of the packages `keys`
    there, the `args` of xcraft keys.
    """
    with _copied(project, name) as copy:
        _send_sources(project, name, copy)
        _send_builds(project, name, copy, dev)
        logs_there = _started(copy)
        code = copy.run("build", *args)
        _copy_back(project, name, copy, logs_there)
        built = copy.keys(*keys)["keys"] if code == 0 and keys else {}
    return code, built


def iso(project: Project, name: str, args: list[str], key: Callable[[], str], dev: str) -> int:
    """Make the ISO with xcraft iso and its `args` on the builder `name` of the architecture of `project`, from a copy
    of the checkout, see _copied(), with the development builds of `dev` here, see _send_builds(), natively, rather
    than with the programs of its packages emulated here, and return
    its exit code: copy back the builds it made, see _copy_back(), and its lock, which it may pin the ISO's packages
    in, then the cached ISO of the `key` of the ISO of the checkout here, computed with them.
    """
    with _copied(project, name) as copy:
        _send_sources(project, name, copy)
        _send_builds(project, name, copy, dev)
        logs_there = _started(copy)
        code = copy.run("iso", *args)
        _copy_back(project, name, copy, logs_there)
        _rsync(copy.host, f"{copy.destination}/xcraft.lock", f"{project.root}/")
        if code == 0:
            tree = cached_iso(project, made := key())
            tree.parent.mkdir(parents=True, exist_ok=True)
            with activity(f"copying the ISO back from {name}"):
                try:
                    _rsync(copy.host, f"{copy.destination}/.xcraft/cache/isos/{project.arch}/{made}/", f"{tree}/")
                except subprocess.CalledProcessError as e:
                    msg = f"the ISO did not come back from {name}, of another key there: {made}"
                    raise BuilderError(msg) from e
    return code


def reproduce(project: Project, name: str, args: list[str]) -> int:
    """Build packages again with xcraft reproduce and its `args` on the builder `name` of the architecture of
    `project`, from a copy of the checkout, see _copied(), and return its exit code: copy back the builds it made
    first, see _copy_back(), not the rebuilds, which stay there.
    """
    with _copied(project, name) as copy:
        logs_there = _started(copy)
        code = copy.run("reproduce", *args)
        _copy_back(project, name, copy, logs_there)
    return code


def shell(project: Project, name: str, package: str) -> int:
    """Open xcraft shell for `package` on the builder `name` of the architecture of `project`, in a copy of the
    checkout, see _copied(), and return its exit code: its build tree is the one of the builder, which keeps it. The
    trees the packages are developed from are copied there again whenever they change, see _syncing().
    """
    with _copied(project, name) as copy, _syncing(project, copy.host, copy.trees):
        return copy.run("shell", package, "--arch", project.arch, "--local")
