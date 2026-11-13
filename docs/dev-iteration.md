# Faster development iterations

What a change to a package costs before its new build can be tried, where that time goes, what xcraft does about it,
and what it does not do yet, with the measurements behind each choice. They were made on xcp-ng-dist, for x86_64_v2,
on one developer machine, in November 2026: one run each, so they give orders of magnitude, not precise figures.

## Where the time goes

The phases of the builds of a few packages, rebuilt with the compiler cache of a checkout, as in an iteration, in
seconds. *Build root* is from the start of xcraft to rpmbuild's `%prep`, mostly dnf installing the build root;
*packaging* is from rpmbuild's "Processing files" to `%clean`: the dependency generators, the compression and the
writing of the packages; *after* is what xcraft does once rpmbuild is done.

| package | total | build root | compile | `%install` | debuginfo | `%check` | packaging | after |
|---|---|---|---|---|---|---|---|---|
| nbd | 212 | 5 | 5 | 0 | 0 | 199 | 0 | 1 |
| xen | 172 | 7 | 115 | 21 | 4 | none | 23 | 1 |
| qemu | 168 | 6 | 136 | 1 | 6 | none | 15 | 1 |
| xapi | 166 | 11 | 48 | 4 | 9 | 35 | 53 | 3 |
| ocaml | 478 | 6 | 70 | 1 | 4 | 296 | 97 | 2 |
| openvswitch | 812 | 10 | 541 | 5 | 12 | 213 | 28 | 2 |

