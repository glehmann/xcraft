"""Write as JSON the upstream packages needed to install our packages and the extra ones, on an empty system.

Run as root in the build environment, with our builds as the xcraft repository:
``runtime.py <input> <output>``. The input has the locked packages, as
``{name: [evr, arch]}``, which keep their versions, the extra packages, and
those of the installer. Each package is resolved alone, without weak
dependencies, as some of ours conflict with others, and the installer's
together.
"""

import json
import sys
import tempfile
from pathlib import Path

import dnf  # ty: ignore[unresolved-import]


def main() -> None:
    """Resolve what the input needs at runtime."""
    data = json.loads(Path(sys.argv[1]).read_text())
    locked = data["locked"]
    base = dnf.Base()
    base.conf.read()
    base.conf.installroot = tempfile.mkdtemp()
    base.conf.install_weak_deps = False
    base.read_all_repos()
    base.fill_sack(load_system_repo=False)
    available = base.sack.query().available()
    # only upstream ones: ours replace them, like kernel-headers
    base.sack.add_excludes(
        [
            p
            for p in available.filter(name=list(locked))
            if not p.reponame.startswith("xcraft-") and [p.evr, p.arch] != locked[p.name]
        ]
    )
    roots = sorted(
        {
            p.name
            for p in available
            if p.reponame.startswith("xcraft-") and not p.name.endswith(("-debuginfo", "-debugsource"))
        }
        | set(data["extra"])
    )
    needed, errors = {}, {}
    # the installer's packages together too: some only need others along with another, like (libsss_sudo if sudo)
    groups = [*((root, [root]) for root in roots), *([("install.img", data["installer"])] if data["installer"] else [])]
    for name, group in groups:
        base.reset(goal=True)
        try:
            for root in group:
                base.install(root)
            base.resolve()
        except dnf.exceptions.Error as e:
            errors[name] = str(e)
            continue
        for p in base.transaction.install_set:
            if not p.reponame.startswith("xcraft-"):
                needed[p.name] = {
                    "evr": p.evr,
                    "arch": p.arch,
                    "source": p.source_name,
                    "repo": p.reponame,
                    "sha256": p.chksum[1].hex(),
                }
    Path(sys.argv[2]).write_text(json.dumps({"packages": needed, "errors": errors}))


if __name__ == "__main__":
    main()
