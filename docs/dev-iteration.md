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
4 s, and runs its tests in 2 s. The kernel was not measured: its compiler cache and its build tree do not fit in the
16 GB of `/tmp` the measurements used.

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

## Not done

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

- **Incremental builds**: rebuilding from the build tree of the previous build, with `rpmbuild --short-circuit`,
  rather than from scratch. Compiling dominates xen, qemu and openvswitch, even with the compiler cache: 115 s,
  136 s and 541 s. It is the only idea cutting that, but the builds would not be reproducible anymore, for local
  iterations only; `xcraft shell` does it by hand.
- **Caching the build roots**, as container images of their packages: their installation takes 5 to 11 s by build.
- **Without debuginfo** for the development builds: extracting it takes 4 to 12 s by build, but the packages change.
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
