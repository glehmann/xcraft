"""The build environment: a container image made only of the configured repositories, run with podman.

The image is built from the configured base image, with its repositories
replaced by the configured ones, and the packages resolved from the lock (see
xcraft.resolution). It is tagged with a hash of its definition, so that it is
built again only when the configuration or the versions of its packages change.
Its ``builder`` user is mapped to the user running xcraft, and can use sudo.

In the containers, dnf only sees the locked version of a locked package.

The resolution runs in the base image of the host, natively, for any platform.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING

from xcraft import host, process
from xcraft.display import activity, changes, read_tail
from xcraft.lock import Locked, versionlock
from xcraft.project import ProjectLockedError, write_atomically

if TYPE_CHECKING:
    from collections.abc import Callable

    from xcraft.project import Project

MAX_LOG_SIZE = 256 * 2**20
"""Size of log after which a container is stopped: the largest build logs are under 10 MiB."""

BUILDENV = Path(__file__).parent / "buildenv"
"""The Containerfile of the image, and the scripts run in the containers, which have them in /xcraft."""


def buildenv_hash() -> str:
    """Return a hash of the files of BUILDENV, but the bytecode Python leaves there, which differs by checkout."""
    h = hashlib.sha256()
    for path in sorted(p for p in BUILDENV.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        h.update(f"{path.relative_to(BUILDENV)}\0".encode())
        h.update(hashlib.sha256(path.read_bytes()).digest())
    return h.hexdigest()


DNF_LOCK = "sudo flock /var/cache/dnf/.xcraft.lock"
"""Prefix of the dnf commands: the containers share the dnf cache volume, which dnf does not lock across them."""


PACKAGES = ["dnf-plugins-core", "python3-dnf-plugin-versionlock", "createrepo_c", "python3-rpm", "ccache", "sudo"]
"""Packages of the build environment used by xcraft, after those of the configuration."""


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
        for name, repo in sorted(project.repos.items())  # in an order of their own, which the build keys cover
    )


def _local_repos(project: Project) -> list[str]:
    """Return the volumes of the repositories on this machine, of file:// URLs, at their paths in the containers."""
    return sorted(
        {
            f"{path}:{path}:ro"
            for platform in project.all_platforms()
            for repo in platform.repos.values()
            if (url := repo.baseurl.format(arch=platform.arch)).startswith("file:///")
            for path in [url.removeprefix("file://").rstrip("/")]
        }
    )


def _image_files(project: Project) -> dict[str, str]:
    from xcraft.resolution import resolve_image  # noqa: PLC0415, which runs containers

    return {
        "Containerfile": (BUILDENV / "Containerfile").read_text(),
        "upstream.repo": repo_file(project),
        "macros.xcraft": "".join(f"%{name} {value}\n" for name, value in sorted(project.config.macros.items())),
        "versionlock.list": versionlock(resolve_image(project).image_packages()),
    }


def _image(project: Project) -> tuple[str, dict[str, str], dict[str, str]]:
    """Return the tag of the build environment image, with its build args and files.

    The tag is a hash of its definition, which pins the versions of its packages,
    so it identifies the image across machines.
    """
    config = project.config
    args = {"BASE_IMAGE": config.base_image, "PACKAGES": " ".join(sorted({*config.packages, *PACKAGES}))}
    files = _image_files(project)
    digest = hashlib.sha256(json.dumps([project.platform, args, files], sort_keys=True).encode()).hexdigest()
    return f"localhost/xcraft-build:{digest}", args, files


def image_tag(project: Project) -> str:
    """Return the tag of the build environment image, built or not."""
    return _image(project)[0]


_existing: set[str] = set()


def _exists(tag: str) -> bool:
    if tag in _existing:
        return True
    if process.run(["podman", "image", "exists", tag], check=False).returncode:
        return False
    _existing.add(tag)
    return True


def build_image(project: Project) -> str:
    """Return the tag of the build environment image, building it if needed.

    A new image must have the packages resolved for it, or it is removed. Its
    packages must be locked: see resolution.prepare().
    """
    if not project.native and (problem := host.emulation_problem(project.arch)):
        raise ContainerError(problem)
    tag, args, files = _image(project)
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


