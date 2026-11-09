"""xcraft repo."""

from __future__ import annotations

import shlex
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated
from urllib.parse import urlsplit

import typer
from rich.text import Text

from xcraft import build as builder
from xcraft import process, vm
from xcraft.cache import cached_build, release_suffix
from xcraft.commands import Arch, Dev, Final, Key, NoCheck, Packages, dev_ident, prepared_project, sign_builds
from xcraft.display import COMMAND, activity, console
from xcraft.graph import dependents
from xcraft.packages import is_debug
from xcraft.project import Project
from xcraft.signing import KEY_FILE as BUILD_KEY
from xcraft.signing import check_signatures, run_signer

if TYPE_CHECKING:
    from collections.abc import Generator

REMOTE = "/var/local/xcraft"
"""Where the repositories of development builds go on the hosts, one per ID."""
KEY_FILE = "RPM-GPG-KEY-xcraft-test"
"""The developer's key, in the repositories of development builds."""


@dataclass
class _Host:
    """A test host: its `name`, as given, and how ssh reaches it, with the `options` of ssh, like its port."""

    name: str
    destination: str
    options: list[str] = field(default_factory=list)

    def ssh(self, command: str) -> list[str]:
        return ["ssh", *self.options, self.destination, command]


def _host(name: str) -> _Host:
    """Return the host `name`, as ssh takes it, or as ssh://[user@]host[:port], whose port rsync cannot take."""
    if name.startswith("-"):  # which ssh and rsync would take for options
        msg = f"not a host: {name}"
        raise typer.BadParameter(msg)
    if not name.startswith("ssh://"):
        return _Host(name, name)
    url = urlsplit(name)
    try:
        port = url.port
    except ValueError:
        port = -1
    if not url.hostname or url.path or port == -1:
        msg = f"not a host: {name}, as ssh://[user@]host[:port]"
        raise typer.BadParameter(msg)
    user = f"{url.username}@" if url.username else ""
    return _Host(name, f"{user}{url.hostname}", ["-p", str(port)] if port else [])


def _hosts(hosts: list[str] | None) -> list[str] | None:
    """Check the hosts, see _host()."""
    for host in hosts or []:
        _host(host)
    return hosts


def _vm_host(project: Project) -> _Host:
    """Return the VM xcraft boot started for the project, running, by its SSH port: without checking its host key,
    which each installation changes, on the same port of localhost.
    """
    try:
        port = vm.ssh_port(project.state / "vm" / project.arch)
    except RuntimeError as e:
        raise typer.BadParameter(str(e), param_hint="--vm") from None
    return _Host(f"the VM, on port {port}", vm.SSH_DESTINATION, vm.ssh_options(port))


def _kept(installed: list[str], rpms: list[str]) -> list[tuple[str, str]]:
    """Return the `installed` packages, as rpm -q gives them, which are not the builds of the repository `rpms`, but
    of the same names, with them: after an upgrade, those installed are newer, or held.
    """
    built = {rpm.removesuffix(".rpm").rsplit("-", 2)[0]: rpm.removesuffix(".rpm") for rpm in rpms}
    return sorted(
        (nevra, built[name])
        for nevra in installed
        if " " not in nevra  # not the packages not installed
        and (name := nevra.rsplit("-", 2)[0]) in built
        and nevra != built[name]
    )


@contextmanager
def _connected(host: _Host) -> Generator[_Host]:
    """Connect to `host` once, asking for its password, if any, before the activity of the copy, and yield it with
    the options of ssh sharing that connection, which its commands and rsync reuse, closed then.
    """
    # a short path: the socket of the connection is longer still, and Unix sockets are limited to about 100 bytes
    with tempfile.TemporaryDirectory(prefix="xcraft-ssh-") as tmp:
        shared = replace(host, options=[*host.options, "-o", f"ControlPath={tmp}/%C"])
        master = ["-o", "ControlMaster=yes", "-o", "ControlPersist=yes", "-f", "-N", host.destination]
        process.run(["ssh", *shared.options, *master])
        try:
            yield shared
        finally:
            process.run(["ssh", *shared.options, "-O", "exit", host.destination], check=False, capture=True)


