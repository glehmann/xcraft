"""The build environment: a container image made only of the configured repositories, run with podman.

The image is built from the configured base image, with its repositories
replaced by the configured ones, and the versions of the lock. It is tagged
with a hash of its definition, so that it is built again only when the
configuration or the locked versions of its packages change. Its ``builder``
user is mapped to the user running xcraft, and can use sudo.

In the containers, dnf only sees the locked version of a locked package.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING

from xcraft import host
from xcraft.display import activity
from xcraft.lock import Lock, Locked, load_lock, save_lock
from xcraft.project import ProjectLockedError

if TYPE_CHECKING:
    from collections.abc import Callable

    from xcraft.project import Project

MAX_LOG_SIZE = 256 * 2**20
"""Size of log after which a container is stopped: the largest build logs are under 10 MiB."""

BUILDENV = Path(__file__).parent / "buildenv"
"""The Containerfile of the image, and the scripts run in the containers, which have them in /xcraft."""
DNF_LOCK = "sudo flock /var/cache/dnf/.xcraft.lock"
"""Prefix of the dnf commands: the containers share the dnf cache volume, which dnf does not lock across them."""


class ContainerError(RuntimeError):
    """Raised when a command fails in the build environment."""


def _image_files(project: Project, lock: Lock) -> dict[str, str]:
    config = project.config
    repos = "".join(
        f"[{name}]\nname={name}\nbaseurl={repo.baseurl.format(arch=project.arch)}\n"
        + (f"gpgcheck=1\ngpgkey={repo.gpgkey}\n" if repo.gpgkey else "gpgcheck=0\n")
        + (f"includepkgs={' '.join(repo.includepkgs)}\n" if repo.includepkgs else "")
        for name, repo in project.repos.items()
    )
    return {
        "Containerfile": (BUILDENV / "Containerfile").read_text(),
        "upstream.repo": repos,
        "macros.xcraft": "".join(f"%{name} {value}\n" for name, value in config.macros.items()),
        "versionlock.list": lock.versionlock([name for name in lock.image if name in lock.packages]),
    }


def _image(project: Project) -> tuple[str, dict[str, str], dict[str, str]]:
    """Return the tag of the build environment image, with its build args and files.

    The tag is a hash of its definition, which pins the versions of its packages
    once they are locked, so it identifies the image across machines.
    """
    config = project.config
    args = {"BASE_IMAGE": config.base_image, "PACKAGES": " ".join(config.packages)}
    files = _image_files(project, load_lock(project))
    digest = hashlib.sha256(json.dumps([project.platform, args, files], sort_keys=True).encode()).hexdigest()
    return f"localhost/xcraft-build:{digest}", args, files


def image_tag(project: Project) -> str:
    """Return the tag of the build environment image, built or not."""
    return _image(project)[0]


def _exists(tag: str) -> bool:
    return not subprocess.run(["podman", "image", "exists", tag], check=False).returncode


def build_image(project: Project) -> str:
    """Return the tag of the build environment image, building it if needed.

    The packages of a new image are added to the lock, and when that changes
    its definition, the image is built again, with them locked.
    """
    if not project.native and (problem := host.emulation_problem(project.arch)):
        raise ContainerError(problem)
    tag, _, _ = _image(project)
    if _exists(tag) and load_lock(project).image:
        return tag
    try:
        with project.lock():
            return _build_image(project)
    except ProjectLockedError as e:
        msg = f"{e}, and the build environment image must be built first: try again when it is done"
        raise ProjectLockedError(msg) from None


def _build_image(project: Project) -> str:
    for _ in range(3):
        tag, args, files = _image(project)
        if not _exists(tag):
            _podman_build(project, "image", tag, files, args, show="the build environment image")
        elif load_lock(project).image:
            return tag
        # a new image, or an image built for another project, before it had a lock
        installed = image_packages(project, tag)
        lock = load_lock(project)
        lock.merge(installed)
        lock.image = sorted(installed)
        save_lock(project, lock)
        if _image(project)[0] == tag:
            return tag
    msg = "the build environment image does not get the locked versions"
    raise ContainerError(msg)


def _podman_build(  # noqa: PLR0913
    project: Project, step: str, tag: str, files: dict[str, str], args: dict[str, str], *, show: str
) -> None:
    """Build `show`, the image `tag`, from `files` and the build `args`, logged in .xcraft/logs/_<step>.log."""
    _warm_dnf_cache(project)
    with project.work(step) as context:
        for name, content in files.items():
            (context / name).write_text(content)
        build_args = [arg for name, value in args.items() for arg in ("--build-arg", f"{name}={value}")]
        # the metadata of the dnf cache, which the containers solved, slow to solve again under emulation, in an
        # overlay, in which the build changes nothing
        if subprocess.run(["podman", "volume", "exists", volume := _dnf_cache(project)], check=False).returncode:
            subprocess.run(["podman", "volume", "create", volume], check=True, capture_output=True)
        inspect = ["podman", "volume", "inspect", "--format", "{{.Mountpoint}}", volume]
        cache = subprocess.run(inspect, check=True, capture_output=True, text=True).stdout.strip()
        cmd = ["podman", "build", "--platform", project.platform, "--volume", f"{cache}:/var/cache/dnf:O", *build_args]
        cmd += ["--tag", tag, str(context)]
        log = project.state / f"logs/_{step}.log"
        with activity(f"building {show}") as on_line:
            if _run_logged(cmd, log, on_line, None):
                msg = f"{show} cannot be built, see {log}"
                raise ContainerError(msg)


def _dnf_cache(project: Project) -> str:
    """Return the volume of the dnf cache, which the containers of the platform share."""
    return "xcraft-dnf-" + re.sub(r"[^a-z0-9]+", "-", project.platform)


_warmed: set[tuple[Path, str]] = set()


def _warm_dnf_cache(project: Project) -> None:
    """Solve the metadata of the repositories of an emulated platform in its dnf cache, natively, once.

    Under emulation, dnf spends most of its time solving them: 27 minutes for aarch64 on an x86_64 host, rather than
    20 seconds. Nothing is done without a native platform, to do it on.
    """
    native = next((p for p in project.all_platforms() if p.native), None)
    if project.native or not native or (project.root, project.platform) in _warmed:
        return
    _warmed.add((project.root, project.platform))
    cache = "/var/cache/dnf-emulated"
    with project.work("warm") as work:
        (work / "upstream.repo").write_text(_image_files(project, load_lock(project))["upstream.repo"])
        run(
            native,
            "warm",
            f"sudo flock {cache}/.xcraft.lock dnf -q makecache --forcearch={project.arch} --setopt=reposdir=/work"
            f" --setopt=cachedir={cache} --setopt=system_cachedir={cache}",
            volumes=[f"{_dnf_cache(project)}:{cache}", f"{work}:/work:ro"],
            show=f"solving the metadata of the repositories of {project.arch}",
        )


def image_packages(project: Project, image: str) -> dict[str, Locked]:
    """Return the upstream packages installed in `image`."""
    return _record(project, image, [])


def latest_versions(project: Project, names: list[str]) -> dict[str, Locked]:
    """Return the latest versions of the upstream packages `names`."""
    return _record(project, build_image(project), names)


def _record(project: Project, image: str, names: list[str]) -> dict[str, Locked]:
    with project.work("record") as work:
        run(
            project,
            "record",
            f"{DNF_LOCK} python3 /xcraft/record.py /work/record.json {' '.join(names)}",
            volumes=[f"{work}:/work"],
            show="looking for the latest packages" if names else "recording the packages of the image",
            image=image,
        )
        return {name: Locked.model_validate(p) for name, p in json.loads((work / "record.json").read_text()).items()}


def run(  # noqa: PLR0913
    project: Project,
    step: str,
    script: str,
    *,
    volumes: list[str] = [],  # noqa: B006
    show: str = "",
    on_line: Callable[[str], None] | None = None,
    network: bool = True,
    image: str = "",
) -> None:
    """Run a bash `script` for the `step` of xcraft, in a container, logged in .xcraft/logs/_<step>.log.

    `show` is shown while it runs, with the last line of the log, or the lines
    are given to `on_line`. See run_container() for the rest.
    """
    with activity(show) if show else nullcontext(on_line) as progress:
        run_container(
            project,
            script,
            container=f"xcraft-{step}-{os.getpid()}",
            volumes=volumes,
            network=network,
            log=project.state / f"logs/_{step}.log",
            on_line=progress,
            image=image,
        )


_SIGN_PACKAGES = ["rpm-sign", "gnupg2"]


def signing_image(project: Project) -> str:
    """Return the tag of the signing image, the build environment's with rpmsign and gpg, building it if needed.

    It is apart from the build environment image, so as not to change it, and the cache keys with it.
    """
    base = build_image(project)
    tag = f"localhost/xcraft-sign:{hashlib.sha256(json.dumps([base, _SIGN_PACKAGES]).encode()).hexdigest()[:16]}"
    if not _exists(tag):
        # the versions of rpm-sign follow rpm's, which the lock may not have anymore
        containerfile = (
            f"FROM {base}\nUSER root\nRUN dnf -y --disableplugin=versionlock install {' '.join(_SIGN_PACKAGES)}"
            " && dnf clean all\nUSER builder\n"
        )
        _podman_build(project, "sign-image", tag, {"Containerfile": containerfile}, {}, show="the signing image")
    return tag


def run_container(  # noqa: PLR0913
    project: Project,
    script: str,
    *,
    container: str,
    volumes: list[str],
    network: bool = True,
    log: Path | None = None,
    on_line: Callable[[str], None] | None = None,
    image: str = "",
) -> None:
    """Run a bash `script` in a container called `container`, of the build environment image, or of `image`.

    The volumes are mounted with the scripts of the build environment, in
    /xcraft, the versionlock of the lock, and a volume keeping the dnf cache
    between containers. The output goes to `log`, or to an interactive terminal
    when there is no log, and `on_line` is called with each line written to the
    log. The container is stopped when interrupted, or when its log gets larger
    than MAX_LOG_SIZE.
    """
    _warm_dnf_cache(project)
    image = image or build_image(project)
    versionlock = project.state / "tmp" / f"versionlock-{os.getpid()}.list"
    versionlock.parent.mkdir(parents=True, exist_ok=True)
    versionlock.write_text(load_lock(project).versionlock())
    volumes = [*volumes, f"{BUILDENV}:/xcraft:ro", f"{versionlock}:/etc/dnf/plugins/versionlock.list:ro"]
    cache = _dnf_cache(project)
    cmd = [
        # replacing a container of a killed xcraft
        "podman", "run", "--rm", "--replace", "--name", container, "--platform", project.platform,
        "--userns=keep-id:uid=1000,gid=1000", "--security-opt", "label=disable",
        *([] if network else ["--network", "none"]), *(["--interactive", "--tty"] if log is None else []),
        *(arg for volume in [f"{cache}:/var/cache/dnf", *volumes] for arg in ("--volume", volume)),
        image, "bash", "-c", script,
    ]  # fmt: skip
    try:
        returncode = (
            subprocess.run(cmd, check=False).returncode if log is None else _run_logged(cmd, log, on_line, container)
        )
    except BaseException:
        _kill(container)
        raise
    if returncode:
        msg = "failed" + (f", see {log}" if log else "")
        raise ContainerError(msg)


def _kill(container: str) -> None:
    # bash, the init process of the container, ignores the signals stopping podman: killed, and removed at once,
    # not later by --rm, as another container of the same name may follow
    subprocess.run(["podman", "rm", "--force", "--time", "0", container], capture_output=True, check=False)


def _run_logged(cmd: list[str], log: Path, on_line: Callable[[str], None] | None, container: str | None) -> int:
    """Run `cmd` with its output in `log`, stopping `container` when the log gets too large."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with (
        log.open("w", buffering=1) as f,  # line buffered, for xcraft log --follow
        subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace") as proc,
    ):
        assert proc.stdout  # noqa: S101
        for line in proc.stdout:
            f.write(line)
            if f.tell() > MAX_LOG_SIZE and container:
                _kill(container)  # before leaving the block, which waits for podman
                msg = f"stopped, its log got larger than {MAX_LOG_SIZE // 2**20} MiB, see {log}"
                raise ContainerError(msg)
            if on_line:
                on_line(line)
    return proc.returncode
