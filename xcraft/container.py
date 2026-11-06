"""The build environment: a container image made only of the configured repositories, run with podman.

The image is built from the configured base image, without its repositories, with the packages resolved from the
lock (see xcraft.resolution). It is tagged with a hash of its definition, so that it is
built again only when the configuration or the versions of its packages change.
Its ``builder`` user is mapped to the user running xcraft, and can use sudo.

In the containers, dnf only sees the pinned upstream packages, in their repository, see xcraft.pinned, and ours: the
image, the builds, and xcraft shell install from them only.

The resolution runs in the base image of the host, natively, for any platform.
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

from xcraft import host, pinned, process
from xcraft.display import activity, changes, read_tail
from xcraft.lock import Locked, cached_lock
from xcraft.project import ProjectLockedError, write_atomically

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from xcraft.lock import Pin
    from xcraft.project import Project

MAX_LOG_SIZE = 256 * 2**20
"""Size of log after which a container is stopped: the largest build logs are under 10 MiB."""

_LINE = 2**16
"""The longest part of a line of a log read at once."""

BUILDENV = Path(__file__).parent / "buildenv"
"""The Containerfile of the image, and the scripts run in the containers, which have them in /xcraft."""

BUILD_IMAGE, SIGN_IMAGE = "localhost/xcraft-build", "localhost/xcraft-sign"
"""The names of the build environment images and of the signing images, tagged by what they are made of."""


def buildenv_hash() -> str:
    """Return a hash of the files of BUILDENV, but the bytecode Python leaves there, which differs by checkout."""
    h = hashlib.sha256()
    for path in sorted(p for p in BUILDENV.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        h.update(f"{path.relative_to(BUILDENV)}\0".encode())
        h.update(hashlib.sha256(path.read_bytes()).digest())
    return h.hexdigest()


DNF_LOCK = "sudo flock /var/cache/dnf/.xcraft.lock"
"""Prefix of the dnf commands: the containers share the dnf cache volume, which dnf does not lock across them."""


PACKAGES = ["dnf-plugins-core", "createrepo_c", "sudo"]
"""Packages of the build environment used by xcraft, after those of the configuration: dnf builddep and repoclosure,
the indexes of the local repositories, and dnf as root. The base image has dnf and its rpm python module."""


class ContainerError(RuntimeError):
    """Raised when a command fails in the build environment."""


def _tail(log: Path) -> str:
    """Return the end of the `log` of a failed step, to show with its error: in CI, the log is out of reach."""
    return ", which ends with:" + "".join(f"\n  {line}" for line in read_tail(log, 2**14)[-15:]) if log.exists() else ""


def repo_file(project: Project) -> str:
    """Return the definition of the repositories of the build environment of the platform."""
    return "".join(
        f"[{name}]\nname={name}\nbaseurl={repo.baseurl.format(arch=project.arch)}\n"
        + (f"gpgcheck=1\ngpgkey={repo.gpgkey}\n" if repo.gpgkey else "gpgcheck=0\n")
        + (f"includepkgs={' '.join(sorted(repo.includepkgs))}\n" if repo.includepkgs else "")
        # read again each time, when local, like those changed between two commands
        + ("metadata_expire=0\n" if repo.baseurl.startswith("file://") else "")
        for name, repo in sorted(project.repos.items())  # in an order of their own, which the build keys cover
    )


def _with_host(project: Project) -> list[Project]:
    """Return the platforms of the project, and the host's, whose image parses the specs, when it is none of them."""
    return [*project.all_platforms(), project.on_host()]


def _local_repos(project: Project) -> list[str]:
    """Return the volumes of the repositories on this machine, and of their sources, of file:// URLs, at their paths
    in the containers, of all the platforms, and of the host's, whose image parses the specs.
    """
    return sorted(
        {
            f"{path}:{path}:ro"
            for platform in _with_host(project)
            for repo in platform.repos.values()
            for url in (repo.baseurl.format(arch=platform.arch), repo.source_baseurl)
            if url.startswith("file:///")
            for path in [url.removeprefix("file://").rstrip("/")]
        }
    )