def _copy_to_host(host: _Host, output: Path, ident: str, binaries: list[str], *, upgrade: bool) -> None:
    """Copy the repository of development builds in `output` to `host`, with its .repo file, and maybe upgrade
    `binaries`, removing the .repo file then.
    """
    remote = f"{REMOTE}/{ident}"
    with activity(f"copying the repository to {host.name}"):
        process.run(host.ssh(f"mkdir -p {remote}"), capture=True)
        rsh = ["-e", shlex.join(["ssh", *host.options])] if host.options else []
        process.run(["rsync", "-a", "--delete", *rsh, f"{output}/", f"{host.destination}:{remote}/"], capture=True)
        process.run(host.ssh(f"install -m 644 {remote}/xcraft-{ident}.repo /etc/yum.repos.d/"), capture=True)
    console.print(f"[bold green]✓[/] repository on {host.name}, in {remote}")
    if upgrade:
        # those installed, upgraded, dnf upgrade failing on the others, and those obsoleting installed packages,
        # installed, replacing them, like openvswitch3.7 replacing openvswitch, which dnf upgrade does not; then the
        # .repo file removed, whatever happened, for the host to keep its usual repositories only
        ours = " ".join(f"-e {shlex.quote(b)}" for b in binaries)
        script = (
            f"trap 'rm -f /etc/yum.repos.d/xcraft-{ident}.repo' EXIT\n"
            f"names=$(rpm -q --qf '%{{NAME}}\\n' {shlex.join(binaries)} | grep -v ' ' || true)\n"
            f"new=$(for p in {remote}/*.rpm; do\n"
            "  rpm -qp --qf '[%{OBSOLETENAME}\\n]' \"$p\" 2>/dev/null |\n"
            "    xargs -r rpm -q --qf '%{NAME}\\n' | grep -qv ' ' && rpm -qp --qf '%{NAME}\\n' \"$p\" 2>/dev/null\n"
            f"done | grep -xF {ours} || true)\n"
            'if [ -z "$names$new" ]; then echo "none of them is installed, nor obsoletes an installed package"; fi\n'
            'if [ -n "$names" ]; then dnf -y upgrade --refresh $names || exit; fi\n'
            'if [ -n "$new" ]; then dnf -y install --refresh $new; fi\n'
        )
        process.run(host.ssh(script))
        query = f"rpm -q --qf '%{{NAME}}-%{{VERSION}}-%{{RELEASE}}.%{{ARCH}}\\n' {shlex.join(binaries)} || true"
        installed = process.run(host.ssh(query), capture=True).stdout.splitlines()
        for kept, built in _kept(installed, sorted(p.name for p in output.glob("*.rpm"))):
            console.print(f"[yellow]warning[/]: {host.name} keeps {kept}, not {built}: newer, or held")


def _sign_repo(project: Project, output: Path, developed: list[str], keys: dict[str, str]) -> list[str]:
    """Sign the metadata of the repository `output` of the development builds `developed`, with their `keys`, by the
    key of the command, and return the files of the public keys of the repository and of the signers of its packages,
    which keep the signatures of their builds: of the key of the command, or of a former one.
    """
    signer, signers = run_signer(project), {}
    for name in developed:
        build = cached_build(project, name, keys[name])
        rpms = [rpm.name for rpm in (build / "RPMS").rglob("*.rpm")]
        public_key = signers.setdefault(build.parents[2].name, (build / BUILD_KEY).read_text())
        check_signatures(project, output, rpms, what="development packages", public_key=public_key)
    signer.sign_file(output / "repodata/repomd.xml")
    (output / KEY_FILE).write_text(signer.public_key())
    files = [KEY_FILE]
    for fingerprint, public_key in signers.items():
        if fingerprint != signer.key:
            (output / (file := f"{KEY_FILE}-{fingerprint}")).write_text(public_key)
            files.append(file)
    return files


