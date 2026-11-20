"""Publishing snapshots of the repositories and ISOs to the remote storage, and what is made from them: their
retention, metalinks, mirrors, and the maintenance branches.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import shutil
import subprocess
import time
import urllib.request
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import pytest

from tests import (
    ARCH,
    PLATFORM,
    REMOTE,
    STAGE,
    TOYS,
    WEB,
    WEB_PORT,
    commit_base,
    edit,
    garage,
    git,
    needs_gpg,
    page,
    pushed_iso,
    unsigned,
    xcraft,
)  # fmt: skip
from xcraft import (
    buildid,
    metalink,
    mirror,
    prune,
    remote,
    remote_pages,
    repodata,
    upload,
    upstream_copy,
    upstream_signed,
)
from xcraft import publish as publisher
from xcraft.cache import BuildError
from xcraft.iso import iso_name, push_isos
from xcraft.lock import Pin, load_lock
from xcraft.project import Project, Remote, Retention
from xcraft.resolution import resolve_image
from xcraft.signing import KEY_FILE, check_signatures, create_dev_key, dev_signer, key_signer, release_signer
from xcraft.sources import sha256_of

ARCHES = {"x86_64_v2": {PLATFORM: ARCH, "linux/amd64": "x86_64"}}.get(ARCH, {PLATFORM: ARCH})
"""The architectures the host runs, by platform, all built for: two on x86_64."""


@pytest.fixture
def root(storage_root: Path) -> Path:
    return storage_root


def should_copy_pushed_isos_to_snapshots_rather_than_upload_them(
    root: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    project = Project.find()
    signer, image, _ = pushed_iso(project, tmp_path)
    assert "0 ISOs pushed, 1 already" in xcraft("push", "isos")[1]
    storage = project.config.remote["dev"]
    snapshot = tmp_path / "snapshot"
    published = snapshot / "iso" / iso_name(project, netinstall=False, signer=signer)
    published.parent.mkdir(parents=True)
    shutil.copy2(image, published)
    with caplog.at_level(logging.DEBUG, logger="xcraft"):
        stored = push_isos(project, storage, signer=signer)
        upload.Uploader(project, storage, signer, build=False).upload_isos(snapshot, "cache-reuse", stored=stored)
    assert any(m.startswith("copying ") and "/image.iso to " in m for m in caplog.messages)
    assert not any(m.startswith(f"uploading {published} ") for m in caplog.messages)
    assert not any(m.startswith("downloading ") and f"/{published.name} to " in m for m in caplog.messages)
    # with the checksums of the ISOs of all the architectures, signed
    manifest = remote.read_manifest(storage, f"isos/9.0/{STAGE}/cache-reuse", trusted=signer)
    assert {published.name, upload.SUMS, f"{upload.SUMS}.asc"} <= manifest.keys()
    sums = tmp_path / upload.SUMS
    assert remote.download(storage, f"isos/9.0/{STAGE}/cache-reuse/{upload.SUMS}", sums)
    assert sums.read_text() == f"{sha256_of(image)}  {published.name}\n"


def should_index_the_source_packages_reused_from_the_latest_snapshot(root: Path, tmp_path: Path) -> None:
    platforms = ", ".join(f'"{p}"' for p in ARCHES)
    (root / "xcraft.toml").write_text(REMOTE.replace(f'["{PLATFORM}"]', f"[{platforms}]"))
    storage = Project.find().config.remote["main"]
    # with the builds of all the architectures in the storage, like CI pushes them, and a change of with-deps only:
    # the source package of trivial, reused from the latest snapshot, is the one of its metadata
    for arch in ARCHES.values():
        assert xcraft("build", "--arch", arch)[0] == 0
        assert xcraft("push", "builds", "--to", "main", "--arch", arch)[0] == 0
    stage = f"sources-{secrets.token_hex(4)}"
    assert xcraft("publish", "repos", "--to", "main", "--stage", stage)[0] == 0
    spec = root / "base/with-deps/with-deps.spec"
    spec.write_text(spec.read_text().replace('echo "with-deps"', 'echo "with-deps, changed"'))
    assert xcraft("publish", "repos", "--to", "main", "--stage", stage)[0] == 0
    assert remote.download_tree(storage, f"repos/9.0/{stage}/latest", tmp_path / "latest")
    sources = tmp_path / "latest/base/Source"
    srpms = {sha256_of(srpm) for srpm in sources.glob("*.src.rpm")}
    assert srpms
    assert srpms <= (repodata.read(sources) or {}).keys()


def should_publish_dated_and_latest_snapshots_of_all_architectures(root: Path) -> None:
    arches = list(ARCHES.values())
    platforms = ", ".join(f'"{p}"' for p in ARCHES)
    (root / "xcraft.toml").write_text(REMOTE.replace(f'["{PLATFORM}"]', f"[{platforms}]") + 'extra = ["bc"]\n')
    code, output = xcraft("publish", "--to", "main")
    assert code == 0, output
    assert {"bc", "glibc"} <= set(load_lock(Project.find()).runtime[ARCH])
    bc = resolve_image(Project.find()).packages["bc"]
    assert bc.repo == "baseos"
    storage = Project.find().config.remote.get("main")
    assert storage
    day = output.split(f"published repos/9.0/{STAGE}/")[1].split(",")[0]  # of the publish, which may be yesterday
    for name in (day, "latest"):
        base = f"repos/9.0/{STAGE}/{name}/base"
        for repo in (*arches, "Source"):
            assert remote.exists(storage, f"{base}/{repo}/repodata/repomd.xml")
            assert remote.exists(
                storage, f"{base}/{repo}/trivial-1.0-1+b1.xcpng8.99.{'src' if repo == 'Source' else 'noarch'}.rpm"
            )
        # the sources of the upstream packages too
        assert remote.exists(storage, f"{base}/Source/bc-{bc.evr}.src.rpm")
        assert 'href="trivial-1.0-1%2Bb1.xcpng8.99.noarch.rpm"' in page(f"{base}/{arches[-1]}/")
    # a second one the same day, without changing the locks, of the repositories only, like all without [iso]
    code, output = xcraft("publish", "repos", "--to", "main", "--locked")
    assert code == 0, output
    assert f"published repos/9.0/{STAGE}/{day}.2, and as repos/9.0/{STAGE}/latest" in output
    assert not remote.tree_exists(storage, f"isos/9.0/{STAGE}/latest")
    # An explicit stage has an independent snapshot sequence and latest tree.
    incoming = f"{STAGE}-incoming"
    code, output = xcraft("publish", "--to", "main", "--stage", incoming, "repos", "--locked")
    assert code == 0, output
    assert f"published repos/9.0/{incoming}/{day}, and as repos/9.0/{incoming}/latest" in output
    assert remote.tree_exists(storage, f"repos/9.0/{STAGE}/latest")
    assert remote.tree_exists(storage, f"repos/9.0/{incoming}/latest")
    code, output = xcraft("publish", "isos", "--to", "main")
    assert code == 2
    assert "no ISO to publish: see [iso] in xcraft.toml" in output
    # a local one is of the host's architecture, unless asked; signed again, from the originals, which the snapshots
    # do not have, but the upstream copy
    shutil.rmtree(root / ".xcraft/cache/rpms")
    code, output = xcraft("publish", "--local")
    assert code == 0, output
    assert sorted(p.name for p in (root / ".xcraft/publish/base").iterdir()) == ["Source", ARCH]
    if len(arches) > 1:
        assert f"warning: only {ARCH} is published, not x86_64: --arch all publishes them all" in output
        code, output = xcraft("publish", "--local", "--arch", "x86_64")
        assert code == 0, output
        assert "warning" not in output
        assert sorted(p.name for p in (root / ".xcraft/publish/base").iterdir()) == ["Source", "x86_64"]


def should_lock_what_is_published_when_preparing(root: Path) -> None:
    # what our packages need at runtime, like the build roots
    (root / "xcraft.toml").write_text(f'{REMOTE}extra = ["bc"]\n')
    assert xcraft("build", "--dry-run")[0] == 0
    buildid.update(Project.find())  # and the build IDs
    (root / "xcraft.toml").write_text(REMOTE)
    # the options of publish apply to its subcommands too, given before them
    for args in (["publish", "--locked", "--local"], ["publish", "--locked", "--local", "repos"],
                 ["publish", "--locked", "repos", "--local"], ["build", "--locked", "--dry-run"]):  # fmt: skip
        code, output = xcraft(*args)
        assert code == 1
        assert re.search(rf"--locked forbids: .*-runtime\.{ARCH} bc\b", output)


def should_refuse_upstream_packages_not_signed_by_their_repositories(root: Path) -> None:
    (root / "xcraft.toml").write_text(f'{REMOTE}extra = ["bc"]\n')
    assert xcraft("build", "trivial")[0] == 0
    bc = resolve_image(Project.find()).packages["bc"]
    srpm = root / f".xcraft/cache/srpms/bc-{bc.evr}.src.rpm"
    srpm.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(next(root.glob(f".xcraft/cache/builds/*/{ARCH}/trivial/*/SRPMS/*.src.rpm")), srpm)
    code, output = xcraft("publish", "--local")
    assert code == 1
    assert f"✗ base/Source/{srpm.name}: digests SIGNATURES NOT OK" in output
    assert "some upstream packages are not signed with the keys of the repositories" in output


@needs_gpg
def should_sign_published_packages_and_metadata(root: Path) -> None:  # noqa: PLR0915
    toml = unsigned(REMOTE)
    (root / "xcraft.toml").write_text(toml)
    code, output = xcraft("publish", "--to", "main")
    assert code == 2
    assert "no key to sign the packages" in " ".join(output.replace("│", "").split())
    signer = create_dev_key(Project.find(), "Release <release@example.com>")
    try:
        # the developer's key, without a release one
        code, output = xcraft("publish", "--local")
        assert code == 0, output
        assert (root / ".xcraft/publish/RPM-GPG-KEY").read_text() == signer.public_key()
        # Its remote builds and snapshots are trusted without adding the developer key to the config.
        code, output = xcraft("push", "builds", "--to", "main")
        assert code == 0, output
        shutil.rmtree(root / ".xcraft/cache/builds")
        code, output = xcraft("fetch", "builds")
        assert code == 0, output
        assert "fetched" in output
        code, output = xcraft("publish", "--to", "main")
        assert code == 0, output
        storage = Project.find().config.remote.get("main")
        assert storage
        assert remote.tree_exists(storage, f"repos/9.0/{STAGE}/latest", trusted=signer)
        assert not remote.tree_exists(storage, f"repos/9.0/{STAGE}/latest")
        # again, on that latest snapshot, which the configuration does not trust
        assert xcraft("publish", "--to", "main")[0] == 0
        (root / "xcraft.toml").write_text(
            re.sub(r"build_keys = \[.*?\]", f'build_keys = ["{signer.key}"]', toml)
            + f'[sign]\nkey = "{signer.key}"\ngnupghome = "{signer.gnupghome}"\n'
        )
        code, output = xcraft("publish", "--to", "main")
        assert code == 0, output
        snapshot = root / ".xcraft/publish"
        assert (snapshot / "RPM-GPG-KEY").read_text() == signer.public_key()
        for repo in (ARCH, "Source"):
            assert (snapshot / "base" / repo / "repodata/repomd.xml.asc").exists()
        ours = [f"base/{ARCH}/trivial-1.0-1+b1.xcpng8.99.noarch.rpm", "base/Source/trivial-1.0-1+b1.xcpng8.99.src.rpm"]
        check_signatures(Project.find(), snapshot, ours, what="packages of ours", public_key=signer.public_key())
        storage = Project.find().config.remote.get("main")
        assert storage
        assert remote.exists(storage, f"repos/9.0/{STAGE}/latest/base/{ARCH}/repodata/repomd.xml.asc")
        signed = list((root / ".xcraft/cache/builds" / signer.key / ARCH / "trivial").iterdir())
        assert remote.tree_exists(storage, f".xcraft/builds/{signer.key}/{ARCH}/trivial/{signed[0].name}")
        # signed once: the next snapshots have the same files
        rpm = signed[0] / "RPMS/noarch/trivial-1.0-1+b1.xcpng8.99.noarch.rpm"
        before = rpm.read_bytes()
        assert xcraft("publish", "--to", "main")[0] == 0
        assert rpm.read_bytes() == before
        config = root / "xcraft.toml"
        config.write_text(config.read_text().replace(f'key = "{signer.key}"', f'key = "{"0" * 40}"'))
        code, output = xcraft("publish", "--key", signer.key, "--to", "main", "repos", "--locked")
        assert code == 0, output
        # Developer publishing defaults to the independent dev bucket and ignores the configured release key.
        code, output = xcraft("publish", "repos", "--locked")
        assert code == 0, output
        dev_storage = Project.find().config.remote["dev"]
        assert remote.tree_exists(dev_storage, f"repos/9.0/{STAGE}/latest", trusted=signer)
        assert not remote.tree_exists(dev_storage, f"repos/9.0/{STAGE}/latest")
        assert remote.tree_exists(
            dev_storage, f".xcraft/builds/{signer.key}/{ARCH}/trivial/{signed[0].name}", trusted=signer
        )
    finally:
        subprocess.run(["gpgconf", "--homedir", str(signer.gnupghome), "--kill", "gpg-agent"], check=False)


def should_start_maintenance_branches_at_the_latest_published_commit(root: Path) -> None:
    (root / ".gitignore").write_text(".xcraft/\n")
    code, output = xcraft("publish", "repos", "--to", "main")  # of the platforms of the test, locking them
    assert code == 0, output
    commit_base(root)
    release = git(root, "rev-parse", "HEAD")
    # published again, from the commit: the same snapshot, reused, with the commit
    code, output = xcraft("publish", "repos", "--to", "main", "--locked")
    assert code == 0, output
    assert "(reused the matching snapshot)" in output
    edit(root, "after the release")
    git(root, "commit", "-qam", "after the release")
    # at the commit of its latest snapshot
    code, output = xcraft("maint", "start", "--stage", STAGE)
    assert code == 0, output
    assert f"maint/9.0, from {release[:12]}" in output
    assert git(root, "rev-parse", "maint/9.0") == release
    assert "maint/9.0 exists already" in xcraft("maint", "start", "--stage", STAGE)[1]
    # of another pattern
    config = root / "xcraft.toml"
    config.write_text(f'maintenance_branches = ["maint/*", "embargo/*"]\n{config.read_text()}')
    code, output = xcraft("maint", "start", "--stage", STAGE, "--name", "embargo/xsa-123")
    assert code == 0, output
    assert git(root, "rev-parse", "embargo/xsa-123") == release
    code, output = xcraft("maint", "start", "--stage", STAGE, "--name", "fix/xsa-123")
    assert code == 1
    assert "fix/xsa-123 is of none of the maintenance_branches: maint/*, embargo/*" in output
    with pytest.raises(subprocess.CalledProcessError):
        git(root, "rev-parse", "-q", "--verify", "fix/xsa-123")
    assert "no published snapshot in repos/9.0/nothing/latest" in xcraft("maint", "start", "--stage", "nothing")[1]


def should_reuse_the_metadata_of_the_latest_snapshot(
    root: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A snapshot has the metadata of the packages of the latest one, without downloading them, and the packages
    rebuilt since.
    """
    stage = f"metadata-{secrets.token_hex(4)}"
    (root / "xcraft.toml").write_text(f'{REMOTE}extra = ["bc"]\n')
    code, output = xcraft("publish", "repos", "--to", "main", "--stage", stage)
    assert code == 0, output
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(spec.read_text().replace('echo "hello"', 'echo "hello, rebuilt"'))
    with caplog.at_level(logging.DEBUG, logger="xcraft"):
        code, output = xcraft("publish", "repos", "--to", "main", "--stage", stage)
    assert code == 0, output
    assert not [m for m in caplog.messages if "downloading " in m and ".rpm to " in m]
    # each manifest checked once
    manifests = [
        m.split()[1] for m in caplog.messages if m.startswith("downloading ") and f"/{remote.MANIFEST} to " in m
    ]
    assert len(manifests) == len(set(manifests))
    # the unchanged packages are already in latest
    latest = f"s3://xcraft/repos/9.0/{stage}/latest/"
    copied = [m.split(" to ")[-1] for m in caplog.messages if m.startswith("copying ") and f" to {latest}" in m]
    assert copied
    assert [f for f in copied if f.endswith(".rpm") and not Path(f).name.startswith(("trivial-", "with-deps-"))] == []
    # only those rebuilt, trivial and the package built against it, not the upstream ones
    local = {p.name for p in root.glob(".xcraft/publish/**/*.rpm")}
    assert local
    assert [name for name in local if not name.startswith(("trivial-", "with-deps-"))] == []
    storage = Project.find().config.remote["main"]
    latest = f"repos/9.0/{stage}/latest"
    manifest = remote.read_manifest(storage, latest)
    # trivial rebuilt, keeping its version-release: published rather than the previous build of the storage
    rpm = f"base/{ARCH}/trivial-1.0-1+b1.xcpng8.99.noarch.rpm"
    published = re.search(r"published (repos/\S+),", output)
    assert published, output
    for name in (published[1], latest):
        assert remote.read_manifest(storage, name)[rpm] == sha256_of(root / ".xcraft/publish" / rpm)
    assert remote.download(storage, f"{latest}/{rpm}", tmp_path / "rebuilt.rpm")
    assert sha256_of(tmp_path / "rebuilt.rpm") == sha256_of(root / ".xcraft/publish" / rpm)
    bc = resolve_image(Project.find()).packages["bc"]
    assert {f"base/{ARCH}/bc-{bc.evr}.{bc.arch}.rpm", f"base/Source/bc-{bc.evr}.src.rpm"} <= manifest.keys()
    for repo in (f"base/{ARCH}", "base/Source"):
        assert remote.download(storage, f"{latest}/{repo}/repodata/repomd.xml", tmp_path / repo / "repodata/repomd.xml")
        for href in repodata.files((tmp_path / repo / "repodata/repomd.xml").read_bytes()).values():
            assert remote.download(storage, f"{latest}/{repo}/{href}", tmp_path / repo / href)
        entries = repodata.read(tmp_path / repo)
        assert entries
        assert {f"{repo}/{repodata.location(e)}": pkgid for pkgid, e in entries.items()} == {
            f: digest for f, digest in manifest.items() if f.startswith(f"{repo}/") and f.endswith(".rpm")
        }
    # the packages of a repository, for its ISO
    rpms = {f: publisher.Stored(f"{latest}/{f}", digest) for f, digest in manifest.items() if f.endswith(".rpm")}
    publisher.materialize(storage, tmp_path / "packages", rpms, f"base/{ARCH}")
    assert {p.relative_to(tmp_path / "packages").as_posix() for p in (tmp_path / "packages").rglob("*.rpm")} == {
        f for f in rpms if f.startswith(f"base/{ARCH}/")
    }