def _image_pins(project: Project) -> dict[str, Pin]:
    """Return the pinned binary packages of the image of the platform."""
    pins = pinned.pins(project)
    return {name: pins[name] for name in cached_lock(project).image.get(project.arch, [])}


def _image_files(project: Project) -> dict[str, str]:

    return {
        "Containerfile": (BUILDENV / "Containerfile").read_text(),
        "pinned.repo": pinned.REPO_FILE,
        # in the image before its packages are installed, and so in its tag: another image for other pins, rather than a
        # layer of the cache of podman
        "pins.list": "".join(f"{name} {pin.text()}\n" for name, pin in sorted(_image_pins(project).items())),
        "macros.xcraft": "".join(f"%{name} {value}\n" for name, value in sorted(project.config.macros.items())),
    }


def _image_definition(project: Project) -> tuple[str, dict[str, str], dict[str, str]]:
    """Return the tag of the build environment image, with its build args and files.

    The tag is a hash of its definition, which pins the versions of its packages,
    so it identifies the image across machines.
    """
    config = project.config
    args = {"BASE_IMAGE": config.base_image, "PACKAGES": " ".join(sorted({*config.packages, *PACKAGES}))}
    files = _image_files(project)
    digest = hashlib.sha256(json.dumps([project.platform, args, files], sort_keys=True).encode()).hexdigest()
    return f"{BUILD_IMAGE}:{digest}", args, files


def image_tag(project: Project) -> str:
    """Return the tag of the build environment image, built or not."""
    return _image_definition(project)[0]


def checkout_images(project: Project) -> set[str]:
    """Return the tags of the images of the checkout, built or not: the build environment images of its platforms and
    of the host, and their signing images.
    """
    tags = {image_tag(p) for p in _with_host(project)}
    return tags | {_signing_tag(tag) for tag in tags}


def checkout_repositories(project: Project) -> set[str]:
    """Return the repositories of pinned packages of the checkout, see pinned.repository(): of all the pinned packages
    of its platforms and of the host, and of those of their images.
    """
    return {pinned.ident(pins) for p in _with_host(project) for pins in (pinned.pins(p), _image_pins(p))}


_existing: set[str] = set()


def _exists(tag: str) -> bool:
    if tag in _existing:
        return True
    if process.run(["podman", "image", "exists", tag], check=False).returncode:
        return False
    _existing.add(tag)
    return True


def _dnf_cache(project: Project) -> str:
    """Return the volume of the dnf cache, which the containers of the platform share."""
    return "xcraft-dnf-" + re.sub(r"[^a-z0-9]+", "-", project.platform)


def _kill(container: str) -> None:
    # bash, the init process of the container, ignores the signals stopping podman: killed, and removed at once,
    # not later by --rm, as another container of the same name may follow
    process.run(["podman", "rm", "--force", "--time", "0", container], check=False, capture=True)