xapi and ocaml were compiled without the cache of dune, which an iteration has: with it, a build of xapi compiles in
4 s, and runs its tests in 2 s. The kernel was not measured there: its compiler cache and its build tree do not fit
in the 16 GB of `/tmp` the measurements used. It was later, with blktap, built from their trees: see
[Incremental builds](#incremental-builds-xcraft-develop).

Besides the builds, xcraft resolved the build roots and recorded what rpm generated for the builds before and after
them, which, for a small package, took several times as long as its build: see below.

## What xcraft does

### Builds without their tests: `--nocheck`

`xcraft build`, `repo`, `iso` and `boot` take `--nocheck`, rpmbuild skipping `%check`. It saves what the `%check`
column shows: 3 minutes for nbd, 5 for ocaml, 3.5 for openvswitch, 35 s for xapi. Those builds are marked in the cache,
built again with their tests by the builds without `--nocheck`, never pushed, and their ISOs are cached apart; it
cannot be used with `--final`. See [Builds](builds.md#builds-without-their-tests).

`_without_check` was not defined too: it would skip the BuildRequires of the tests in the build roots, which would then
not be the ones resolved, and none of the specs of xcp-ng-dist runs tests out of `%check`.

### Development builds compressed faster

The binary packages of development builds are compressed with zstd at level 3, rather than at the level 19 of
AlmaLinux 10, on one thread, which the final builds keep, as they may be pushed:

| package | packaging at level 19 | at level 3 | size at 19 | size at 3 |
|---|---|---|---|---|
| xen | 22.5 s | 4.2 s | 41 MB | 52 MB |
| xapi | 53.1 s | 18.4 s | 133 MB | 187 MB |
| ocaml | 96.7 s | 31.1 s | 137 MB | 181 MB |

Level 1 is no faster, and no compression at all neither: xapi's packaging took 18.7 s at level 1, for 202 MB, and
19.0 s uncompressed, for 638 MB. What is left is not the compression, see the OCaml dependency generator below. The
setting is not in the recipe of the builds: the packages have the same contents, and their keys do not change.

### Less resolving around the builds

A change to a package made xcraft resolve the build roots of all the architectures again, and record what rpm
generated for the builds of all the others, before and after the build. Measured with `xcraft bump xo-lite` then
`xcraft build xo-lite`, a build of 3 s, the second time in a checkout:

| step | at first | with 1 and 2 | with 1 to 4 |
|---|---|---|---|
| parsing the changed spec, of both architectures | 1.5 s | 1.6 s | 1.5 s |
| resolving the build roots | 14.3 s | 8.3 s | cached |
| recording what rpm generated, before the build | 3.9 s | none | none |
| the build, with its own record | 4.5 s | 4.1 s | 3.3 s |
| recording what rpm generated, after the build | 3.8 s | 3.7 s | none |
| total | 30.6 s | 17.3 s | 5.3 s |

1. The build roots of the architectures are resolved at the same time, in their own containers.
2. Before the builds, what the cached builds kept of what rpm generated is restored as it is while the base context of
   its resolution is the same, without resolving it again; the builds record it again after them.
3. What a build kept holds while the base context, the names of the packages of ours there and the builds of those its
   requirements resolved to are the same: a new build of another package resolves again only the records which
   resolved to it. See [Build keys](build-keys.md).
4. The resolutions of the build roots are cached by what they are resolved from without the epoch-version-releases of
   our packages: a new release of a package resolves nothing again. The resolution reports the binary packages of ours
   any package of the repositories, upstream ones included, a build of ours, or what is published refers to with a
   version, or in a rich dependency, with their epoch-version-releases: a cached resolution is used only while they are the same, and a
   resolution made again replaces the one of the remote storage. See [the locks](lock-design.md).

The record of the build itself, about 1 s, is still made right after it, for the packages built after it in the same
run.

### A new checkout

`xcraft build` in a new checkout of xcp-ng-dist fetches the builds of all the packages: it recorded what rpm generated
for each one once fetched, with only some of them there, which resolved them all again. The builds now come with what
they recorded, `rpm-deps.json`, and the builds fetched are recorded together, once all are there, or before a build
that may need them: a new checkout fetched all of them in 82 s rather than 118 s, without resolving anything, but
parsing the specs.

`xcraft boot` and `xcraft iso` also take the ISO CI published, signed with a key of the builds of the storage, rather
than making one signed with the developer's key, when they are given neither `--key` nor `--unsigned`.

### Incremental builds: `xcraft develop`

Compiling dominates the builds above, even with the compiler cache: 115 s for xen, 136 s for qemu, 541 s for
openvswitch, while a change of a developer recompiles a few files. `xcraft develop <tree>` builds a package from the git
tree of its project, as it is, and keeps the directory of its sources from one build to the next, for `make`, or dune,
to make only what changed, like a build in the tree. The development builds of the checkout only: the builds are not
reproducible anymore, and the final builds, `publish` and `push` refuse the packages developed from trees. See
[Builds](builds.md#developing-from-a-projects-tree) for what it does, and [Iterating on a package](iterating.md) for
how to use it.

**Keeping the directory of the sources.** rpmbuild removes it after a successful build, and xcraft makes a new build
tree for each build, `.xcraft/build/<pkg>/`. For a package developed from a tree:

- rpmbuild runs with `--noclean`, which skips that removal;
- xcraft removes and stages the build tree as usual, but keeps its `BUILD/`;
- the spec's main archive is the tree, `xcraft-develop.tar`, as `git archive` makes it of a commit of its files,
  untracked ones too, but those git ignores, the same commit for the same files; its patches are dropped, as the tree
  has them, as commits;
- its first `%setup` or `%autosetup` of `%prep` becomes `%setup -q -T -D -n <dir>`, which neither unpacks nor removes
  the directory, then `develop.py` of the build environment synchronizes the directory with the archive: it writes the
  files whose contents differ, removes those the tree no longer has, and leaves the others as they are, with their
  times. The rest of `%prep` runs as usual.

**In the directory of the builds of the spec.** The directory is the one of the `%setup` it replaces, with its `-n`,
like `linux-%{usver}` for the kernel. ccache hashes the working directory of the compilations made with `-g`, which all
the builds of xcp-ng-dist are: in another directory, the first build of a tree would find nothing in the compiler cache
of the package. It used `%{name}-%{version}` at first, `kernel-6.12.0` rather than `linux-6.12.0`: the first build of
the kernel from its tree took 26.5 min, rather than 7.5 min in `linux-6.12.0`, with 10,203 hits and 9 misses.

**Measured** on the kernel, from `kernel/xcpng-6.12.0-206.100.3.1/patched` of xcp-ng/linux, whose files are those of
the spec's archive and patches, and on blktap, from `dev` of xcp-ng/blktap, ahead of the spec, which needed more
BuildRequires, glib2, zlib and zstd, and other files installed; all with `--nocheck`, a debug log changed at each
iteration, in seconds:

| package | first build of the tree | iteration | of which compiling |
|---|---|---|---|
| kernel | 452, 1590 when the compiler cache missed | 257 to 276 | 6 |
| blktap | 45 | 19 | 1.3 |

The first build compiles everything, from the compiler cache, like a build from the spec: an iteration takes about 60 %
of it for the kernel, 40 % for blktap.

The kernel's iteration, after a change to a module, ixgbe, 257 s, with the build of `uname -v` fixed, see
[the kernel's vmlinux](#the-kernels-vmlinux-linked-again-at-each-build); a change to xen-blkback, built in vmlinux, took
272 s without it:

| step | time |
|---|---|
| xcraft before the build: `git archive` of the tree, 2.5 s, staging, resolution | 8 |
| installing the build root | 13 |
| `%prep`: synchronizing the directory, a 1.6 GB archive | 7.5 |
| compiling the module, and modpost | 6 |
| linking vmlinux again, and making bzImage | 31 |
| the BTF of all the modules, made again against the new vmlinux | 51 |
| perf | 6 |
| `%install` | 29 |
| find-debuginfo, then strip | 21 + 28 |
| packaging: the binary packages, and the source package, 235 MB of the tree | 52 |

blktap's iteration, 23.5 s before the waiting packages below:

| step | time |
|---|---|
| xcraft before the build | 3.8 |
| starting the container | 0.6 |
| installing the build root, ~230 packages | 4.2 |
| `autogen.sh` and `configure`, which `%build` runs at each build | 5.1 |
| `make`: 2 files compiled | 1.3 |
| `%install`, debuginfo and packaging | 2 |
| xcraft after the build, 3.2 s of which resolving again packages waiting for builds of ours | 5.1 |

### Packages waiting for builds of ours

After each build, xcraft resolved again what rpm generated for the packages it could not resolve before, as long as
those needed packages of ours that were not built yet: 3.2 s of blktap's iteration. It now records, with what the build
kept, which packages of ours they wait for, and resolves them again only once one of those is built: blktap's
iteration takes 19 s. See [Build keys](build-keys.md).

## Not done

### The kernel's vmlinux, linked again at each build

After a change to a module, the kernel's build still links vmlinux again, and makes the BTF of all the modules again
against it: 82 s of its 257. kbuild makes them again as:

- `include/generated/compile.h` and `utsversion.h` change at each build: the counter of `.version`, the time of the
  build, as kbuild ignores `SOURCE_DATE_EPOCH`, and the host name, the container's, random. In the spec, before the
  build, `export KBUILD_BUILD_HOST=xcp-ng KBUILD_BUILD_VERSION=1 KBUILD_BUILD_TIMESTAMP="$(date -u -d
  @${SOURCE_DATE_EPOCH})"` fixes them, which also makes the final builds closer to
  [identical rebuilds](identical-rebuilds-design.md): 257 s rather than 276;
- `certs/x509_certificate_list` is made again at each build, for a reason not found yet, which links vmlinux again
  anyway.

A change of the kernel's spec, for the kernel team.

### Without debuginfo

find-debuginfo and strip take 49 s of the kernel's iteration, and a few seconds of the others, but the development
builds are debugged on the test hosts: they keep their debug information. Their packages are already compressed with
zstd at level 3, see above.

### Caching the build roots

As container images of their packages: installing them takes 4.2 s for blktap, 13 s for the kernel, 5 to 11 s for
the others. Not worth the images to make, store and remove, for each build root: the build environment images already
pile up, which `xcraft clean --shared` removes.

### `configure` only when it changed

blktap's `%build` runs `autogen.sh` and `configure` at each build, 5.1 s of its 19, to compile 2 files. Running them
only when `configure.ac` or a `Makefile.am` changed is up to the spec, or would need a hook of xcraft in `%build`.

### Synchronizing only what git changed

The kernel's tree is archived whole, 2.5 s, then compared whole with the directory of the sources, 7.5 s: 4 % of an
iteration. Giving `develop.py` the files changed since the commit of the previous build, as `git diff` lists them,
would save most of it.

### Directories of the sources without their versions

The directory of the sources has the version of the package, and ccache hashes it, see above: the first build after a
version update finds nothing in the compiler cache. `CCACHE_BASEDIR` alone does not help: it makes the absolute paths
of the arguments relative, but the working directory stays in the hash with `-g`, unless the compilations are given
`-fdebug-prefix-map` too, which keeps debugedit from finding the sources of the debug information under the build
directory.

The specs could unpack into a directory without version instead: `%setup -q -T -c -n src`, then
`tar --strip-components=1 -xf %{SOURCE0}` and `%autopatch -p1`; `%autosetup -S git` needs its repository made after.
But the kernel already has `linux-%{usver}`, which stays `linux-6.12.0` across the updates of UEK, and the others with
large caches, xen, qemu, edk2, openvswitch, grub, change their versions once to three times a year, while their fixes
come as patches, which keep the directory. Not worth the differences with the specs of XenServer and AlmaLinux to keep
across their imports, for a cold build per machine and in CI a few times a year.

### The OCaml dependency generator, with rpm 4.20

For packages with many OCaml object files, most of what is left of the packaging is rpm's OCaml dependency generator,
`ocamldeps.sh` of rpm-build: rpm 4.19, AlmaLinux 10's, runs it once per file, twice, for the provides then the
requires, and each run starts bash, `ocamlobjinfo` and awk. About 25 ms per file:

| package | OCaml object files | generator time |
|---|---|---|
| xs-opam-repo | 3271 | about 82 s |
| ocaml | 816 | about 20 s |
| xapi | 545 | 13.6 s measured, of the 19 s of its packaging at zstd level 3 |

rpm 4.20 adds the multifile protocol, `%__<name>_protocol multifile`: a generator gets all the files on its standard
input, and writes the dependencies of each after a line `;<file>`. The generator in the appendix reads all the files
with one `ocamlobjinfo`, and keeps the logic of `ocamldeps.sh` for each file: on the 545 files of xapi, its provides
take 0.22 s rather than 3.05 s, its requires 0.22 s rather than 3.18 s, and both are identical to those of
`ocamldeps.sh`, for all the files: 1224 provides and 21942 requires. rpm 4.19 ignores the protocol, and calls it once
per file, which it fails then.

When AlmaLinux has rpm 4.20 or later, it would replace `ocamldeps.sh` by setting `%__ocaml_provides`,
`%__ocaml_requires` and `%__ocaml_protocol multifile`, for the final builds too, as it generates the same
dependencies: checking it on the packages of xapi, xs-opam-repo and ocaml, and mapping the recipe in `_SAME_BUILDS` to
keep the keys. It saves most of the generator time above, at each build of those packages, in CI too.

Under rpm 4.19, a generator computing the dependencies of all the files at its first call, then giving those of each
file from a cache, at the next ones, saves only about a third: 1.0 s rather than 3.0 s, by kind of dependency, for the
files of xapi, about 4 s of its 13.6, as rpm starts a process for each file anyway. Not worth a generator of xcraft
to maintain.

### Other ideas

- **Parallel builds** of the packages independent of each other, for full rebuilds, or a change of a package many are
  built against, like xen: not for iterating on one package. The builds share the local repository of their
  dependencies, `.xcraft/repo`, which each build would need its own of; the lock of the project is re-entrant in a
  process, which does not keep the threads of the builds from writing `xcraft.lock` and `xcraft.builds` at the same
  time; the build roots are installed one at a time anyway, under the lock of dnf; each compiler would use all the
  processors; and the progress shows one activity at a time.

## Appendix: the OCaml dependency generator for the multifile protocol

```python
#!/usr/bin/python3
"""The provides or requires of OCaml object files, like rpm's ocamldeps.sh, for its multifile protocol: the files on
stdin, read with one ocamlobjinfo for all of them, rather than one bash, ocamlobjinfo and awk each; the dependencies of
each file printed after a line ;<file>.

Options, like ocamldeps.sh: --provides/-P or --requires/-R, -f "ocamlobjinfo command", -i NAME, an interface to omit,
-x NAME, an implementation to omit, and -c, ignored.
"""

import shlex
import subprocess
import sys

SUFFIXES = (".cma", ".cmi", ".cmo", ".cmx", ".cmxa")  # not .cmxs, whose references are those compiled into them


def options(args: list[str]) -> tuple[str, list[str], set[str], set[str]]:
    mode, command, interfaces, implementations = "", ["ocamlobjinfo"], set(), set()
    while args:
        arg = args.pop(0)
        if arg in ("-P", "--provides"):
            mode = "provides"
        elif arg in ("-R", "--requires"):
            mode = "requires"
        elif arg == "-f":
            command = shlex.split(args.pop(0))
        elif arg == "-i":
            interfaces.add(args.pop(0))
        elif arg == "-x":
            implementations.add(args.pop(0))
        elif arg != "-c":
            sys.exit(f"usage: {sys.argv[0]} --provides|--requires [-f 'ocamlobjinfo cmd'] [-i NAME] [-x NAME]")
    if not mode:
        sys.exit("--provides or --requires needed")
    return mode, command, interfaces, implementations


def dependencies(output: str, mode: str, interfaces: set[str], implementations: set[str]) -> dict[str, list[str]]:
    """Return the dependencies of `mode` by file, from the `output` of ocamlobjinfo for all the files, as
    ocamldeps.sh reads it for each one.
    """
    found: dict[str, list[str]] = {}
    file, unit, state = "", "", "find"
    provided: dict[str, dict[str, str]] = {}
    required: dict[str, dict[str, str]] = {}

    def done() -> None:
        if file:
            kinds = provided if mode == "provides" else required
            found[file] = [
                f"{prefix}({name}) = {value}" for prefix in ("ocaml", "ocamlx") for name, value in kinds[prefix].items()
            ]

    for line in output.splitlines():
        if line.startswith("File "):
            done()
            file, unit, state = line[5:], "", "file"
            provided, required = {"ocaml": {}, "ocamlx": {}}, {"ocaml": {}, "ocamlx": {}}
        elif line.startswith("Unit name:"):
            unit, state = line.split()[2], "cma"
        elif line.startswith("Name:"):
            unit, state = line.split()[1], "cmx"
        elif line.startswith("CRC of implementation:"):
            if state == "cmx" and unit not in implementations:
                provided["ocamlx"][unit] = line.split()[3]
            state = "crc"
        elif line.startswith("Interfaces imported:"):
            state = "interface"
        elif line.startswith("Implementations imported:"):
            state = "implementation"
        elif line.startswith("\t"):
            fields = line.split()
            if state in ("interface", "implementation") and len(fields) > 1 and not fields[0].startswith("-"):
                prefix, omitted = ("ocaml", interfaces) if state == "interface" else ("ocamlx", implementations)
                if fields[1] not in omitted:
                    (provided if fields[1] == unit else required)[prefix][fields[1]] = fields[0]
        else:
            state = "find"
    done()
    return found


def main() -> None:
    mode, command, interfaces, implementations = options(sys.argv[1:])
    files = [f for f in sys.stdin.read().splitlines() if f.endswith(SUFFIXES)]
    if not files:
        return
    output = subprocess.run([*command, *files], capture_output=True, text=True, check=True).stdout
    for file, deps in dependencies(output, mode, interfaces, implementations).items():
        print(f";{file}", *deps, sep="\n")


main()
```

The macros, in place of those of `/usr/lib/rpm/fileattrs/ocaml.attr`, with the generator in the build environment:

```
%__ocaml_provides /xcraft/ocamldeps.py --provides
%__ocaml_requires /xcraft/ocamldeps.py --requires
%__ocaml_protocol multifile
```