def should_reuse_the_latest_snapshot_only_when_nothing_changed(root: Path, caplog: pytest.LogCaptureFixture) -> None:
    stage = f"reuse-{secrets.token_hex(4)}"
    code, output = xcraft("publish", "repos", "--to", "main", "--stage", stage)
    assert code == 0, output
    project = Project.find()
    project.config.publish.stage = stage
    for name in ("publish", "cache/builds", "cache/rpms", "cache/srpms", "cache/checks"):
        shutil.rmtree(project.state / name, ignore_errors=True)
    with caplog.at_level(logging.DEBUG, logger="xcraft"):
        summary = project.state.parent / "summary.md"
        code, output = xcraft(
            "publish", "repos", "--to", "main", "--stage", stage, "--locked", "--summary", str(summary)
        )
    assert code == 0, output
    assert "reused the matching snapshot" in output
    target = output.split("✓ published ")[1].split(",")[0]
    # its summary too, for CI
    assert f"/{target}/" in summary.read_text()
    assert not (project.state / "publish").exists()
    assert not (project.state / "cache/builds").exists()
    assert not any(
        "podman " in message or ("downloading " in message and ".rpm to " in message) for message in caplog.messages
    )
    storage = project.config.remote["main"]
    prefix = f"repos/9.0/{stage}"
    manifest = remote.read_manifest(storage, target)
    assert manifest
    # but their metalinks, of their own trees
    links = {f for f in manifest if f.endswith(f"/{metalink.FILE}")}
    assert links
    assert {f: d for f, d in manifest.items() if f not in links} == {
        f: d for f, d in remote.read_manifest(storage, f"{prefix}/latest").items() if f not in links
    }
    # with its own .repo file, of its tree, in the latest one too
    for tree in (target, f"{prefix}/latest"):
        assert remote.download(storage, f"{tree}/xcraft-{stage}-{ARCH}.repo", project.state / "reused.repo")
        assert f"/{target}/base/{ARCH}/" in (project.state / "reused.repo").read_text()
    # what it was made from is out of it
    assert not [f for f in manifest if "inputs" in f]
    assert remote.read_manifest(storage, upload.inputs_tree(f"{prefix}/latest")).keys() == {upload.INPUTS}

    project.config.publish.extra.append("bc")
    assert not upload.Uploader(project, storage, release_signer(project), build=False).reuse(
        [project], only="repos", isos=[]
    )
    project.config.publish.extra.clear()
    # nor for dev mode, whose packages keep their signatures, even without development builds
    assert not upload.Uploader(project, storage, release_signer(project), build=False, dev=True).reuse(
        [project], only="repos", isos=[]
    )
    # nor another latest snapshot than the one it was made from
    signer = release_signer(project)
    assert signer
    uploader = upload.Uploader(project, storage, signer, build=False)
    latest = remote.read_manifest(storage, f"{prefix}/latest")
    remote.write_manifest(storage, f"{prefix}/latest", {**latest, "other": "0" * 64}, signer=signer)
    assert not uploader.reuse([project], only="repos", isos=[])
    remote.write_manifest(storage, f"{prefix}/latest", latest, signer=signer)
    assert uploader.reuse([project], only="repos", isos=[])
    # nor with a package missing in the latest snapshot: assembled again, as the name of the copy, deleted
    remote.delete(storage, [f"{prefix}/latest/{next(f for f in manifest if f.endswith('.rpm'))}"])
    name = uploader.snapshot_name()
    assert not uploader.reuse([project], only="repos", isos=[])
    assert not remote.listing(storage, f"{prefix}/{name}/")
    # nor with what it was made from changed
    remote.upload(storage, f"{upload.inputs_tree(f'{prefix}/latest')}/{upload.INPUTS}", root / "xcraft.toml")
    assert not uploader.reuse([project], only="repos", isos=[])


