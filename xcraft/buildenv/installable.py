"""Check that each package of a repository can be installed from it, and those it builds on, on an empty system.

Run as root in the build environment: ``installable.py <output> <repository>...``, the last repository being the one
checked. It writes as JSON the packages that cannot, with why. Unlike repoclosure, the resolution evaluates rich
dependencies, like ``(a if b)``.
"""

import json
import sys
import tempfile
from pathlib import Path

import dnf  # ty: ignore[unresolved-import]


def main() -> None:
    """Check the repositories of the command line."""
    output, *repos = sys.argv[1:]
    base = dnf.Base()
    base.conf.read()
    base.conf.installroot = tempfile.mkdtemp()
    base.conf.install_weak_deps = False
    for i, repo in enumerate(repos):
        base.repos.add_new_repo(f"checked-{i}", base.conf, baseurl=[f"file://{repo}"])
    base.fill_sack(load_system_repo=False)
    errors = {}
    for package in base.sack.query().available().filter(reponame=f"checked-{len(repos) - 1}"):
        base.reset(goal=True)
        try:
            base.package_install(package)
            base.resolve()
        except dnf.exceptions.Error as e:
            errors[str(package)] = str(e)
    Path(output).write_text(json.dumps(errors))


if __name__ == "__main__":
    main()
