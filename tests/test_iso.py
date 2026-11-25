"""The ISOs: their names, their keys, by what they contain, their cache, and the checks before making them."""

from __future__ import annotations

import itertools
import os
import re
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from tests import TOML, cache_image, needs_gpg, xcraft
from xcraft import buildkey, container, iso, publish
from xcraft.builds import BuildId, load_builds, save_builds
from xcraft.cache import BuildError
from xcraft.commands import publish as publish_command
from xcraft.iso import cached_iso, checkout_iso, iso_id, iso_key, iso_name, snapshot_isos
from xcraft.lock import Pin, load_lock, save_lock
from xcraft.project import Iso, Project, Remote
from xcraft.signing import Signer, create_dev_key


@pytest.fixture
def project(unprepared_root: Path) -> Project:
    """Return the test project, unprepared: the ISOs are looked up without preparing it."""
    return Project.find()


def should_change_the_iso_key_only_with_what_the_iso_contains(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    def key() -> str:
        return iso_key(project, netinstall=False, volid=None, signer=None)

    original = key()
    project.config.remote["main"] = Remote(url="https://example.com")
    project.config.publish.stage = "other"
    project.config.logo = "https://example.com/logo.png"
    project.config.iso.unattended = "unattended.xml"  # given at boot, not in the ISO
    assert key() == original
    project.config.iso.packages = ["other"]
    assert key() != original
    project.config.iso.packages = []
    assert iso_key(project, netinstall=True, volid=None, signer=None) != original
    assert iso_key(project, netinstall=False, volid="OTHER", signer=None) != original
    assert iso_key(project, netinstall=False, volid=None, signer=Signer("A" * 40)) != original
    for path in (project.root / "base/trivial/trivial.spec", project.root / "iso/tree/config"):
        before = key()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("changed")
        assert key() != before
    before = key()
    lock = load_lock(project)
    lock.sources["kernel"] = "1-1"
    save_lock(project, lock)
    assert key() != before
    # the units the installer masks, by their targets
    before = key()
    unit = project.root / "iso/installimg/etc/systemd/system/masked.service"
    unit.symlink_to("/dev/null")
    masked = key()
    assert masked != before
    unit.unlink()
    unit.symlink_to("/another/missing/target")
    assert key() != masked
    unit.unlink()
    assert key() == before
    # of a copy of xcraft, which the other tests do not read: not the bytecode Python leaves next to the scripts of
    # the build environment, which differs by checkout
    code = shutil.copytree(Path(iso.__file__).parent, project.root.parent / "xcraft")
    monkeypatch.setattr(iso, "__file__", str(code / "iso.py"))
    monkeypatch.setattr(container, "BUILDENV", code / "buildenv")
    before = key()
    (code / "buildenv/__pycache__").mkdir(exist_ok=True)
    (code / "buildenv/__pycache__/xcraft-test.cpython-0.pyc").write_bytes(b"bytecode")
    assert key() == before
    # but any other file of xcraft, like project.py, which gives the default volume id
    (code / "project.py").write_text(f"{(code / 'project.py').read_text()}\n")
    assert key() != before
    # the build IDs: the numbers of the builds, and the reuses of builds
    for accepts in (None, "45" * 32):
        before = key()
        builds = load_builds(project)
        builds.ids["trivial"] = BuildId(number=2, key="01" * 32, accepts=accepts)
        save_builds(project, builds)
        assert key() != before


def should_change_the_iso_key_with_the_inputs_of_the_builds(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    def key() -> str:
        return iso_key(project, netinstall=False, volid=None, signer=None)

    # what the build keys cover, before they are computed again, on all the architectures: the pins of the images and
    # of the build roots, even of the same versions, and how xcraft makes the builds
    pin = Pin(evr="1-1", arch="noarch", sha256="1" * 64, source="kernel")
    for arch in (project.arch, "aarch64"):
        lock = load_lock(project)
        lock.sources["kernel"] = "1-1"
        lock.binaries.setdefault(arch, {}).update({"image-tool": pin, "root-tool": pin})
        lock.image.setdefault(arch, []).append("image-tool")
        lock.roots.arches.setdefault(arch, {})["trivial"] = ["root-tool"]
        save_lock(project, lock)
        for tool in ("image-tool", "root-tool"):
            before = key()
            lock.binaries[arch][tool] = pin.model_copy(update={"sha256": "2" * 64})
            save_lock(project, lock)
            assert key() != before
    before = key()
    monkeypatch.setattr(buildkey, "FORMAT", buildkey.FORMAT + 1)
    assert key() != before


@needs_gpg
def should_get_a_cached_iso_without_preparing_the_project(project: Project, tmp_path: Path) -> None:
    # The cache transport treats ISO contents as opaque bytes. No RPM resolution is available in this project.
    image = tmp_path / "test.iso"
    image.write_bytes(b"cached ISO contents")
    signer = create_dev_key(project, "Developer")
    tree = cache_image(project, image, signer=signer, keep=True)
    code, output = xcraft("iso", "--locked")
    assert code == 0, output
    output = project.state / "iso" / iso_name(project, netinstall=False, signer=signer)
    assert output.read_bytes() == image.read_bytes()
    # the file of the cache, read-only: replaced, rather than changed
    assert not output.stat().st_mode & 0o222
    output.unlink()
    output.write_bytes(b"changed output")
    assert checkout_iso(project, signer=signer, dev="dev") == output
    assert output.read_bytes() == image.read_bytes()
    assert checkout_iso(project, tmp_path / "other.iso", signer=signer, dev="dev").read_bytes() == image.read_bytes()
    assert (tree / "image.iso").read_bytes() == image.read_bytes()


def should_refuse_a_corrupted_cached_iso(project: Project, tmp_path: Path) -> None:
    image = tmp_path / "test.iso"
    image.write_bytes(b"cached ISO contents")
    tree = cache_image(project, image, keep=True)
    (tree / "image.iso").write_bytes(b"corrupted cache")
    with pytest.raises(RuntimeError, match="does not match its SHA-256"):
        checkout_iso(project, dev="dev")


@needs_gpg
def should_check_the_configuration_before_the_snapshot(project: Project) -> None:
    code, output = xcraft("iso")
    assert code == 1, output
    assert "the ISO needs the installer's packages" in output
    (project.root / "xcraft.toml").write_text(f'{TOML}installer = ["bash"]\n')
    (project.root / "iso/hook").chmod(0o644)
    for arch in ("x86_64_v2", "aarch64"):  # both architectures of their boot loaders
        code, output = xcraft("iso", "--arch", arch)
        assert code == 1, output
        assert "the ISO needs the hook of the project" in output
    # neither prepared nor snapshot
    assert not (project.root / "xcraft.lock").exists()
    assert not (project.state / "publish").exists()


def should_not_cache_the_iso_when_the_lock_changed_meanwhile(
    project: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # like a checkout of another lock, while the ISO of the one before is assembled
    key = iso_key(project, netinstall=False, volid=None, signer=None)

    def assembled(project: Project, *_: object, **__: object) -> Path:
        lock = load_lock(project)
        lock.sources["kernel"] = "1-1"
        save_lock(project, lock)
        (made := project.state / "tmp/made.iso").parent.mkdir(parents=True, exist_ok=True)
        made.write_bytes(b"ISO of the lock before")
        return made

    monkeypatch.setattr(iso, "_assemble_iso", assembled)
    with pytest.raises(RuntimeError, match="ISO inputs changed while making the image"):
        snapshot_isos(project, project.root, publish.current_checkout(project), tmp_path / "isos", signer=None)
    assert not cached_iso(project, key).exists()
    assert not cached_iso(project, iso_key(project, netinstall=False, volid=None, signer=None)).exists()


def should_refuse_a_snapshot_of_another_checkout(
    project: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # like a git checkout after the snapshot, before its ISO is made: its key would be the new checkout's
    checkout = publish.current_checkout(project)
    lock = load_lock(project)
    lock.sources["kernel"] = "1-1"
    save_lock(project, lock)
    monkeypatch.setattr(iso, "_assemble_iso", lambda *_, **__: pytest.fail("assembled"))
    with pytest.raises(BuildError, match="the checkout changed while making the snapshot"):
        snapshot_isos(project, project.root, checkout, tmp_path / "isos", signer=None)


def should_reject_iso_versions_that_are_paths() -> None:
    with pytest.raises(ValueError, match="version"):
        Iso(name="toy", version="../../outside")


def should_name_isos_by_their_contents_and_architecture(project: Project) -> None:
    full = iso_name(project, netinstall=False, signer=None)
    assert re.fullmatch(rf"toy-1\.0-[0-9a-f]{{6}}-{project.arch}\.iso", full)
    # the same identifier for the netinstall ISO, and those of the other architectures
    ident = full.split("-")[2]
    assert iso_name(project, netinstall=True, signer=None) == f"toy-1.0-{ident}-{project.arch}-netinstall.iso"
    for platform in project.all_platforms():
        assert iso_id(platform, signer=None) == ident
    # another one for other contents
    (project.root / "base/trivial/trivial.spec").write_text("changed")
    assert iso_id(project, signer=None) != ident


def should_publish_the_isos_of_emulated_architectures_made_natively_only(project: Project, tmp_path: Path) -> None:
    # by xcraft iso on a host of their architecture, as the programs of their packages run to make them: never here
    emulated = replace(project, platform="linux/arm64" if os.uname().machine == "x86_64" else "linux/amd64/v2")
    assert not emulated.native
    for dev in (None, "dev"):
        with pytest.raises(RuntimeError, match=f"the ISOs of {emulated.arch} are made natively, not here"):
            publish_command._check_emulated_isos([emulated], signer=None, dev=dev)
    # cached, like pushed and fetched, or, in dev mode, copied from the cache of the host which made them
    image = tmp_path / "test.iso"
    image.write_bytes(b"made natively")
    for netinstall, keep in itertools.product((False, True), (False, True)):
        cache_image(emulated, image, netinstall=netinstall, keep=keep)
    for dev in (None, "dev"):
        publish_command._check_emulated_isos([emulated], signer=None, dev=dev)
        assert iso.fetch_isos(emulated, signer=None, dev=dev)
    # those of the host's architecture are made here, when missing
    publish_command._check_emulated_isos([project], signer=None, dev=None)
    assert not iso.fetch_isos(project, signer=None)