def should_select_the_dated_snapshots_past_their_days() -> None:
    names = {"latest", "2026-01-01", "2026-01-01.2", "2026-01-01.10", "2026-02-01", "other"}
    assert upload.expired_snapshots(names, 30, date(2026, 3, 1)) == ["2026-01-01", "2026-01-01.10", "2026-01-01.2"]
    # the latest one is kept, however old, and the order of the suffixes is numeric
    assert upload.expired_snapshots(names, 30, date(2026, 3, 5)) == ["2026-01-01", "2026-01-01.10", "2026-01-01.2"]
    assert upload.expired_snapshots({"2026-01-01", "2026-01-01.2", "2026-01-01.10"}, 1, date(2026, 3, 1)) == [
        "2026-01-01", "2026-01-01.2",
    ]  # fmt: skip


@needs_gpg
def should_remove_expired_snapshots_of_stages_with_snapshot_days(root: Path) -> None:
    stage = f"expiry-{secrets.token_hex(4)}"
    code, output = xcraft("publish", "repos", "--to", "main", "--stage", stage)
    assert code == 0, output
    project = Project.find()
    storage, signer = project.config.remote["main"], release_signer(project)
    assert signer
    # older ones, of the repositories and of the ISOs
    old = [f"{kind}/9.0/{stage}/{name}" for kind in ("repos", "isos") for name in ("2020-01-01", "2020-01-01.2")]
    for tree in old:
        remote.upload(storage, f"{tree}/file", root / "xcraft.toml")
        remote.write_manifest(storage, tree, {"file": sha256_of(root / "xcraft.toml")}, signer=signer)
    remote_pages.update_pages(project, storage, [f"{tree}/" for tree in old])
    # kept without snapshot_days, like the stages of the releases
    code, output = xcraft("publish", "repos", "--to", "main", "--stage", stage, "--locked")
    assert code == 0, output
    assert [tree for tree in old if not remote.tree_exists(storage, tree)] == []
    (root / "xcraft.toml").write_text(f"{REMOTE}\n[publish.stages.{stage}]\nsnapshot_days = 30\n")
    code, output = xcraft("publish", "repos", "--to", "main", "--stage", stage, "--locked")
    assert code == 0, output
    # but the latest ISOs, which have no newer ones
    assert f"removed {', '.join(old[:3])}, published more than 30 days ago" in " ".join(output.split())
    for tree in old[:3]:
        assert not remote.listing(storage, f"{tree}/")
        assert f'href="{tree.rsplit("/", 1)[1]}/"' not in page(f"{tree.rsplit('/', 1)[0]}/")
    assert remote.tree_exists(storage, old[3])
    today = output.split(f"published repos/9.0/{stage}/")[1].split(",")[0]
    assert remote.tree_exists(storage, f"repos/9.0/{stage}/{today}")
    assert remote.tree_exists(storage, f"repos/9.0/{stage}/latest")