def _build_image(project: Project, tag: str, args: dict[str, str], files: dict[str, str]) -> None:
    from xcraft.resolution import resolve_image  # noqa: PLC0415, which runs containers

    _podman_build(project, "image", tag, files, args, show="the build environment image")
    try:
        resolved = {f"{n}-{p.evr}.{p.arch}" for n, p in resolve_image(project).image_packages().items()}
        if differences := changes(resolved, {f"{n}-{p.evr}.{p.arch}" for n, p in image_packages(project, tag).items()}):
            msg = f"the build environment image does not have the packages resolved for it: {', '.join(differences)}"
            raise ContainerError(msg)  # noqa: TRY301, removed like on any error
    except BaseException:  # not to be used unchecked by the next commands
        process.run(["podman", "rmi", tag], capture=True, check=False)
        _existing.discard(tag)
        raise


def _podman_build(
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
        if process.run(["podman", "volume", "exists", volume := _dnf_cache(project)], check=False).returncode:
            process.run(["podman", "volume", "create", volume], capture=True)
        inspect = ["podman", "volume", "inspect", "--format", "{{.Mountpoint}}", volume]
        cache = process.run(inspect, capture=True).stdout.strip()
        cmd = ["podman", "build", "--platform", project.platform, "--security-opt", "label=disable"]
        cmd += [a for v in [f"{cache}:/var/cache/dnf:O", *_local_repos(project)] for a in ("--volume", v)]
        cmd += build_args
        cmd += ["--tag", tag, str(context)]
        log = project.state / f"logs/_{step}.log"
        with activity(f"building {show}") as on_line:
            if _run_logged(cmd, log, on_line, None):
                msg = f"{show} cannot be built, see {log}{_tail(log)}"
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
        (work / "upstream.repo").write_text(repo_file(project))
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
    with project.work("record") as work:
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


def run_natively(project: Project, step: str, args: list[str], *, volumes: list[str], show: str) -> None:
    """Run `args` as root in the base image of the host, for the platform of `project`, with its repositories and its
    dnf cache, logged in .xcraft/logs/_<step>.log.
    """
    native, log = project.on_host(), project.state / f"logs/_{step}.log"
    container = f"xcraft-{step}-{os.getpid()}"
    with project.work(f"{step}-repos") as repos:
        (repos / "upstream.repo").write_text(repo_file(project))
        mounts = [f"{_dnf_cache(project)}:/var/cache/dnf", f"{BUILDENV}:/xcraft:ro", f"{repos}:/etc/yum.repos.d:ro"]
        mounts += _local_repos(project)
        cmd = [
            "podman", "run", "--rm", "--replace", "--name", container, "--platform", native.platform,
            "--security-opt", "label=disable", *(a for v in [*mounts, *volumes] for a in ("--volume", v)),
            base_image(native), "flock", "/var/cache/dnf/.xcraft.lock", *args,
        ]  # fmt: skip
        with activity(show) as on_line:
            try:
                failed = _run_logged(cmd, log, on_line, container)
            except BaseException:
                _kill(container)
                raise
        if failed:
            msg = f"{show} failed, see {log}{_tail(log)}"
            raise ContainerError(msg)


def run(
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
    log = project.state / f"logs/_{step}.log"
    with activity(show) if show else nullcontext(on_line) as progress:
        try:
            run_container(
                project,
                script,
                container=f"xcraft-{step}-{os.getpid()}",
                volumes=volumes,
                network=network,
                log=log,
                on_line=progress,
                image=image,
            )
        except ContainerError as e:
            msg = f"{e}{_tail(log)}"
            raise ContainerError(msg) from None


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
) -> None:
    """Run a bash `script` in a container called `container`, of the build environment image, or of `image`.

    The volumes are mounted with the scripts of the build environment, in
    /xcraft, the versionlock of the lock, and a volume keeping the dnf cache
    between containers. The output goes to `log`, or to an interactive terminal
    when there is no log, and `on_line` is called with each line written to the
    log. The container is stopped when interrupted, or when its log gets larger
    than MAX_LOG_SIZE.
    """
    from xcraft.resolution import resolve_image  # noqa: PLC0415, which runs containers

    _warm_dnf_cache(project)
    image = image or build_image(project)
    (project.state / "tmp").mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=project.state / "tmp", prefix="versionlock-") as lock_list:
        lock_list.write(versionlock(resolve_image(project).packages))
        lock_list.flush()
        volumes = [*volumes, f"{BUILDENV}:/xcraft:ro", f"{lock_list.name}:/etc/dnf/plugins/versionlock.list:ro"]
        volumes += _local_repos(project)
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
