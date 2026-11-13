# Iterating on a package

How to change a package, build it and try it on a test host, as fast as xcraft makes it. What each command does is in
[Builds](builds.md), why and how much it saves in [faster development iterations](dev-iteration.md).

## A change of the spec or of the patches

Edit the package in xcp-ng-dist, in `base/<pkg>/`, then build it without its tests and install it on a test host:

```
xcraft build --nocheck xen
xcraft repo xen --host root@testhost --upgrade
```

Each build starts again from the sources, compiling from the compiler cache of the package, which xcraft fetches from
the remote storage when there is none on the machine.

## A change of the code: `xcraft develop`

To work on the code of a project, build its package from its git tree: each build compiles only what changed in the
tree, like `make` in it. Start from the branch the package is made of, then:

```
git clone -b kernel/xcpng-6.12.0-206.100.3.1/patched https://github.com/xcp-ng/linux ../linux
xcraft develop ../linux --package kernel   # or xcraft develop ../blktap, named after its repository
xcraft build --nocheck kernel              # the first build: everything, from the compiler cache
# edit ../linux, then, as many times as needed:
xcraft build --nocheck kernel
xcraft repo kernel --host root@testhost --upgrade
xcraft develop --stop                      # done: back to the package's sources
```

The builds take the files of the tree as they are, committed or not, untracked ones too, but those git ignores: no need
to commit to build. `xcraft develop` alone shows the packages developed from trees, and every command reminds them.

What to expect, measured on one developer machine, with a debug log changed at each iteration:

| package | first build of the tree | each iteration |
|---|---|---|
| kernel | 7.5 min | 4.5 min |
| blktap | 45 s | 19 s |

**A first build much longer than a build from the spec**, like 26 min rather than 7.5 for the kernel, means the
compiler cache missed: report it.

**Use `--nocheck`, always or never.** The build tree is kept from one build to the next, with what the tests left in it.
blktap's tests compile it again with gcov, and the next build without them then fails to link. `xcraft clean <pkg>`
removes the kept tree, if a build fails for what a previous one left, or after a change of the spec's build options: the
next build starts again, from the compiler cache.

**A tree ahead of the spec may need spec changes.** The spec stays the package's: a branch with new dependencies or
files needs its BuildRequires, `%install` or `%files` changed in the checkout of xcp-ng-dist, like blktap's `dev`
branch, which needs glib2, zlib and zstd. They are part of the changes to propose in xcp-ng-dist.

**The builds are development builds.** They upgrade the test hosts like the others, but never change `xcraft.builds`,
and are never pushed: the final builds, `publish` and `push` refuse them, until `xcraft develop --stop`. To ship the
change, push it to the project's repository, then update the package from it, see [Import and patchsets](import.md).

**Disk space.** The kernel's kept build tree takes about 16 GB, in `.xcraft/build/kernel/`. `xcraft develop --stop`
keeps it until the next build of the package, or `xcraft clean kernel`.

**The kernel links vmlinux again at each build**, even after a change to a module only: about 80 s of each iteration,
until its spec is fixed, see [the kernel's vmlinux](dev-iteration.md#the-kernels-vmlinux-linked-again-at-each-build).