def repo(
    ctx: typer.Context,
    packages: Packages = None,
    with_dependents: Annotated[
        bool, typer.Option("--dependents", help="Also build the packages built against these ones.")
    ] = False,
    output: Annotated[
        Path | None,
        typer.Option(
            help="Directory of the repository. repo/ in the project by default, repo-<ID>/ for development builds."
        ),
    ] = None,
    baseurl: Annotated[
        str | None,
        typer.Option(
            help="Where hosts find the repository of development builds, for its .repo file. Its directory by default."
        ),
    ] = None,
    hosts: Annotated[
        list[str] | None,
        typer.Option(
            "--host",
            help=f"Test host, as for ssh, or as ssh://[user@]host[:port], to copy the repository of development "
            f"builds to, in {REMOTE}/<ID>, with its .repo file. Can be repeated.",
            show_default=False,
            callback=_hosts,
        ),
    ] = None,
    to_vm: Annotated[
        bool,
        typer.Option(
            "--vm",
            help="The VM xcraft boot started for the project, running, as a test host: root on its SSH port.",
        ),
    ] = False,
    upgrade: Annotated[
        bool,
        typer.Option(
            "--upgrade",
            help="Also upgrade, on the hosts, the packages they have installed, or replace those obsoleted.",
        ),
    ] = False,
    dev: Dev = None,
    final: Final = False,
    nocheck: NoCheck = False,
    arch: Arch = None,
    key: Key = None,
) -> None:
    """Build the given packages, all by default, and what they need, put them in a repository, and check them.

    When the given packages have development builds, see --dev, the repository only has them, to install on test
    hosts over their packages, with a .repo file.
    """
    ident = dev_ident(ctx, dev, final=final, nocheck=nocheck)
    if (hosts or to_vm or upgrade) and not (packages and ident):
        msg = "--host, --vm and --upgrade are for the development builds of the given packages"
        raise typer.BadParameter(msg)
    if upgrade and not (hosts or to_vm):
        msg = "--upgrade needs --host or --vm"
        raise typer.BadParameter(msg)
    if (hosts or to_vm) and baseurl:
        msg = f"with --host or --vm, the hosts find the repository in {REMOTE}/<ID>: no --baseurl"
        raise typer.BadParameter(msg)
    project = prepared_project(packages, arch=arch)
    targets = [*map(_host, hosts or []), *([_vm_host(project)] if to_vm else [])]  # before building
    sign_builds(project, key, final=ident is None)
    names = list(packages or sorted(project.packages))
    if with_dependents:
        names += sorted(dependents(project.deps, names) - set(names))
    with project.lock():
        results, keys, autorev = builder.build(project, names, dev=ident, nocheck=nocheck)
        if failed := [r.name for r in results if r.failed]:
            msg = f"not built: {', '.join(failed)}"
            raise RuntimeError(msg)
        # the development builds of the given packages, alone, to install over a host's packages, and for the hosts,
        # the main branch's builds of the others, which it may have in other builds
        developed = sorted(set(names) & autorev.keys()) if packages else []
        if targets and (main := [n for n in names if n not in autorev]):
            console.print(f"[yellow]warning[/]: no development builds of {', '.join(main)}: the main branch's ones")
            developed = sorted(names)
        output = (output or project.root / (f"repo-{ident}" if developed else "repo")).absolute()
        builder.make_repo(project, developed or [r.name for r in results], output, keys, alone=bool(developed))
    console.print(f"[bold green]✓[/] repository in {output}, with all dependencies satisfied")
    if developed and ident:
        url = baseurl or (f"file://{REMOTE}/{ident}" if targets else f"file://{output}")
        repo_file = output / f"xcraft-{ident}.repo"
        gpgkey = " ".join(f"{url.rstrip('/')}/{file}" for file in _sign_repo(project, output, developed, keys))
        repo_file.write_text(
            f"[xcraft-{ident}]\nname=xcraft development builds {ident}\nbaseurl={url}\n"
            f"enabled=1\ngpgcheck=1\nrepo_gpgcheck=1\ngpgkey={gpgkey}\nmetadata_expire=0\n"
        )
        binaries = [
            n
            for name in developed
            for n in project.packages[name].built_names(release_suffix(keys[name]))
            if not is_debug(n)
        ]
        for host in targets:
            with _connected(host) as shared:
                _copy_to_host(shared, output, ident, binaries, upgrade=upgrade)
        if not targets:
            console.print(f"on the hosts, with this directory at {url}:", markup=False)
            console.print(
                Text.assemble(
                    f"  copy {repo_file.name} to /etc/yum.repos.d/, then: ",
                    (f"dnf upgrade --refresh {' '.join(binaries)}", COMMAND),
                )
            )