def _run_logged(cmd: list[str], log: Path, on_line: Callable[[str], None] | None, container: str | None) -> int:
    """Run `cmd` with its output in `log`, stopping `container` when the log gets too large."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with (
        log.open("w", buffering=1) as f,  # line buffered, for xcraft log --follow
        process.popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as proc,
    ):
        out = proc.stdout
        assert out  # noqa: S101
        # in parts of the longest lines, not to keep in memory those never ending
        for line in iter(lambda: out.readline(_LINE), ""):
            f.write(line)
            if f.tell() > MAX_LOG_SIZE and container:
                _kill(container)  # before leaving the block, which waits for podman
                msg = f"stopped, its log got larger than {MAX_LOG_SIZE // 2**20} MiB, see {log}"
                raise ContainerError(msg)
            if on_line:
                on_line(line)
    return proc.returncode


def _podman_build(
    project: Project,
    step: str,
    tag: str,
    files: dict[str, str],
    args: dict[str, str],
    *,
    show: str,
    pinned: Path | None = None,
) -> None:
    """Build `show`, the image `tag`, from `files` and the build `args`, logged in .xcraft/logs/_<step>.log, with the
    repository of the `pinned` packages, if any, on /pinned.
    """
    with project.tmp_dir(step) as context:
        for name, content in files.items():
            (context / name).write_text(content)
        build_args = [arg for name, value in args.items() for arg in ("--build-arg", f"{name}={value}")]
        # the metadata of the dnf cache, which the containers solved, slow to solve again under emulation, in an
        # overlay, in which the build changes nothing
        if process.run(["podman", "volume", "exists", volume := _dnf_cache(project)], check=False).returncode:
            process.run(["podman", "volume", "create", volume], capture=True)
        inspect = ["podman", "volume", "inspect", "--format", "{{.Mountpoint}}", volume]
        cache = process.run(inspect, capture=True).stdout.strip()
        cmd = ["podman", "build", "--platform", project.platform, "--security-opt", "label=disable"]
        # the local repositories, for the signing image, which installs from the upstream ones
        volumes = [f"{cache}:/var/cache/dnf:O", *_local_repos(project), *([f"{pinned}:/pinned:ro"] if pinned else [])]
        cmd += [a for v in volumes for a in ("--volume", v)]
        cmd += build_args
        cmd += ["--tag", tag, str(context)]
        log = project.state / f"logs/_{step}.log"
        with activity(f"building {show}") as on_line:
            if _run_logged(cmd, log, on_line, None):
                msg = f"{show} cannot be built, see {log}{_tail(log)}"
                raise ContainerError(msg)


def run_container(
    project: Project,
    script: str,
    *,
    container: str,
    volumes: list[str],
    network: bool = True,
    log: Path | None = None,
    on_line: Callable[[str], None] | None = None,
    image: str = "",
    install: bool = False,
) -> None:
    """Run a bash `script` in a container called `container`, of the build environment image, or of `image`.

    The volumes are mounted with the scripts of the build environment, in
    /xcraft, the repository of the pinned packages with `install`, for the
    scripts installing packages, all of them downloaded first, and a volume keeping the dnf cache
    between containers. The output goes to `log`, or to an interactive terminal
    when there is no log, and `on_line` is called with each line written to the
    log. The container is stopped when interrupted, or when its log gets larger
    than MAX_LOG_SIZE.
    """
    image = image or built_image(project)
    volumes = [*volumes, f"{BUILDENV}:/xcraft:ro", *([f"{pinned.repository(project)}:/pinned:ro"] if install else [])]
    cmd = [
        # replacing a container of a killed xcraft
        "podman", "run", "--rm", "--replace", "--name", container, "--platform", project.platform,
        "--userns=keep-id:uid=1000,gid=1000", "--security-opt", "label=disable",
        *([] if network else ["--network", "none"]), *(["--interactive", "--tty"] if log is None else []),
        *(arg for volume in [f"{_dnf_cache(project)}:/var/cache/dnf", *volumes] for arg in ("--volume", volume)),
        image, "bash", "-c", script,
    ]  # fmt: skip
    try:
        returncode = (
            process.run(cmd, check=False).returncode if log is None else _run_logged(cmd, log, on_line, container)
        )
    except BaseException:
        _kill(container)
        raise
    if returncode:
        msg = "failed" + (f", see {log}" if log else "")
        raise ContainerError(msg)


def run(
    project: Project,
    step: str,
    script: str,
    *,
    volumes: Sequence[str] = (),
    show: str = "",
    on_line: Callable[[str], None] | None = None,
    network: bool = True,
    image: str = "",
    install: bool = False,
) -> None:
    """Run a bash `script` for the `step` of xcraft, in a container, logged in .xcraft/logs/_<step>.log.

    `show` is shown while it runs, with the last line of the log, or the lines
    are given to `on_line`. See run_container() for the rest.
    """
    log = project.state / f"logs/_{step}.log"
    with activity(show) if show else nullcontext(on_line) as progress:
        try:
            run_container(
                project,
                script,
                container=f"xcraft-{step}-{os.getpid()}",
                volumes=list(volumes),
                network=network,
                log=log,
                on_line=progress,
                image=image,
                install=install,
            )
        except ContainerError as e:
            msg = f"{e}{_tail(log)}"
            raise ContainerError(msg) from None


def image_packages(project: Project, image: str) -> dict[str, Locked]:
    """Return the upstream packages installed in `image`."""
    with project.tmp_dir("record") as work:
        run(
            project,
            "record",
            f"{DNF_LOCK} python3 /xcraft/record.py /work/record.json",
            volumes=[f"{work}:/work"],
            show="recording the packages of the image",
            image=image,
        )
        installed = json.loads((work / "record.json").read_text())["installed"]
        return {name: Locked.model_validate(p) for name, p in installed.items()}


def _build_image(project: Project, tag: str, args: dict[str, str], files: dict[str, str]) -> None:

    _podman_build(
        project,
        "image",
        tag,
        files,
        args,
        show="the build environment image",
        pinned=pinned.repository(project, _image_pins(project)),  # its own only
    )
    try:
        resolved = {f"{n}-{p.evr}.{p.arch}" for n, p in _image_pins(project).items()}
        if differences := changes(resolved, {f"{n}-{p.evr}.{p.arch}" for n, p in image_packages(project, tag).items()}):
            msg = f"the build environment image does not have the packages resolved for it: {', '.join(differences)}"
            raise ContainerError(msg)  # noqa: TRY301, removed like on any error
    except BaseException:  # not to be used unchecked by the next commands
        process.run(["podman", "rmi", tag], capture=True, check=False)
        _existing.discard(tag)
        raise


def built_image(project: Project) -> str:
    """Return the tag of the build environment image, building it if needed.

    A new image must have the packages resolved for it, or it is removed. Its
    packages must be locked: see resolution.prepare().
    """
    if not project.native and (problem := host.emulation_problem(project.arch)):
        raise ContainerError(problem)
    tag, args, files = _image_definition(project)
    if _exists(tag):
        return tag
    try:
        with project.lock():
            if not _exists(tag):
                _build_image(project, tag, args, files)
            return tag
    except ProjectLockedError as e:
        msg = f"{e}, and the build environment image must be built first: try again when it is done"
        raise ProjectLockedError(msg) from None


def run_dnf(
    project: Project,
    step: str,
    args: str,
    *,
    volumes: list[str] | None = None,
    show: str,
    repos: dict[str, str] | None = None,
) -> None:
    """Run dnf with `args` natively, in the build environment image of the host, which has the plugins of dnf, like
    repoclosure, unlike the base image of run_natively(), for the platform of `project`: with the repository of its
    pinned packages, the other `repos`, by file name, and its dnf cache, logged in .xcraft/logs/_<step>.log.
    """
    cache = "/var/cache/dnf-emulated"  # the cache of the platform of `project`, besides the host's
    with project.tmp_dir(f"{step}-dnf") as work:
        (work / "repos").mkdir()
        # the host's is on /pinned
        repo = f"[{pinned.REPO_ID}]\nname={pinned.REPO_ID}\nbaseurl=file:///platform-pinned\ngpgcheck=0\nmetadata_expire=0\n"
        for name, text in {"pinned.repo": repo, **(repos or {})}.items():
            (work / "repos" / name).write_text(text)
        run(
            project.on_host(),
            step,
            f"sudo flock {cache}/.xcraft.lock dnf -q {args} --forcearch={project.arch} --setopt=reposdir=/work/repos"
            f" --setopt=cachedir={cache} --setopt=system_cachedir={cache}",
            volumes=[
                f"{_dnf_cache(project)}:{cache}",
                f"{work}:/work:ro",
                f"{pinned.repository(project)}:/platform-pinned:ro",
                *(volumes or []),
            ],
            show=show,
        )


def base_image(project: Project) -> str:
    """Return the id of the base image of the platform, pulled if needed."""
    ref = project.config.base_image
    cache = project.state / "cache/base" / hashlib.sha256(f"{ref} {project.platform}".encode()).hexdigest()
    if cache.exists() and _exists(image := cache.read_text()):
        return image
    with activity(f"pulling the base image of {project.arch}"):
        pulled = process.run(["podman", "pull", "-q", "--platform", project.platform, ref], capture=True)
    image = pulled.stdout.split()[-1]
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(image)
    return image


def base_packages(project: Project) -> list[str]:
    """Return the packages of the base image of the platform, read natively."""
    image = base_image(project)
    cache = project.state / "cache/base" / f"{hashlib.sha256(image.encode()).hexdigest()}.json"
    if not cache.exists():
        native = project.on_host()
        mount = f"type=image,source={image},target=/base"
        cmd = ["podman", "run", "--rm", "--platform", native.platform, "--mount", mount, base_image(native)]
        names = process.run([*cmd, "rpm", "--root", "/base", "-qa", "--qf", "%{NAME}\\n"], capture=True).stdout.split()
        write_atomically(cache, json.dumps(sorted(set(names) - {"gpg-pubkey"})))
    return json.loads(cache.read_text())


def run_natively(
    project: Project,
    step: str,
    args: list[str],
    *,
    volumes: list[str],
    show: str,
    repos: dict[str, str] | None = None,
    quiet: bool = False,
) -> None:
    """Run `args` as root in the base image of the host, for the platform of `project`, with its repositories, the
    upstream copies of the storages, see xcraft.upstream_copy, the other `repos`, by file name, and its dnf cache,
    logged in .xcraft/logs/_<step>.log. dnf gets the architecture of the platform with --forcearch, or the
    substitutions of its API.
    """
    from xcraft import upstream_copy  # noqa: PLC0415, which runs containers

    native, log = project.on_host(), project.state / f"logs/_{step}.log"
    container = f"xcraft-{step}-{os.getpid()}"
    with project.tmp_dir(f"{step}-repos") as directory:
        (directory / "upstream.repo").write_text(repo_file(project))
        # for the resolution and the pin downloads, not in the image: the storage is not an input of the builds
        (directory / "xcraft-upstream.repo").write_text(upstream_copy.repo_file(project))
        for name, text in (repos or {}).items():
            (directory / name).write_text(text)
        # older than their cached metadata: dnf expires it otherwise, see check_config_file_age, and downloads the
        # repomd.xml of all the repositories again; their caches are by URL
        for path in directory.iterdir():
            os.utime(path, (0, 0))
        mounts = [
            f"{_dnf_cache(project)}:/var/cache/dnf", f"{BUILDENV}:/xcraft:ro", f"{directory}:/etc/yum.repos.d:ro"
        ]  # fmt: skip
        mounts += _local_repos(project)
        cmd = [
            "podman", "run", "--rm", "--replace", "--name", container, "--platform", native.platform,
            "--security-opt", "label=disable", *(a for v in [*mounts, *volumes] for a in ("--volume", v)),
            base_image(native), "flock", "/var/cache/dnf/.xcraft.lock", *args,
        ]  # fmt: skip
        # quiet, for those run at the same time, under one activity: one only shows at a time
        with nullcontext(lambda _: None) if quiet else activity(show) as on_line:
            try:
                failed = _run_logged(cmd, log, on_line, container)
            except BaseException:
                _kill(container)
                raise
        if failed:
            msg = f"{show} failed, see {log}{_tail(log)}"
            raise ContainerError(msg)


_SIGN_PACKAGES = ["rpm-sign", "gnupg2"]


def _signing_tag(base: str) -> str:
    """Return the tag of the signing image of the build environment image `base`."""
    return f"{SIGN_IMAGE}:{hashlib.sha256(json.dumps([base, _SIGN_PACKAGES]).encode()).hexdigest()[:16]}"


def signing_image(project: Project) -> str:
    """Return the tag of the signing image, the build environment's with rpmsign and gpg, building it if needed.

    It is apart from the build environment image, so as not to change it, and the cache keys with it.
    """
    base = built_image(project)
    tag = _signing_tag(base)
    if not _exists(tag):
        # the versions of rpm-sign follow rpm's, which the lock may not have anymore
        containerfile = (
            f"FROM {base}\nUSER root\nCOPY upstream.repo /etc/yum.repos.d/\n"
            f"RUN dnf -y --disablerepo={pinned.REPO_ID} install {' '.join(_SIGN_PACKAGES)} && dnf clean all"
            " && rm /etc/yum.repos.d/upstream.repo\n"
            "USER builder\n"
        )
        files = {"Containerfile": containerfile, "upstream.repo": repo_file(project)}
        _podman_build(project, "sign-image", tag, files, {}, show="the signing image")
    return tag
