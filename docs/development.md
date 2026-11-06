# Development

See [Concepts and code map](code-overview.md) for the core concepts, build flow, and where to implement features.

```bash
make install    # the dependencies, with the dev ones, and the pre-commit hooks
make check      # ruff, ruff format, ty, typos and zizmor, like the GitHub workflows, without changing anything
make fix        # ruff's fixes, like the order of the imports, the formatting, and the typos
make test       # pytest, in parallel; the build tests need podman, and the network the first time, for the image
```

Each check also runs alone, by the id of its hook in `.pre-commit-config.yaml`, like `make ty`. The workflows run for
the pushes to master and the pull requests, those of forks too. The tests run in parallel with pytest-xdist, each worker
with its own Garage server for the tests of the remote storage: `pytest -n0` runs them in one process, like to debug one
with `--pdb`. `pytest -m 'not podman'` runs the tests which need no container, in seconds: the others have the `podman`
marker. The tests have upstream packages of their own too, built once from the specs of `tests/upstream/` into a
repository on this machine, `tests/upstream/repos/`, with versions to update to, some for one architecture only.
