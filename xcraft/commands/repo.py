"""xcraft repo."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Annotated

import typer

from xcraft import build as builder
from xcraft import process
from xcraft.cache import release_suffix
from xcraft.commands import Arch, Dev, Final, Packages, dev_ident, load, update_build_ids
from xcraft.devbuilds import dev_suffixes, keep_built
from xcraft.display import activity, console
from xcraft.graph import dependents
from xcraft.signing import check_signatures, dev_signer
from xcraft.specs import is_debug

REMOTE = "/var/local/xcraft"
"""Where the repositories of development builds go on the hosts, one per ID."""
KEY_FILE = "RPM-GPG-KEY-xcraft-test"
"""The developer's key, in the repositories of development builds."""


def _hosts(hosts: list[str] | None) -> list[str] | None:
    """Refuse the hosts ssh and rsync would take for options."""
    if bad := [host for host in hosts or [] if host.startswith("-")]:
        msg = f"not a host: {', '.join(bad)}"
        raise typer.BadParameter(msg)
    return hosts


def _copy_to_host(host: str, output: Path, ident: str, binaries: list[str], *, upgrade: bool) -> None:
    """Copy the repository of development builds in `output` to `host`, with its .repo file, and maybe upgrade
    `binaries`.
    """  # noqa: D205
    remote = f"{REMOTE}/{ident}"
    with activity(f"copying the repository to {host}"):
        process.run(["ssh", host, f"mkdir -p {remote}"], capture=True)
        process.run(["rsync", "-a", "--delete", f"{output}/", f"{host}:{remote}/"], capture=True)
        process.run(["ssh", host, f"install -m 644 {remote}/xcraft-{ident}.repo /etc/yum.repos.d/"], capture=True)
    console.print(f"[bold green]✓[/] repository on {host}, in {remote}")
    if upgrade:
        # only those installed: dnf upgrade fails on the others
        script = (
            f"names=$(rpm -q --qf '%{{NAME}}\\n' {shlex.join(binaries)} | grep -v ' ' || true)\n"
            'if [ -n "$names" ]; then dnf -y upgrade --refresh $names; else echo "none of them is installed"; fi\n'
        )
        process.run(["ssh", host, script])
        query = f"rpm -q --qf '%{{NAME}}-%{{VERSION}}-%{{RELEASE}}.%{{ARCH}}\\n' {shlex.join(binaries)} || true"
        installed = process.run(["ssh", host, query], capture=True).stdout.splitlines()
        for kept, built in _kept(installed, sorted(p.name for p in output.glob("*.rpm"))):
            console.print(f"[yellow]warning[/]: {host} keeps {kept}, not {built}: newer, or held")


def _kept(installed: list[str], rpms: list[str]) -> list[tuple[str, str]]:
    """Return the `installed` packages, as rpm -q gives them, which are not the builds of the repository `rpms`, but
    of the same names, with them: after an upgrade, those installed are newer, or held.
    """  # noqa: D205
    built = {rpm.removesuffix(".rpm").rsplit("-", 2)[0]: rpm.removesuffix(".rpm") for rpm in rpms}
    return sorted(
        (nevra, built[name])
        for nevra in installed
        if " " not in nevra  # not the packages not installed
        and (name := nevra.rsplit("-", 2)[0]) in built
        and nevra != built[name]
    )


def repo(  # noqa: C901, PLR0913, PLR0915, PLR0917
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
            help=f"Test host, as for ssh, to copy the repository of development builds to, in {REMOTE}/<ID>, with "
            "its .repo file. Can be repeated.",
            show_default=False,
            callback=_hosts,
        ),
    ] = None,
    upgrade: Annotated[
        bool, typer.Option("--upgrade", help="Also upgrade, on the hosts, the packages they have installed.")
    ] = False,
    dev: Dev = None,
    final: Final = False,
    arch: Arch = None,
) -> None:
    """Build the given packages, all by default, and what they need, put them in a repository, and check them.

    When the given packages have development builds, see --dev, the repository only has them, to install on test
    hosts over their packages, with a .repo file.
    """
    ident = dev_ident(ctx, dev, final=final)
    if (hosts or upgrade) and not (packages and ident):
        msg = "--host and --upgrade are for the development builds of the given packages"
        raise typer.BadParameter(msg)
    if upgrade and not hosts:
        msg = "--upgrade needs --host"
        raise typer.BadParameter(msg)
    if hosts and baseurl:
        msg = f"with --host, the hosts find the repository in {REMOTE}/<ID>: no --baseurl"
        raise typer.BadParameter(msg)
    project = load(packages, arch=arch)
    names = list(packages or sorted(project.packages))
    if with_dependents:
        names += sorted(dependents(project.deps, names) - set(names))
    with project.lock():
        update_build_ids(project)
        autorev = dev_suffixes(project, ident, advance=True)
        results, keys = builder.build(project, names, autorev=autorev)
        update_build_ids(project)
        keep_built(project, ident, autorev, [r.name for r in results if r.status == "built"])
        if failed := [r.name for r in results if r.failed]:
            msg = f"not built: {', '.join(failed)}"
            raise RuntimeError(msg)
        # the development builds of the given packages, alone, to install over a host's packages
        developed = sorted(set(names) & autorev.keys()) if packages else []
        if hosts and not developed:
            msg = f"no development builds of {', '.join(names)}: they are the main branch's"
            raise typer.BadParameter(msg)
        output = (output or project.root / (f"repo-{ident}" if developed else "repo")).absolute()
        published = developed or [r.name for r in results]
        builder.make_repo(project, published, output, keys, autorev=autorev if developed else None)
    console.print(f"[bold green]✓[/] repository in {output}, with all dependencies satisfied")
    if developed and ident:
        url = baseurl or (f"file://{REMOTE}/{ident}" if hosts else f"file://{output}")
        repo_file = output / f"xcraft-{ident}.repo"
        if signer := dev_signer(project):
            key = signer.public_key()
            rpms = sorted(f.relative_to(output).as_posix() for f in output.glob("*.rpm"))
            check_signatures(project, output, rpms, what="development packages", public_key=key)
            signer.sign_file(output / "repodata/repomd.xml")
            (output / KEY_FILE).write_text(key)
            checks = f"gpgcheck=1\nrepo_gpgcheck=1\ngpgkey={url.rstrip('/')}/{KEY_FILE}\n"
        else:
            console.print("[yellow]the development packages are not signed[/]: see xcraft key create")
            checks = "gpgcheck=0\n"
        repo_file.write_text(
            f"[xcraft-{ident}]\nname=xcraft development builds {ident}\nbaseurl={url}\n"
            f"enabled=1\n{checks}metadata_expire=0\n"
        )
        binaries = [
            n
            for name in developed
            for n in project.packages[name].built_names(release_suffix(keys[name]))
            if not is_debug(n)
        ]
        for host in hosts or []:
            _copy_to_host(host, output, ident, binaries, upgrade=upgrade)
        if not hosts:
            console.print(f"on the hosts, with this directory at {url}:", markup=False)
            console.print(
                f"  copy {repo_file.name} to /etc/yum.repos.d/, then: dnf upgrade --refresh {' '.join(binaries)}",
                markup=False,
            )
