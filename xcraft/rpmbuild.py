"""The build trees of the packages, laid out as packaging repositories, and rpmbuild in them."""

from __future__ import annotations

import os
import shlex
import shutil
from typing import TYPE_CHECKING

from xcraft import ccache
from xcraft.cache import BuildError, build_keys, deps_repo
from xcraft.container import DNF_LOCK, run_container
from xcraft.localrepo import LocalRepo, link, repo_id
from xcraft.resolution import Resolved, resolve
from xcraft.sources import fetch_sources, read_manifest
from xcraft.specs import STAGED_TIME, hash_dir, main_spec

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from xcraft.project import Project


def prepare_tree(project: Project, name: str, expected: str | None = None) -> Path:
    """Return a fresh build tree of `name`, laid out as a packaging repository, of the files whose hash, see
    specs.hash_dir(), is `expected`, if given.

    The main spec is in SPECS/, everything else in SOURCES/, with the archives
    from the source cache, downloaded when missing (see fetch_sources). The
    files have the modes 0644, or 0755 for those their owners may run, and
    0444 for the archives, and all of them, directories included, have the
    time STAGED_TIME: the same trees for the same hash.
    """
    pkg_dir = project.package_dirs[name]
    spec = main_spec(pkg_dir)
    tree = project.state / "build" / name
    shutil.rmtree(tree, ignore_errors=True)
    for directory in ("SPECS", "SOURCES"):
        (tree / directory).mkdir(parents=True)

    def stage(path: str, data: bytes, executable: bool) -> None:  # noqa: FBT001, as hash_dir() gives them
        if path == "sources":  # xcraft's, for the archives
            return
        dest = tree / ("SPECS" if path == spec.name else "SOURCES") / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        dest.chmod(0o755 if executable else 0o644)

    staged = hash_dir(pkg_dir, project.root, stage)
    if expected is not None and staged != expected:
        msg = f"{pkg_dir.relative_to(project.root)} changed since the builds started, build it again"
        raise BuildError(msg)
    if missing := fetch_sources(project, name):
        msg = f"{', '.join(missing)} is not in the source cache, the remote storage, nor at a URL of the spec"
        raise BuildError(msg)
    if (pkg_dir / "sources").exists():
        for filename, digest in read_manifest(pkg_dir / "sources").items():
            archive = project.source_cache / digest / filename
            # read-only: the build tree has it hard-linked, and could change it
            archive.chmod(0o444)
            link(archive, tree / "SOURCES" / filename)
    for path in [tree, *tree.rglob("*")]:
        os.utime(path, (STAGED_TIME, STAGED_TIME))
    return tree


def run_build(
    project: Project,
    name: str,
    repo: LocalRepo,
    rpmbuild_stage: str,
    *,
    replace: list[str],
    suffix: str = "",
    after: str = "",
    log: Path | None = None,
    on_line: Callable[[str], None] | None = None,
) -> None:
    """Run rpmbuild `rpmbuild_stage` (a: all, p: prep...) in the build tree of `name`, then the `after` commands.

    The upstream packages of the image named in `replace` are replaced first by
    ours. `suffix` is the release suffix of the build, %autorev: its number, and its development build suffix, if
    any. The output goes to
    `log`, or to an interactive terminal without log.
    """
    parameters = {
        "spec": main_spec(project.package_dirs[name]).name,
        "stage": rpmbuild_stage,
        "repo": repo_id(project),
        "lock": DNF_LOCK,
        "autorev": suffix,
        "replace": " ".join(replace),
    }
    script = "".join(f"{p}={shlex.quote(value)}\n" for p, value in parameters.items()) + f". /xcraft/build.sh\n{after}"
    if size := project.config.ccache_size.get(name):
        # not in the recipe of the builds: it does not change them
        script = f"export CCACHE_MAXSIZE={shlex.quote(size)}\n{script}"
    cache = ccache.local_path(project, name)
    cache.mkdir(parents=True, exist_ok=True)
    tree = project.state / "build" / name
    volumes = [f"{tree}:/build", f"{cache}:/ccache", *repo.volumes()]
    run_container(project, script, container=f"xcraft-{project.id}-{name}", volumes=volumes, log=log, on_line=on_line)


def image_replacements(resolved: Resolved, name: str) -> list[str]:
    """Return the upstream packages of the image to replace by ours in the `resolved` build root of `name`."""
    return sorted(set(resolved.ours.get(name, [])) & set(resolved.image))


def shell(project: Project, name: str) -> None:
    """Open a shell in the build tree of `name`, with its build dependencies installed and sources prepared.

    rpmbuild works on the build tree there, like in the builds: rpmbuild -ba SPECS/<spec> builds it again, shown before
    the prompt. The build tree is the one of the host, in .xcraft/build/, where its files can be edited, and its
    packages are. Its dependencies are their development builds, as last built, if any.
    """
    from xcraft.devbuilds import built_dev_suffixes  # noqa: PLC0415, which imports the builds

    repo = deps_repo(project, name, build_keys(project, built_dev_suffixes(project)))
    tree = prepare_tree(project, name)
    replace = image_replacements(resolve(project), name)
    spec = main_spec(project.package_dirs[name]).name
    # the labels dim, the path bold, the command bold cyan: on a terminal, without NO_COLOR, like xcraft's console
    rows = [("build tree", f"{tree}, on the host", "bold"), ("to build", f"rpmbuild -ba SPECS/{spec}", "cyan")]
    after = (
        "echo '%_topdir /build' > ~/.rpmmacros"
        """ && echo 'alias rpmbuild="python3 /xcraft/offline.py rpmbuild"' >> ~/.bashrc && cd /build"""
        ' && dim= bold= cyan= off= && if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then'
        " dim=$'\\e[2m' bold=$'\\e[0;1m' cyan=$'\\e[0;1;36m' off=$'\\e[0m'; fi"
        + "".join(
            f' && printf "%s%-12s%s%s%s\\n" "$dim" {shlex.quote(label)} "${style}" {shlex.quote(value)} "$off"'
            for label, value, style in rows
        )
        + " && exec bash -l"
    )
    run_build(project, name, repo, "p", replace=replace, after=after)