def should_keep_recent_versions_and_the_newest_older_ones() -> None:
    now = datetime(2026, 3, 1, tzinfo=UTC)
    older = {f"base/x/a-{v}.rpm": ("a", "noarch") for v in (1, 2, 3, 10)} | {"base/x/gone-1.rpm": ("gone", "noarch")}
    left = {
        "base/x/a-1.rpm": datetime(2026, 1, 1, tzinfo=UTC), "base/x/a-2.rpm": datetime(2026, 1, 2, tzinfo=UTC),
        "base/x/a-3.rpm": datetime(2026, 1, 2, 10, tzinfo=UTC), "base/x/a-10.rpm": datetime(2026, 2, 20, tzinfo=UTC),
        "base/x/gone-1.rpm": datetime(2026, 1, 1, tzinfo=UTC),
    }  # fmt: skip
    current = {("base/x", "a", "noarch")}
    # the recent ones, and the newest of the packages of the snapshot, however old: not those it does not have
    assert upload.kept_versions(older, left, current, Retention(days=30), now) == {"base/x/a-10.rpm"}
    # the newest by when they left, of the same day too, not by their names
    assert upload.kept_versions(older, left, current, Retention(days=30, keep=3), now) == {
        "base/x/a-3.rpm", "base/x/a-10.rpm",
    }  # fmt: skip
    assert upload.kept_versions(older, left, set(), Retention(days=30, keep=3), now) == {"base/x/a-10.rpm"}
    # all of them, without days
    assert upload.kept_versions(older, left, current, Retention(), now) == set(older)


def _versions(storage: Remote, tree: str) -> list[str]:
    """Return the versions of trivial in the snapshot `tree`, as its metadata lists them."""
    manifest = remote.read_manifest(storage, tree)
    known, _ = publisher.published_packages(Project.find(), storage, None, tree)
    return sorted(
        repodata.nevra(known[digest][0])[1].split("-")[0]
        for f, digest in manifest.items()
        if f.startswith(f"base/{ARCH}/trivial-") and digest in known
    )


