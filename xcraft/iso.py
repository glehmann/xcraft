"""The installation ISO, made from a snapshot of the distribution, like XCP-ng's create-install-image does.

Its repository is the closure of xcp-ng-deps in the snapshot, and install.img, the installer's root filesystem, is
installed from the snapshot too, which has its packages. The tools making it, which are not in the build
environment image, are installed in its containers, from the packages pinned for them in the lock.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field
from rich.markup import escape

from xcraft import buildkey, remote
from xcraft.buildid import final_only
from xcraft.builds import load_builds
from xcraft.container import DNF_LOCK, buildenv_hash, run
from xcraft.devbuilds import dev_keys
from xcraft.display import activity, changes, console
from xcraft.lock import load_lock
from xcraft.pinned import REPO_ID
from xcraft.project import INTERNAL
from xcraft.publish import REPO, snapshot
from xcraft.resolution import ISO_TOOLS, prepare, resolve
from xcraft.signing import key_paths, repo_keys, signing_key
from xcraft.sources import sha256_of

if TYPE_CHECKING:
    from xcraft.project import Project, Remote
    from xcraft.signing import Signer

_INSTALL_TOOLS = (
    f"{DNF_LOCK} dnf -q -y --disablerepo='*' --enablerepo={REPO_ID} --setopt=install_weak_deps=False install "
    + " ".join(ISO_TOOLS)
)
"""Installs the tools making the ISO, which the image does not have, from the pinned packages only."""
KEY_FILE = "RPM-GPG-KEY-xcpng"
"""The public key of the signed repository of the ISO, as named in its .treeinfo."""


class Image(BaseModel):
    """A cached ISO: the version of the distribution it installs, and its SHA-256."""

    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.+-]*$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _templates(project: Project) -> str:
    """Return the hash of the templates of the ISO: their files, and the targets of their links, like the units
    masked by links to /dev/null.
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
    """Return the release suffixes of the development builds of the ISO of the checkout, with the identifier `dev`: none
    for final builds, or without packages whose build ID is not the main branch's. Read without preparing the lock when
    they are all final ones, see final_only(), which the keys need otherwise.
    """
    if not dev or final_only(project):
        return {}
    prepare(project)
    return dev_keys(project, dev)[1]


