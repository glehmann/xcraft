# Import and patchsets

## Import

`xcraft import <pkg>` imports the history of a package from its packaging repository, `https://github.com/xcp-ng-rpms/<pkg>`
or `--url`, into the git repository of the project: each branch of `branches` of `[import]` in `xcraft.toml`,
`["master"]` by default, like `["master", "XS-9.0"]`, or `--branch`, becomes the branch `import/<pkg>/<branch>`. The
project merges it to bring the package, in `base/` for a new one, or another repository with `--repo`. Only git is
used.

The commits are rewritten for the project: the files of `SPECS/` and `SOURCES/` in the package directory, the archives
stored with git LFS in its `sources` manifest, by the SHA-256 of their LFS pointer, so without downloading them, and
nothing else, like the README and `.github/`. A commit without `SPECS/` nor `SOURCES/`, of a flat layout like
Fedora's dist-git, has the files at its top instead, but its README, those starting with a dot, and those of the
dist-git: its `sources`, `Makefile` and `import.log`. The archives of its `sources` are in Fedora's lookaside cache, by
their SHA-512: they are not in the manifest, which only has those stored with git LFS. Links and submodules are refused, like a link to a file of the host. The
commits left without a change are skipped, and so are the merges of what another parent already merges. Each commit
gets the trailer `Imported-from: <owner>/<repository>@<commit>`, and keeps its author, committer, dates and message.

The project's history must start with an empty commit: the first imported commits get it as parent. The rewrite only
depends on the commits imported and on it: importing again gives the same commits, with the new ones on top, and the
branches sharing commits, like master merging XS-9.0, share them. A merge brings the package into the project, with
its history, and the next ones bring the new commits, which `xcraft import --all` imports for all the packages with
import branches of histories, on the branches they have import branches of, like `ydi/9` for a package imported with
`--branch ydi/9`. The import branches can be pushed, for others to see the histories: they only move forward.

```
git merge import/xen/master
git merge import/libempserver/master
```

The package must not be in the project before its first merge: `git log` and `git blame` follow a merge only through
the parents it is the same as, which would be the project's.

`xcraft import <srpm>`, a path or a URL ending with `.src.rpm`, imports a source RPM, like a new one of XenServer, as
a new commit of the import branch of its package, `srpm_branch` of `[import]`, from the first commit of the project for
a new package. Its files are laid out the same way, its archives go into the source cache, and `removed` lists those to
remove for legal reasons, each replaced by a `<file>.deleted-by-XCP-ng.txt` saying so:

```toml
[import]
branches = ["master", "XS-9.0"]
srpm_branch = "XS-9.0"
removed = ["Citrix_Logo_Black.png", "COPYING.CitrixCommercial"]
```

A merge brings it into the project, like the history. The import of a history refuses to move an import branch which
has commits it does not import, like those of a source RPM: the packaging repository is then no longer followed.

## Patchsets

Developers maintain the changes of XCP-ng to a project in a branch of its git repository, or of a fork. `xcraft
patchset`, run in that repository on that branch, turns the commits of the branch into the patches of the package:

```bash
cd ~/src/xen-api   # on the branch of the XCP-ng changes
xcraft patchset --package xapi --base v26.4.0
```

- the package is the one named after the repository (its `origin` remote, or its directory): a package directory, a
  spec `Name`, or a `Source0` named `<repository>-<version>...`; `--package` gives it otherwise;
- the base of the branch is the tag of the version of the package, `v<version>` or `<version>`; `--base` gives it
  otherwise;
- the commits from the base are exported with `git format-patch --zero-commit`, so that the patches only change when
  the commits do, not when they are rebased;
- the spec lists them in a block managed by xcraft, numbered from 1000 (`--first`), and recording where they come
  from; an update replaces the block and removes the patches not used anymore. Other patches are kept. The first
  time, the patches of the spec with the same subject as exported commits are replaced, as previous exports of the
  patchset.

The patches are applied by `%autosetup` or `%autopatch`, in the order of their numbers. Then `xcraft build` checks
them, and `xcraft bump` makes a release of them.