@needs_gpg
def should_keep_older_versions_in_cumulative_stages(root: Path) -> None:  # noqa: PLR0915
    stage = f"cumulative-{secrets.token_hex(4)}"
    (root / "xcraft.toml").write_text(f"{REMOTE}\n[publish.stages.{stage}]\nretention = {{ days = 30, keep = 2 }}\n")
    spec = root / "base/trivial/trivial.spec"
    project = Project.find()
    storage, latest = project.config.remote["main"], f"repos/9.0/{stage}/latest"
    names = []
    for version in ("1.0", "1.1", "1.2"):
        spec.write_text(re.sub(r"^Version: .*$", f"Version: {version}", spec.read_text(), flags=re.MULTILINE))
        code, output = xcraft("publish", "repos", "--to", "main", "--stage", stage)
        assert code == 0, output
        names.append(output.split(f"published repos/9.0/{stage}/")[1].split(",")[0])
        # the dated snapshot is exact, the latest one has the older versions too, in its metadata
        assert _versions(storage, f"repos/9.0/{stage}/{names[-1]}") == [version]
    assert _versions(storage, latest) == ["1.0", "1.1", "1.2"]
    manifest = remote.read_manifest(storage, latest)
    sources = sorted(f for f in manifest if f.startswith("base/Source/trivial-"))
    assert [s.split("-")[1] for s in sources] == ["1.0", "1.1", "1.2"]
    # as if 1.0 and 1.1 left it long ago: 1.1 is kept, the newest older version of a package it has, 1.0 goes with
    # its source package, at the next publication, the latest snapshot reused
    signer = release_signer(project)
    assert signer
    assert remote.download_tree(storage, upload.inputs_tree(latest), root / "inputs")
    inputs = upload.SnapshotInputs.model_validate_json((root / "inputs" / upload.INPUTS).read_bytes())
    assert sorted(inputs.left) == sorted(f for f in manifest if "trivial-1.0-" in f or "trivial-1.1-" in f)
    old = inputs.model_copy(
        update={"left": {f: f"2020-01-0{2 if 'trivial-1.1-' in f else 1}T00:00:00+00:00" for f in inputs.left}}
    )
    with project.tmp_dir("inputs") as work:
        (work / upload.INPUTS).write_text(old.model_dump_json())
        remote.upload_tree(storage, upload.inputs_tree(latest), work, [upload.INPUTS], signer=signer)
    code, output = xcraft("publish", "repos", "--to", "main", "--stage", stage, "--locked")
    assert code == 0, output
    assert "reused the matching snapshot" in output
    assert _versions(storage, latest) == ["1.1", "1.2"]
    manifest = remote.read_manifest(storage, latest)
    assert sorted(f.split("-")[1] for f in manifest if f.startswith("base/Source/trivial-")) == ["1.1", "1.2"]
    assert not any("trivial-1.0-" in f for f in remote.listing(storage, f"{latest}/"))
    # published again unchanged, its metadata are the same, rather than new ones the hosts would download
    code, output = xcraft("publish", "repos", "--to", "main", "--stage", stage, "--locked")
    assert code == 0, output
    metadata = {f: d for f, d in manifest.items() if "/repodata/" in f}
    assert {f: d for f, d in remote.read_manifest(storage, latest).items() if "/repodata/" in f} == metadata
    # a publication interrupted, which left it without its manifest, loses none of its older versions: they are in
    # what it was made from; 1.2 is kept, the newest older one, 1.1 goes, which left it long ago
    remote.delete(storage, [f"{latest}/{remote.MANIFEST}"])
    spec.write_text(spec.read_text().replace("Version: 1.2", "Version: 1.3"))
    code, output = xcraft("publish", "repos", "--to", "main", "--stage", stage)
    assert code == 0, output
    assert _versions(storage, latest) == ["1.2", "1.3"]
    # nor when the metadata of an older version cannot be read: the publication fails
    primary = next(
        f for f in remote.read_manifest(storage, latest) if f.startswith(f"base/{ARCH}/repodata/") and "-primary." in f
    )
    remote.delete(storage, [f"{latest}/{primary}"])
    spec.write_text(spec.read_text().replace("Version: 1.3", "Version: 1.4"))
    code, output = xcraft("publish", "repos", "--to", "main", "--stage", stage)
    assert code == 1
    assert "whose older versions would be lost" in " ".join(output.split())
    # nor when, without its manifest, what it was made from is signed by a key no longer trusted
    assert remote.download_tree(storage, upload.inputs_tree(latest), root / "failed")
    inputs = upload.SnapshotInputs.model_validate_json((root / "failed" / upload.INPUTS).read_bytes())
    remote.delete(storage, [f"{latest}/{remote.MANIFEST}"])
    with project.tmp_dir("inputs") as work:
        (work / upload.INPUTS).write_text(inputs.model_dump_json())
        other = create_dev_key(project, "Other")
        remote.upload_tree(storage, upload.inputs_tree(latest), work, [upload.INPUTS], signer=other)
    remote.forget()
    code, output = xcraft("publish", "repos", "--to", "main", "--stage", stage)
    assert code == 1
    assert "has a manifest no trusted key signs" in " ".join(output.split())


@needs_gpg
@pytest.mark.parametrize("cumulative", [False, True])
def should_publish_metalinks_with_mirrors_and_previous_metadata(
    root: Path, tmp_path: Path, *, cumulative: bool
) -> None:
    stage = f"metalinks-{secrets.token_hex(4)}"
    retention = "retention = { days = 30 }\n" if cumulative else ""
    mirror = "https://mirror.example/xcp-ng"
    (root / "xcraft.toml").write_text(f'{REMOTE}\n[publish.stages.{stage}]\nmirrors = ["{mirror}/"]\n{retention}')
    storage = Project.find().config.remote["main"]
    repo, latest = f"base/{ARCH}/repodata", f"repos/9.0/{stage}/latest"

    def read(tree: str, file: str) -> bytes:
        assert remote.download(storage, f"{tree}/{repo}/{file}", tmp_path / file)
        assert sha256_of(tmp_path / file) == remote.read_manifest(storage, tree)[f"{repo}/{file}"]
        return (tmp_path / file).read_bytes()

    hashes = []
    for version in ("1.0", "1.1"):
        spec = root / "base/trivial/trivial.spec"
        spec.write_text(re.sub(r"^Version: .*$", f"Version: {version}", spec.read_text(), flags=re.MULTILINE))
        code, output = xcraft("publish", "repos", "--to", "main", "--stage", stage)
        assert code == 0, output
        name = output.split(f"published repos/9.0/{stage}/")[1].split(",")[0]
        dated = f"repos/9.0/{stage}/{name}"
        # the dated snapshot on our server only, the latest one on the mirrors first
        for tree, servers in ((dated, [WEB]), (latest, [mirror, WEB])):
            text = read(tree, metalink.FILE).decode()
            assert re.findall(r'preference="\d+">([^<]*)<', text) == [
                f"{server}/{tree}/{repo}/repomd.xml" for server in servers
            ]
            assert (
                re.findall(r'<hash type="sha256">([0-9a-f]{64})</hash>', text)[0]
                == hashlib.sha256(read(tree, "repomd.xml")).hexdigest()
            )
        hashes.append(hashlib.sha256(read(latest, "repomd.xml")).hexdigest())
    # with the previous repomd.xml of the latest snapshot as an alternate
    assert (
        re.findall(r'<hash type="sha256">([0-9a-f]{64})</hash>', read(latest, metalink.FILE).decode()) == hashes[::-1]
    )