def iso_key(
    project: Project, *, netinstall: bool, volid: str | None, signer: Signer | None, dev: str | None = None
) -> str:
    """Return the key of the ISO of the checkout: what it is made of, read without containers, nor the secret key of
    `signer`, with the development builds of the identifier `dev`, if any.
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
        # the numbers of the builds, in their releases, and the reuses of builds
        {name: i.model_dump() for name, i in builds.ids.items()},
        _templates(project),
        lock.sources,
        *[getattr(builds, f).of(project.arch) for f in ("requires", "provides", "buildrequires")],
        *(
            {n: lock.binaries[project.arch][n].text() for n in getattr(lock, f).get(project.arch, [])}
            for f in ("runtime", "iso")
        ),
        buildenv_hash(),
        [
            Path(__file__).with_name(module).read_text()
            for module in (
                "iso.py",
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
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def cached_iso(project: Project, key: str) -> Path:
    """Return the directory of a cached ISO."""
    return project.state / "cache/isos" / project.arch / key


def iso_inputs(project: Project, *, signer: Signer | None) -> dict[str, str]:
    """Return the keys of the full and netinstall ISOs of the checkout, by architecture and variant."""
    return {
        f"{project.arch}/{'netinstall' if netinstall else 'full'}": iso_key(
            project, netinstall=netinstall, volid=None, signer=signer
        )
        for netinstall in (False, True)
    }


def iso_id(project: Project, *, signer: Signer | None) -> str:
    """Return the identifier of the ISOs of the checkout, the same for all the architectures, full or netinstall: the
    beginning of a hash of the keys of all of them, which cover what they are made of.
    """
    keys = sorted(key for platform in project.all_platforms() for key in iso_inputs(platform, signer=signer).values())
    return hashlib.sha256(json.dumps(keys).encode()).hexdigest()[:6]


def iso_name(project: Project, version: str, *, netinstall: bool, signer: Signer | None, dev: str | None = None) -> str:
    """Return the file name of an ISO of the distribution `version`, with its identifier, the one of its development
    builds `dev`, if any, and its architecture.
    """
    ident = f"{iso_id(project, signer=signer)}{f'.{dev}' if dev else ''}"
    return f"xcp-ng-{version}-{ident}-{project.arch}{'-netinstall' if netinstall else ''}.iso"


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


def iso_path(project: Project, key: str) -> str:
    """Return the path of the tree of the ISO of `key` in the remote storage."""
    return f"{INTERNAL}/isos/{project.arch}/{key}"


def _checkout(project: Project) -> tuple[dict[str, str], str, str]:
    return buildkey.directories(project), _templates(project), sha256_of(project.root / "xcraft.toml")


def _image(tree: Path) -> Image:
    image = Image.model_validate_json((tree / "image.json").read_text())
    if sha256_of(tree / "image.iso") != image.sha256:
        msg = f"cached ISO does not match its SHA-256: {tree}"
        raise RuntimeError(msg)
    return image


def _output(
    project: Project, tree: Path, output: Path | None, *, netinstall: bool, signer: Signer | None, dev: str | None
) -> Path:
    """Return the cached ISO in `tree` as `output`, or with its name in .xcraft/iso/ by default."""
    image = _image(tree)
    name = iso_name(project, image.version, netinstall=netinstall, signer=signer, dev=dev)
    output = (output or project.state / "iso" / name).absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    if not output.exists() or sha256_of(output) != image.sha256:
        with tempfile.TemporaryDirectory(dir=output.parent, prefix=".iso-") as tmp:
            part = Path(tmp) / "image.iso"
            shutil.copy2(tree / "image.iso", part)
            part.replace(output)
    return output


def _find(
    project: Project, key: str, output: Path | None, *, netinstall: bool, signer: Signer | None, dev: str | None
) -> Path | None:
    """Return the ISO of `key` as `output`: cached, or fetched from the remote storage, but of development builds `dev`,
    which it has not.
    """
    tree = cached_iso(project, key)
    if tree.exists():
        return _output(project, tree, output, netinstall=netinstall, signer=signer, dev=dev)
    if not project.config.remote or dev:
        return None
    try:
        with activity("looking for the ISO in the remote storage") as on_line:
            if remote.fetch_tree(project, iso_path(project, key), tree, on_line):
                return _output(project, tree, output, netinstall=netinstall, signer=signer, dev=None)
    except remote.RemoteError as e:
        console.print(f"[yellow]remote storage unavailable, making the ISO locally[/]: {escape(str(e))}")
    return None


def push_isos(
    project: Project, storage: Remote, *, signer: Signer | None, upload_signer: Signer | None = None
) -> dict[str, tuple[str, str]]:
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
        image = _image(tree)
        stored[iso_name(project, image.version, netinstall=netinstall, signer=signer)] = (
            f"{path}/image.iso",
            image.sha256,
        )
        if remote.tree_exists(storage, path, trusted=upload_signer or signer or signing_key(project)):
            present += 1
            continue
        with activity("uploading the ISO", log=True):
            remote.upload_tree(
                storage, path, tree, ["image.iso", "image.json"], signer=upload_signer or signer or signing_key(project)
            )
        remote.update_pages(project, storage, [f"{path}/"])
        count += 1
    console.print(f"{count} ISOs pushed, {present} already in the remote storage")
    return stored


def build_iso(
    project: Project,
    output: Path | None = None,
    *,
    netinstall: bool = False,
    volid: str | None = None,
    signer: Signer | None = None,
    dev: str | None = None,
) -> Path:
    """Build or fetch the current packages, assemble a local snapshot, and make its installation ISO: with the
    development builds of the identifier `dev`, like xcraft build makes them, or final builds only without.
    """
    with project.lock():
        dev = dev if dev_suffixes(project, dev) else None  # of final builds only otherwise, like the remote storage's
        key = iso_key(project, netinstall=netinstall, volid=volid, signer=signer, dev=dev)
        if found := _find(project, key, output, netinstall=netinstall, signer=signer, dev=dev):
            return found
        checkout = _checkout(project)
        prepare(project)
        dest = (project.state / "publish").absolute()
        snapshot([project], dest, signer=signer, storage=None, build=False, dev=dev)
        if checkout != _checkout(project):
            msg = "ISO inputs changed while assembling the snapshot: run again"
            raise RuntimeError(msg)
        return make_iso(
            project, dest / REPO / project.arch, output, netinstall=netinstall, volid=volid, signer=signer, dev=dev
        )


def _check_tools(project: Project, recorded: Path) -> None:
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


def make_iso(
    project: Project,
    snapshot: Path,
    output: Path | None,
    *,
    netinstall: bool,
    volid: str | None,
    signer: Signer | None,
    dev: str | None = None,
) -> Path:
    """Return the ISO of `snapshot`, with the development builds of the identifier `dev`, if any: cached, fetched, or
    made, and cached by the key of the checkout once made.
    """
    with project.lock():
        key = iso_key(project, netinstall=netinstall, volid=volid, signer=signer, dev=dev)
        if found := _find(project, key, output, netinstall=netinstall, signer=signer, dev=dev):
            return found
        checkout = _checkout(project)
        made, version = _make_iso(project, snapshot, netinstall=netinstall, volid=volid, signer=signer)
        if checkout != _checkout(project):
            msg = "ISO inputs changed while making the image: run again"
            raise RuntimeError(msg)
        key = iso_key(project, netinstall=netinstall, volid=volid, signer=signer, dev=dev)
        tree = cached_iso(project, key)
        with project.work("cache-iso") as work:
            staged = work / "tree"
            staged.mkdir()
            shutil.move(made, staged / "image.iso")
            (staged / "image.json").write_text(
                Image(version=version, sha256=sha256_of(staged / "image.iso")).model_dump_json()
            )
            tree.parent.mkdir(parents=True, exist_ok=True)
            if not tree.exists():  # of the same inputs, kept from before the lock went back to them
                staged.rename(tree)
        return _output(project, tree, output, netinstall=netinstall, signer=signer, dev=dev)


def _make_iso(
    project: Project, snapshot: Path, *, netinstall: bool, volid: str | None, signer: Signer | None
) -> tuple[Path, str]:
    """Make the ISO of `snapshot`, its binaries for the architecture, in .xcraft/tmp/, and return it, with the
    version of the distribution it installs.
    """
    iso = project.config.iso
    if error := resolve(project).tools_error:
        msg = f"the tools making the ISO cannot be installed: {error}"
        raise RuntimeError(msg)
    with project.work("iso") as work:
        (work / "config.json").write_text(
            json.dumps({
                "snapshot": "/snapshot", "templates": "/templates", "tree": "/work/tree", "installer": iso.installer,
                "packages": iso.packages, "netinstall": netinstall, "signed": bool(signer),
                "keys": key_paths(repo_keys(project), work) if signer else [],
            })
        )  # fmt: skip
        volumes = [f"{snapshot}:/snapshot:ro", f"{project.root / 'iso'}:/templates:ro", f"{work}:/work"]
        run(
            project,
            "iso",
            f"{_INSTALL_TOOLS} && {DNF_LOCK} python3 /xcraft/record.py /work/tools.json"
            " && sudo python3 /xcraft/iso.py /work/config.json",
            volumes=volumes,
            show="making the tree of the ISO",
            install=True,
        )
        _check_tools(project, work / "tools.json")
        tree = work / "tree"
        if signer and not netinstall:
            signer.sign_file(tree / "repodata/repomd.xml")
            keys = tree / KEY_FILE
            keys.write_text(signer.public_key() + "\n" + keys.read_text())
        info = json.loads((work / "info.json").read_text())
        # hybrid, for BIOS and UEFI, from a CD or a USB stick
        xorriso = (
            f"xorriso -as mkisofs -quiet -o /work/image.iso -V {shlex.quote(volid or info['volid'])} -R -J -joliet-long"
            " -b boot/isolinux/isolinux.bin -c boot/isolinux/boot.cat -no-emul-boot -boot-load-size 4 -boot-info-table"
            " -isohybrid-mbr /usr/share/syslinux/isohdpfx.bin"
            " -eltorito-alt-boot -e boot/efiboot.img -no-emul-boot -isohybrid-gpt-basdat /work/tree"
        )
        run(project, "iso", f"{_INSTALL_TOOLS} && {xorriso}", volumes=volumes, show="making the ISO", install=True)
        made = project.state / "tmp" / f"{work.name}.iso"
        shutil.move(work / "image.iso", made)
    return made, info["version"]


def make_isos(project: Project, snapshot: Path, dest: Path, *, signer: Signer | None) -> list[Path]:
    """Make the ISOs of `snapshot`, its binaries for the architecture, with the packages and netinstall, in `dest`,
    the directory of the ISOs of all the architectures, with their detached signatures by `signer`, if any, and return
    them.
    """
    dest.mkdir(parents=True, exist_ok=True)
    isos = []
    for netinstall in (False, True):
        iso = make_iso(project, snapshot, None, netinstall=netinstall, volid=None, signer=signer)
        isos.append(Path(shutil.move(iso, dest / iso.name)))
        if signer:
            signer.sign_file(isos[-1])
    return isos
