"""The installation ISO, made from a snapshot of the distribution, with the hook of the project, iso/hook.

Its repository is the closure of [iso] packages in the snapshot, and install.img, the installer's root filesystem, is
installed from the snapshot too, which has its packages. The tools making it, which are not in the build
environment image, are installed in its containers, from the packages pinned for them in the lock.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field
from rich.markup import escape

from xcraft import buildid, buildkey, container, devbuilds, publish, remote, remote_pages, resolution
from xcraft.builds import load_builds
from xcraft.display import activity, changes, console
from xcraft.lock import load_lock
from xcraft.pinned import REPO_ID
from xcraft.project import INTERNAL
from xcraft.signing import Signer, key_paths, key_signer, repo_keys
from xcraft.sources import sha256_of

if TYPE_CHECKING:
    from xcraft.project import Project, Remote

_INSTALL_TOOLS = (
    f"{container.DNF_LOCK} dnf -q -y --disablerepo='*' --enablerepo={REPO_ID} --setopt=install_weak_deps=False install "
    + " ".join(resolution.ISO_TOOLS)
)
"""Installs the tools making the ISO, which the image does not have, from the pinned packages only."""


class Image(BaseModel):
    """A cached ISO: its SHA-256."""

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _templates_hash(project: Project) -> str:
    """Return the hash of the templates of the ISO: their files, with whether they are executable, which gives their
    modes in the ISO, see copy_templates() in buildenv/iso.py, and the targets of their links, like the units masked
    by links to /dev/null.
    """
    root = project.root / "iso"
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            digest.update(f"{path.relative_to(root)}\0link\0{path.readlink()}\0".encode())
        elif path.is_file():
            digest.update(f"{path.relative_to(root)}\0file\0{bool(path.stat().st_mode & 0o111)}\0".encode())
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def dev_suffixes(project: Project, dev: str | None) -> dict[str, str]:
    """Return the release suffixes of the development builds `dev` of the ISO of the checkout.

    None for final builds, nor for packages whose build ID is the main branch's. Read without preparing the lock when
    they are all final ones, see final_only().
    """
    if not dev or buildid.final_only(project):
        return {}
    resolution.prepare(project)
    return devbuilds.dev_keys(project, dev)[1]


def iso_key(
    project: Project,
    *,
    netinstall: bool,
    volid: str | None,
    signer: Signer | None,
    dev: str | None = None,
    nocheck: bool = False,
) -> str:
    """Return the key of the ISO of the checkout, with the development builds `dev`, if any, and with the builds
    without the tests of their specs with `nocheck`: what it is made of.

    Read without containers, nor the secret key of `signer`.
    """
    lock, builds = load_lock(project), load_builds(project)
    config = project.config.model_dump(
        exclude={
            **dict.fromkeys(("remote", "logo", "import_", "sign", "ccache_size", "same_version", "publish"), True),
            "iso": {"unattended"},
        }
    )
    config["extra"] = project.config.publish.extra
    data = [
        project.platform,
        config,
        buildkey.directories(project),
        buildkey.recipe(project),
        # all of the locks, which the build keys cover on all the architectures, read as they are, before prepare():
        # the numbers of the builds, in their releases, and the reuses of builds too
        builds.model_dump(mode="json"),
        lock.model_dump(mode="json"),
        _templates_hash(project),
        container.buildenv_hash(),
        [
            Path(__file__).with_name(module).read_text()
            for module in (
                "iso.py",
                "buildkey.py",
                "publish.py",
                "container.py",
                "cache.py",
                "specs.py",
                "resolution.py",
                "localrepo.py",
                "repodata.py",
                "signing.py",
            )
        ],
        netinstall,
        volid,
        signer.key.upper() if signer else None,
    ]
    if suffixes := dev_suffixes(project, dev):  # but the keys of the ISOs of final builds
        data.append(suffixes)
    if nocheck:  # but the keys of the others
        data.append("nocheck")
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def iso_inputs(project: Project, *, signer: Signer | None) -> dict[str, str]:
    """Return the keys of the full and netinstall ISOs of the checkout, by architecture and variant."""
    return {
        f"{project.arch}/{'netinstall' if netinstall else 'full'}": iso_key(
            project, netinstall=netinstall, volid=None, signer=signer
        )
        for netinstall in (False, True)
    }


def iso_id(project: Project, *, signer: Signer | None) -> str:
    """Return the identifier of the ISOs of the checkout, the same for all the architectures and variants: the
    beginning of a hash of all their keys.
    """
    keys = sorted(key for platform in project.all_platforms() for key in iso_inputs(platform, signer=signer).values())
    return hashlib.sha256(json.dumps(keys).encode()).hexdigest()[:6]


def iso_name(project: Project, *, netinstall: bool, signer: Signer | None, dev: str | None = None) -> str:
    """Return the file name of an ISO of the checkout: the name and version of the distribution, with its
    identifier, the one of its development builds `dev`, if any, and its architecture.
    """
    iso, ident = project.config.iso, f"{iso_id(project, signer=signer)}{f'.{dev}' if dev else ''}"
    return f"{iso.name}-{iso.version}-{ident}-{project.arch}{'-netinstall' if netinstall else ''}.iso"


def cached_iso(project: Project, key: str) -> Path:
    """Return the directory of a cached ISO."""
    return project.state / "cache/isos" / project.arch / key


def iso_path(project: Project, key: str) -> str:
    """Return the path of the tree of the ISO of `key` in the remote storage."""
    return f"{INTERNAL}/isos/{project.arch}/{key}"


def _verified_image(tree: Path) -> Image:
    """Return the metadata of the cached ISO in `tree`. Raise when the ISO does not match its SHA-256."""
    image = Image.model_validate_json((tree / "image.json").read_text())
    if sha256_of(tree / "image.iso") != image.sha256:
        msg = f"cached ISO does not match its SHA-256: {tree}"
        raise RuntimeError(msg)
    return image


def _copy_iso(
    project: Project, tree: Path, output: Path | None, *, netinstall: bool, signer: Signer | None, dev: str | None
) -> Path:
    """Give the cached ISO in `tree` as `output`, or with its name in .xcraft/iso/ by default, and return it: a hard
    link to it, not a copy, on the same file system, like in .xcraft/: read-only, not to change the cache through it.
    """
    image = _verified_image(tree)
    output = (
        output or project.state / "iso" / iso_name(project, netinstall=netinstall, signer=signer, dev=dev)
    ).absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() and (output.samefile(tree / "image.iso") or sha256_of(output) == image.sha256):
        return output
    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".iso-") as tmp:
        part = Path(tmp) / "image.iso"
        try:
            (tree / "image.iso").chmod(0o444)
            os.link(tree / "image.iso", part)
        except OSError:  # another file system
            shutil.copy2(tree / "image.iso", part)
        part.replace(output)
    return output


def _existing_iso(
    project: Project, key: str, output: Path | None, *, netinstall: bool, signer: Signer | None, dev: str | None
) -> Path | None:
    """Return the ISO of `key`, copied to `output`, when cached or in the remote storage, which has none of
    development builds `dev`.
    """
    tree = cached_iso(project, key)
    if tree.exists():
        os.utime(tree)  # used, see xcraft clean --keep-days
        return _copy_iso(project, tree, output, netinstall=netinstall, signer=signer, dev=dev)
    if not project.config.remote or dev:
        return None
    try:
        with activity("looking for the ISO in the remote storage") as on_line:
            if remote.fetch_tree(project, iso_path(project, key), tree, on_line):
                return _copy_iso(project, tree, output, netinstall=netinstall, signer=signer, dev=None)
    except remote.RemoteError as e:
        console.print(f"[yellow]remote storage unavailable, making the ISO locally[/]: {escape(str(e))}")
    return None


def fetch_isos(project: Project, *, signer: Signer | None) -> bool:
    """Put the full and netinstall ISOs of the checkout in the cache, fetched from the remote storage when missing,
    and return whether they are all there.
    """
    try:
        with activity("looking for the ISOs in the remote storage"):
            return all(
                (tree := cached_iso(project, key)).exists() or remote.fetch_tree(project, iso_path(project, key), tree)
                for key in iso_inputs(project, signer=signer).values()
            )
    except remote.RemoteError:
        return False


def push_isos(
    project: Project, storage: Remote, *, signer: Signer | None, upload_signer: Signer | None = None
) -> dict[str, publish.Stored]:
    """Upload the cached ISOs of the checkout the storage misses, and return their paths there, with their SHA-256,
    by file name.
    """
    count = present = 0
    stored = {}
    for netinstall in (False, True):
        key = iso_key(project, netinstall=netinstall, volid=None, signer=signer)
        tree, path = cached_iso(project, key), iso_path(project, key)
        if not tree.exists():
            continue
        _verified_image(tree)
        trusted = upload_signer or signer or key_signer(project)
        if remote.tree_exists(storage, path, trusted=trusted):
            present += 1
        else:
            with activity("uploading the ISO", log=True):
                remote.upload_tree(storage, path, tree, ["image.iso", "image.json"], signer=trusted)
            remote_pages.update_pages(project, storage, [f"{path}/"])
            count += 1
        # as the manifest found or written has it, which may be of another machine's image
        root, files = remote.read_tree(storage, path, trusted=trusted)
        stored[iso_name(project, netinstall=netinstall, signer=signer)] = publish.Stored(
            f"{root}/image.iso", files["image.iso"]
        )
    console.print(f"{count} ISOs pushed, {present} already in the remote storage")
    return stored


def _check_installed_tools(project: Project, recorded: Path) -> None:
    """Raise when the tools making the ISO, `recorded` by record.py, are not those pinned in the lock, besides the
    image's.
    """
    lock = load_lock(project)
    pinned, image = lock.binaries.get(project.arch, {}), {*lock.image.get(project.arch, [])}
    installed = json.loads(recorded.read_text())["installed"]
    differences = changes(
        {f"{n}-{pinned[n].evr}.{pinned[n].arch}" for n in lock.iso.get(project.arch, [])},
        {f"{n}-{p['evr']}.{p['arch']}" for n, p in installed.items() if n not in image},
    )
    if differences:
        msg = f"the tools making the ISO are not those pinned in the lock: {', '.join(differences)}"
        raise RuntimeError(msg)


def check_iso_config(project: Project) -> None:
    """Raise when the ISO of the platform cannot be made: on x86_64 only, for its boot loaders, with the installer's
    packages, and the hook of the project, an executable iso/hook.
    """
    if not project.config.iso.installer:
        msg = "the ISO needs the installer's packages: see [iso] in xcraft.toml"
        raise RuntimeError(msg)
    if not project.arch.startswith("x86_64"):
        msg = f"the ISOs are for x86_64, for their boot loaders, not {project.arch}"
        raise RuntimeError(msg)
    if not os.access(project.root / "iso/hook", os.X_OK):
        msg = "the ISO needs the hook of the project, an executable iso/hook: see xcraft/buildenv/iso.py"
        raise RuntimeError(msg)


def _assemble_iso(
    project: Project, snapshot: Path, *, netinstall: bool, volid: str | None, signer: Signer | None
) -> Path:
    """Make the ISO of `snapshot`, its binaries for the architecture, in .xcraft/tmp/, and return it."""
    iso = project.config.iso
    check_iso_config(project)
    if error := resolution.resolve(project).tools_error:
        msg = f"the tools making the ISO cannot be installed: {error}"
        raise RuntimeError(msg)
    with project.tmp_dir("iso") as work:
        if signer:
            (work / "key.asc").write_text(signer.public_key())
        (work / "config.json").write_text(
            json.dumps({
                "snapshot": "/snapshot", "templates": "/templates", "tree": "/work/tree", "installer": iso.installer,
                "packages": iso.packages, "netinstall": netinstall, "signed": bool(signer),
                "keys": ["/work/key.asc", *key_paths(repo_keys(project), work)] if signer else [],
                "name": iso.name, "version": iso.version,
            })
        )  # fmt: skip
        volumes = [f"{snapshot}:/snapshot:ro", f"{project.root / 'iso'}:/templates:ro", f"{work}:/work"]
        container.run(
            project,
            "iso",
            f"{_INSTALL_TOOLS} && {container.DNF_LOCK} python3 /xcraft/record.py /work/tools.json"
            " && sudo python3 /xcraft/iso.py /work/config.json",
            volumes=volumes,
            show="making the tree of the ISO",
            install=True,
        )
        _check_installed_tools(project, work / "tools.json")
        tree = work / "tree"
        if signer and not netinstall:
            signer.sign_file(tree / "repodata/repomd.xml")
        # hybrid, for BIOS and UEFI, from a CD or a USB stick
        xorriso = (
            f"xorriso -as mkisofs -quiet -o /work/image.iso -V {shlex.quote(volid or iso.default_volid)}"
            " -R -J -joliet-long"
            " -b boot/isolinux/isolinux.bin -c boot/isolinux/boot.cat -no-emul-boot -boot-load-size 4 -boot-info-table"
            " -isohybrid-mbr /usr/share/syslinux/isohdpfx.bin"
            " -eltorito-alt-boot -e boot/efiboot.img -no-emul-boot -isohybrid-gpt-basdat /work/tree"
        )
        container.run(
            project, "iso", f"{_INSTALL_TOOLS} && {xorriso}", volumes=volumes, show="making the ISO", install=True
        )
        made = project.state / "tmp" / f"{work.name}.iso"
        shutil.move(work / "image.iso", made)
    return made


def _snapshot_iso(
    project: Project,
    snapshot: Path,
    checkout: publish.Checkout,
    output: Path | None,
    *,
    netinstall: bool,
    volid: str | None,
    signer: Signer | None,
    dev: str | None = None,
    nocheck: bool = False,
) -> Path:
    """Return the ISO of `snapshot`, made of `checkout`, with the development builds `dev`, if any, and those without
    the tests of their specs with `nocheck`: cached, fetched, or assembled and cached by the key of the checkout.

    Raise when the checkout is not the snapshot's anymore, or when the key changed while assembling it, like the lock
    by a git checkout: the ISO would be of the one before.
    """
    with project.lock():
        publish.check_checkout(project, checkout)
        key = iso_key(project, netinstall=netinstall, volid=volid, signer=signer, dev=dev, nocheck=nocheck)
        if found := _existing_iso(project, key, output, netinstall=netinstall, signer=signer, dev=dev):
            return found
        made = _assemble_iso(project, snapshot, netinstall=netinstall, volid=volid, signer=signer)
        if key != iso_key(project, netinstall=netinstall, volid=volid, signer=signer, dev=dev, nocheck=nocheck):
            made.unlink()
            msg = "ISO inputs changed while making the image: run again"
            raise RuntimeError(msg)
        tree = cached_iso(project, key)
        with project.tmp_dir("cache-iso") as work:
            staged = work / "tree"
            staged.mkdir()
            shutil.move(made, staged / "image.iso")
            (staged / "image.json").write_text(Image(sha256=sha256_of(staged / "image.iso")).model_dump_json())
            tree.parent.mkdir(parents=True, exist_ok=True)
            if not tree.exists():
                staged.rename(tree)
        return _copy_iso(project, tree, output, netinstall=netinstall, signer=signer, dev=dev)


def snapshot_isos(
    project: Project, snapshot: Path, checkout: publish.Checkout, dest: Path, *, signer: Signer | None
) -> list[Path]:
    """Make the full and netinstall ISOs of `snapshot`, its binaries for the architecture, made of `checkout`, in
    `dest`, with their detached signatures by `signer`, if any, and return them.

    `dest` is the directory of the ISOs of all the architectures.
    """
    dest.mkdir(parents=True, exist_ok=True)
    isos = []
    for netinstall in (False, True):
        iso = _snapshot_iso(project, snapshot, checkout, None, netinstall=netinstall, volid=None, signer=signer)
        isos.append(Path(shutil.move(iso, dest / iso.name)))
        if signer:
            signer.sign_file(isos[-1])
    return isos


def _build_signers(project: Project) -> list[Signer]:
    """Return the keys of the builds of the remote storages, which sign what CI publishes, see Remote.build_keys."""
    return [
        Signer(key) for key in sorted({k for storage in project.config.remote.values() for k in storage.build_keys})
    ]


def checkout_iso_keys(project: Project, dev: str) -> set[str]:
    """Return the keys of the ISOs of the checkout, which xcraft clean keeps: the full and netinstall ones, signed with
    the default key, with those of the builds of the storage, like CI's, or unsigned, of final builds, or of the
    development builds `dev`, but those of another volume id or without the tests of the specs, made again if needed.
    """
    signers = [key_signer(project), *_build_signers(project), None]
    idents = [None, dev] if dev_suffixes(project, dev) else [None]
    return {
        iso_key(project, netinstall=netinstall, volid=None, signer=signer, dev=ident)
        for netinstall in (False, True)
        for signer in signers
        for ident in idents
    }


def checkout_iso(
    project: Project,
    output: Path | None = None,
    *,
    netinstall: bool = False,
    volid: str | None = None,
    signer: Signer | None = None,
    dev: str | None = None,
    nocheck: bool = False,
    published: bool = False,
) -> Path:
    """Return the ISO of the checkout: build or fetch its packages, assemble a local snapshot, and make its ISO.

    With the development builds `dev`, like xcraft build makes them, or final builds only without, and without the
    tests of the specs with `nocheck`. With `published`, when the ISO signed by `signer` is neither cached nor in the
    remote storage, the one CI published, signed with a key of the builds of the storage, is taken rather than made.
    """
    with project.lock():
        dev = dev if dev_suffixes(project, dev) else None  # of final builds only otherwise, like the remote storage's
        nocheck = nocheck and dev is not None  # which the final builds ignore
        key = iso_key(project, netinstall=netinstall, volid=volid, signer=signer, dev=dev, nocheck=nocheck)
        if found := _existing_iso(project, key, output, netinstall=netinstall, signer=signer, dev=dev):
            return found
        for ci in [] if dev or not published else _build_signers(project):
            ci_key = iso_key(project, netinstall=netinstall, volid=volid, signer=ci)
            if ci.key != (signer and signer.key) and (
                found := _existing_iso(project, ci_key, output, netinstall=netinstall, signer=ci, dev=None)
            ):
                return found
        check_iso_config(project)  # before the snapshot
        resolution.prepare(project)
        dest = (project.state / "publish").absolute()
        _, _, checkout = publish.snapshot(
            [project], dest, signer=signer, storage=None, build=False, dev=dev, nocheck=nocheck
        )
        return _snapshot_iso(
            project,
            dest / publish.REPO / project.arch,
            checkout,
            output,
            netinstall=netinstall,
            volid=volid,
            signer=signer,
            dev=dev,
            nocheck=nocheck,
        )