@needs_gpg
def should_prune_the_builds_no_publication_has(root: Path, tmp_path: Path) -> None:
    # a bucket of its own: the others have the builds of the tests running meanwhile
    garage("bucket", "create", bucket := f"xcraft-prune-{secrets.token_hex(4)}")
    garage("bucket", "allow", "--read", "--write", bucket, "--key", "test")
    garage("bucket", "website", "--allow", bucket)
    web = f"http://{bucket}.web.garage.localhost:{WEB_PORT}"
    (root / "xcraft.toml").write_text(REMOTE.replace(WEB, web).replace('bucket = "xcraft"\n', f'bucket = "{bucket}"\n'))
    code, published_output = xcraft("publish", "repos", "--to", "main")
    assert code == 0, published_output
    project = Project.find()
    storage, signer = project.config.remote["main"], release_signer(project)
    assert signer
    manifests = [f for f in remote.listing(storage, f"{prune.BUILDS}/") if f.endswith(f"/{remote.MANIFEST}")]
    published = [f"{prune.BUILDS}/{f.removesuffix(f'/{remote.MANIFEST}')}" for f in manifests]
    assert published
    # a build no publication has, with the public key of its signer, like the publications, and one pushed again
    # with other files, whose previous generation is garbage
    unpublished, again = (f"{prune.BUILDS}/{signer.key}/{ARCH}/{name}/key" for name in ("unpublished", "again"))
    for name in ("a.rpm", "b.rpm"):
        (tmp_path / name).write_text(name)
    (tmp_path / KEY_FILE).write_text(signer.public_key())
    remote.upload_tree(storage, unpublished, tmp_path, ["a.rpm", KEY_FILE], signer=signer)
    remote.upload_tree(storage, again, tmp_path, ["a.rpm"], signer=signer)
    first = remote.read_tree(storage, again, trusted=signer)[0]
    written = remote.modified(storage, f"{again}/{remote.MANIFEST}")
    assert written
    between = written + timedelta(seconds=1)  # the dates of the storage are in seconds
    time.sleep(max(0, (between - datetime.now(UTC)).total_seconds()))
    remote.forget()
    remote.upload_tree(storage, again, tmp_path, ["b.rpm"], signer=signer)
    found = prune.pruned(storage, 0, between)
    assert found.trees == [unpublished]
    assert sorted(found.files) == sorted(
        [f"{first}/a.rpm", *(f"{unpublished}/{f}" for f in remote.listing(storage, f"{unpublished}/"))]
    )
    # a dry run removes nothing
    code, output = xcraft("prune", "--to", "main", "--keep-days", "0", "-n")
    assert code == 0, output
    assert f"would remove {unpublished}" in output
    assert remote.listing(storage, f"{unpublished}/")
    # a latest snapshot without its manifest, as while a cumulative one changes, keeps its builds by what it was
    # made from, its dated snapshot gone
    dated, latest = re.findall(r"published (repos/\S+), and as (repos/\S+)", published_output)[0]
    upload.remove_trees(project, storage, [dated])
    remote.delete(storage, [f"{latest}/{remote.MANIFEST}"])
    # without grace, what no publication has goes: the published builds stay
    code, output = xcraft("prune", "--to", "main", "--keep-days", "0")
    assert code == 0, output
    assert not remote.listing(storage, f"{unpublished}/")
    assert not remote.listing(storage, f"{again}/")
    remote.forget()
    assert [tree for tree in published if not remote.tree_exists(storage, tree, trusted=signer)] == []
    with urllib.request.urlopen(f"{web}/{prune.BUILDS}/{signer.key}/{ARCH}/") as response:  # noqa: S310, the test garage
        assert 'href="unpublished/"' not in response.read().decode()
    # nothing more to prune
    code, output = xcraft("prune", "--to", "main", "--keep-days", "0")
    assert code == 0, output
    assert "removed 0 builds, 0 files" in output


def _stats(directory: Path) -> dict[str, tuple[int, float, int]]:
    """Return the sizes, dates and modes of the files of `directory`, as rsync compares them, by path."""
    return {
        p.relative_to(directory).as_posix(): (p.stat().st_size, p.stat().st_mtime, p.stat().st_mode)
        for p in directory.rglob("*")
        if p.is_file()
    }


@needs_gpg
def should_mirror_the_latest_snapshot_of_a_stage(root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: PLR0915
    stage = f"mirror-{secrets.token_hex(4)}"
    spec = root / "base/trivial/trivial.spec"
    code, output = xcraft("publish", "repos", "--to", "main", "--stage", stage)
    assert code == 0, output
    storage, latest = Project.find().config.remote["main"], f"repos/9.0/{stage}/latest"
    copy = tmp_path / "copy"
    code, output = xcraft("mirror", str(copy), "--stage", stage)
    assert code == 0, output
    assert f"copied {latest}" in output
    link = copy / latest
    assert link.is_symlink()
    manifest = remote.read_manifest(storage, latest)
    assert {f: sha256_of(link / f) for f in manifest} == manifest
    # the dates of the storage, and fixed modes
    rpm = next(f for f in manifest if f.endswith(".rpm"))
    date = remote.modified(storage, f"{latest}/{rpm}")
    assert date
    assert (link / rpm).stat().st_mtime == date.timestamp()
    assert {oct(p.stat().st_mode & 0o777) for p in link.resolve().rglob("*")} == {"0o644", "0o755"}
    code, output = xcraft("mirror", str(copy), "--stage", stage)
    assert code == 0, output
    assert "is up to date" in output
    # a new publication: the files it did not change are hard links to those of the previous version, kept
    first = link.resolve()
    spec.write_text(spec.read_text().replace("Version: 1.0", "Version: 1.1"))
    code, output = xcraft("publish", "repos", "--to", "main", "--stage", stage)
    assert code == 0, output
    code, output = xcraft("mirror", str(copy), "--stage", stage)
    assert code == 0, output
    assert link.resolve() != first
    assert first.exists()
    key = next(f for f in remote.read_manifest(storage, latest) if f.startswith("RPM-GPG-KEY"))
    assert (link / key).stat().st_ino == (first / key).stat().st_ino
    # made again from scratch, the copy is the same for rsync
    again = tmp_path / "again"
    code, output = xcraft("mirror", str(again), "--stage", stage)
    assert code == 0, output
    assert _stats((again / latest).resolve()) == _stats(link.resolve())
    # checked against its manifests: the damaged files downloaded again, the others reported
    code, output = xcraft("mirror", str(copy), "--stage", stage, "--check")
    assert code == 0, output
    (link / key).chmod(0o644)
    (link / key).write_text("damaged")
    (link / "extra").write_text("extra")
    code, output = xcraft("mirror", str(copy), "--stage", stage, "--check")
    assert code == 1
    assert f"✗ {latest}/{key}: damaged" in output
    assert f"✗ {latest}/extra: not in the manifest" in output
    assert sha256_of(link / key) == remote.read_manifest(storage, latest)[key]
    (link / "extra").unlink()
    code, output = xcraft("mirror", str(copy), "--stage", stage, "--check")
    assert code == 0, output
    # a publication finished during the copy, its manifest dated otherwise after it than before: not switched to
    real = remote.modified
    dated = iter(range(1, 10))

    def later(storage: Remote, path: str) -> datetime | None:
        date = real(storage, path)
        return date + timedelta(seconds=next(dated)) if date and path.endswith(remote.MANIFEST) else date

    monkeypatch.setattr(remote, "modified", later)
    project = Project.find()
    project.config.publish.stage = stage
    with pytest.raises(BuildError, match="was published again while it was copied"):
        mirror.mirrored(project, storage, tmp_path / "raced", key_signer(project))
    assert not list((tmp_path / "raced" / latest).parent.iterdir())


def should_publish_the_inputs_of_netinstall_isos_without_the_others(root: Path, tmp_path: Path) -> None:
    project = Project.find()
    signer = release_signer(project)
    assert signer
    image = tmp_path / "test.iso"
    image.write_bytes(secrets.token_bytes(2048))
    snapshot = tmp_path / "snapshot"
    netinstall = snapshot / "iso" / iso_name(project, netinstall=True, signer=signer)
    netinstall.parent.mkdir(parents=True)
    shutil.copy2(image, netinstall)
    storage = project.config.remote["main"]
    project.config.publish.stage = f"netinstall-{secrets.token_hex(4)}"
    upload.Uploader(project, storage, signer, build=False).upload_isos(snapshot, "netinstall")
    inputs = upload.inputs_tree(f"isos/9.0/{project.config.publish.stage}/latest")
    assert remote.download_tree(storage, inputs, tmp_path / "inputs")
    assert upload.SnapshotInputs.model_validate_json((tmp_path / "inputs" / upload.INPUTS).read_bytes()).keys


def should_number_iso_snapshots_by_stage(root: Path, tmp_path: Path) -> None:
    project = Project.find()
    storage = project.config.remote["main"]
    signer = release_signer(project)
    assert signer
    isos = tmp_path / "iso" / ARCH
    isos.mkdir(parents=True)
    image = isos / "install.iso"
    stages = [f"{STAGE}-incoming", f"{STAGE}-testing"]
    project.config.publish.stage = stages[0]
    day = upload.Uploader(project, storage, None, build=False).snapshot_name()
    for stage in stages:
        project.config.publish.stage = stage
        assert upload.Uploader(project, storage, None, build=False).snapshot_name() == day
        image.write_text(stage)
        upload.Uploader(project, storage, signer, build=False).upload_isos(tmp_path, day)
        assert upload.Uploader(project, storage, None, build=False).snapshot_name() == f"{day}.2"
    for stage in stages:
        for name in (day, "latest"):
            prefix = f"isos/9.0/{stage}/{name}"
            assert remote.download_tree(storage, prefix, tmp_path / stage / name)
            assert (tmp_path / stage / name / ARCH / "install.iso").read_text() == stage


def should_not_reuse_snapshot_names_taken_by_another_signer(root: Path) -> None:
    project = Project.find()
    project.config.publish.stage = f"snapshot-name-{secrets.token_hex(4)}"
    storage = project.config.remote["dev"]
    signer = release_signer(project)
    assert signer
    assert not storage.build_keys
    day = upload.Uploader(project, storage, None, build=False).snapshot_name()
    prefix = f"isos/9.0/{project.config.publish.stage}/{day}"
    remote.write_manifest(storage, prefix, {}, signer=signer)
    assert not remote.tree_exists(storage, prefix)
    assert upload.Uploader(project, storage, None, build=False).snapshot_name() == f"{day}.2"


@needs_gpg
def should_publish_pull_requests_to_a_stage_of_their_own(
    root: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # master's builds in the main bucket, signed by the key of [sign], CI's
    assert xcraft("build")[0] == 0
    commit_base(root)
    code, output = xcraft("push", "builds", "--to", "main")
    assert code == 0, output
    ci = key_signer(Project.find())
    assert ci
    # a pull request changing with-deps, built again by each of its runs, numbered by them
    git(root, "checkout", "-qb", "feature")
    edit(root, "changed", name="with-deps")
    monkeypatch.setenv("XCRAFT_DEV_COUNTER", "7")
    stage = f"pr7-{STAGE}"
    with caplog.at_level(logging.DEBUG, logger="xcraft"):
        code, output = xcraft(
            "publish", "repos", "--to", "dev", "--stage", stage, "--dev", "pr7", "--summary", str(tmp_path / "summary")
        )
    assert code == 0, output
    developer = dev_signer(Project.find())  # the key of the pull request, made by the publication
    assert developer
    dev = Project.find().config.remote["dev"]
    latest = f"repos/9.0/{stage}/latest"
    manifest = remote.read_manifest(dev, latest, trusted=developer)
    ours = sorted(f for f in manifest if f.startswith(f"base/{ARCH}/") and f.endswith(".noarch.rpm"))
    assert ours == [
        f"base/{ARCH}/trivial-1.0-1+b1.xcpng8.99.noarch.rpm",
        f"base/{ARCH}/with-deps-1.0-1+b2~7.pr7.xcpng8.99.noarch.rpm",
    ]
    # master's build copied from the main bucket, server-side, keeping CI's signature, whose key the stage has
    assert any(
        m.startswith(f"copying s3://xcraft/.xcraft/builds/{ci.key}/") and f"s3://xcraft-dev/repos/9.0/{stage}/" in m
        for m in caplog.messages
    )
    snapshot = root / ".xcraft/publish"
    check_signatures(Project.find(), snapshot, [ours[0]], what="master's", public_key=ci.public_key())
    check_signatures(Project.find(), snapshot, [ours[1]], what="the pull request's", public_key=developer.public_key())
    assert {"RPM-GPG-KEY", f"RPM-GPG-KEY-{ci.key}"} <= set(manifest)
    # the repository of the snapshot for the hosts, trusting all the signers of its packages
    day = re.search(rf"published repos/9.0/{stage}/(\S+), and as", output)
    assert day, output
    tree = f"{dev.url.rstrip('/')}/repos/9.0/{stage}/{day[1]}"
    repo = (snapshot / f"xcraft-{stage}-{ARCH}.repo").read_text()
    assert f"baseurl={tree}/base/{ARCH}/\n" in repo
    assert f"gpgkey={tree}/RPM-GPG-KEY {tree}/RPM-GPG-KEY-{ci.key}" in repo
    assert f"xcraft-{stage}-{ARCH}.repo" in manifest
    # where it is, how to use it, and the files of the development builds, by source package
    summary = (tmp_path / "summary").read_text()
    assert "latest" not in summary
    assert f"dnf config-manager --add-repo {tree}/xcraft-{stage}-{ARCH}.repo\n" in summary
    name = "with-deps-1.0-1+b2~7.pr7.xcpng8.99"
    assert (
        f"- with-deps\n  - [{name}.noarch.rpm]({tree}/base/{ARCH}/{quote(name)}.noarch.rpm)\n"
        f"  - [{name}.src.rpm]({tree}/base/Source/{quote(name)}.src.rpm)\n"
    ) in summary
    assert "- trivial" not in summary  # master's
    # its contents, which the checkout is compared with: its development build, final in the checkout
    assert "contents.json" in manifest
    code, output = xcraft("contents", "--diff-stage", f"dev@{stage}")
    assert code == 0, output
    assert "  ~ with-deps 1.0-1+b2~7.pr7.xcpng8.99 → 1.0-1+b2.xcpng8.99" in output.splitlines()
    assert xcraft("contents", "--diff-stage", f"dev@{stage}/no-such-snapshot")[0] == 1
    # the development builds are files of the stage only
    assert not remote.listing(dev, f".xcraft/builds/{developer.key}/")
    # removed with the pull request
    code, output = xcraft("unpublish", "--stage", stage)
    assert code == 0, output
    assert not remote.listing(dev, f"repos/9.0/{stage}/")
    assert not remote.listing(dev, f".xcraft/inputs/repos/9.0/{stage}/")
    assert xcraft("unpublish", "--stage", stage)[1].endswith(f"no stage {stage} to remove\n")


@needs_gpg
def should_sign_upstream_packages_once_in_final_mode(root: Path, tmp_path: Path) -> None:
    stage = f"signed-{secrets.token_hex(4)}"
    # an upstream package for the hosts, and an extra one, unsigned
    extra = TOYS / ARCH / f"toyarch-1-1.{os.uname().machine}.rpm"  # installable on the platform
    (root / "xcraft.toml").write_text(
        f'{REMOTE}extra = ["bc"]\n'
        f'[extra_upstream.toyarch-new]\nurl = "{extra.as_uri()}"\nsha256 = "{sha256_of(extra)}"\n'
    )
    code, output = xcraft("publish", "repos", "--to", "main", "--stage", stage)
    assert code == 0, output
    signer = key_signer(Project.find())  # of [sign]
    assert signer
    storage = Project.find().config.remote["main"]
    # all the packages of the hosts signed by its key, the upstream ones too
    latest = f"repos/9.0/{stage}/latest"
    binaries = [f for f in remote.read_manifest(storage, latest, trusted=signer) if f.startswith(f"base/{ARCH}/")]
    upstream = [f for f in binaries if f.endswith(".rpm") and not Path(f).name.startswith(("trivial-", "with-deps-"))]
    assert f"base/{ARCH}/{extra.name}" in upstream
    for f in upstream:
        assert remote.download(storage, f"{latest}/{f}", tmp_path / f)
    check_signatures(Project.find(), tmp_path, upstream, what="upstream", public_key=signer.public_key())
    copies = remote.listing(storage, f".xcraft/upstream-signed/{signer.key}/{ARCH}/")
    assert any(f.endswith(".rpm") for f in copies)
    # signed once: another snapshot copies them, even without the cache
    shutil.rmtree(root / ".xcraft/cache/upstream-signed", ignore_errors=True)  # signed here, or by another test
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(spec.read_text().replace('echo "hello"', 'echo "hello, signed upstream"'))
    code, output = xcraft("publish", "repos", "--to", "main", "--stage", stage)
    assert code == 0, output
    assert not (root / ".xcraft/cache/upstream-signed").exists()  # none signed again
    assert remote.listing(storage, f".xcraft/upstream-signed/{signer.key}/{ARCH}/") == copies
    # their metadata signed by the key, as the bucket is not: another signature is refused
    signature = f".xcraft/upstream-signed/{signer.key}/{ARCH}/repodata/repomd.xml.asc"
    assert remote.download(storage, signature, tmp_path / "repomd.xml.asc")
    (tmp_path / "other").write_text("other")
    remote.upload(storage, signature, signer.sign_file(tmp_path / "other"))
    try:
        with pytest.raises(upstream_copy.UpstreamError, match=f"is not signed by {signer.key}"):
            upstream_signed.read(Project.find(), storage, signer, tmp_path / "read")
    finally:
        remote.upload(storage, signature, tmp_path / "repomd.xml.asc")


def should_accept_only_upstream_entries_of_their_pinned_versions() -> None:
    # forged metadata of the unsigned upstream copy, for a package of the pinned SHA-256
    def entry(version: str, location: str) -> repodata.Entry:
        return (f'<package><name>bc</name><arch>x86_64</arch>{version}<location href="{location}"/></package>', "", "")

    pins = {
        "bc": Pin(evr="1.07.1-11.el10", arch="x86_64", sha256="0" * 64, source="bc"),
        "dbus": Pin(evr="1:1.14.10-5.el10", arch="x86_64", sha256="1" * 64, source="dbus"),
    }
    pinned_version = '<version epoch="0" ver="1.07.1" rel="11.el10"/>'
    assert publisher.as_pinned(pins, "bc", entry(pinned_version, "bc-1.07.1-11.el10.x86_64.rpm"))
    assert publisher.as_pinned(pins, "bc", entry(pinned_version, "0123/bc-1.07.1-11.el10.x86_64.rpm"))  # a signed copy
    assert not publisher.as_pinned(pins, "bc", entry(pinned_version, "bc-1.06-1.el10.x86_64.rpm"))
    assert not publisher.as_pinned(
        pins, "bc", entry('<version epoch="0" ver="1.06" rel="1.el10"/>', "bc-1.07.1-11.el10.x86_64.rpm")
    )
    assert not publisher.as_pinned(
        pins, "bc", entry('<version epoch="1" ver="1.07.1" rel="11.el10"/>', "bc-1.07.1-11.el10.x86_64.rpm")
    )
    dbus = entry('<version epoch="1" ver="1.14.10" rel="5.el10"/>', "dbus-1.14.10-5.el10.x86_64.rpm")
    assert publisher.as_pinned(pins, "dbus", (dbus[0].replace("<name>bc", "<name>dbus"), "", ""))
