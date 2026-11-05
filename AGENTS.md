# Quality Checks

- After modifying Python code, run `uv run ruff format <files>`.
- Run `uv run ruff check --fix <files>` before completing the task.

# Code Style

- Prefer compact code. Do not assign an expression to a variable when it reads clearly as-is.
- Avoid abstractions unless they actually reduce complexity. Apply DRY only to identical use cases, or to duplicated
  code large enough to warrant it. YAGNI.
- Keep comments few, only where they help understand the code, and keep them in sync with the code.
- Keep names short when their type, docstring or module makes them clear. Rename only names that mislead, like the
  same word used for different kinds of values.
- Keep the line length at 120 and the code formatted by ruff.

# Naming and Layout

- Name functions after what they return or do: a noun for what they return (`templates_hash`, `verified_image`), a
  verb for an action (`copy_iso`, `assemble_iso`). Avoid vague words like `work`, `find`, `make` or `build` when the
  name could say what exactly is found, made or built.
- Tell related functions apart by what they start from or produce (`checkout_iso` and `snapshot_iso`), not by
  synonyms like `build` and `make`, or by a leading underscore alone.
- Use singular and plural for one item and all of them (`snapshot_iso` and `snapshot_isos`).
- Don't use a verb like `verify` or `check` for a function that returns a value; `check_*` raises and returns nothing.
- Add a suffix like `_dir` or `_hash` only when the name would be vague without it. Don't add one to names that are
  already clear, like `root` or `state`.
- Import the module rather than the function when the function's name is generic (`container.run`,
  `resolution.prepare`) or would collide with a local name (`publish.snapshot` and a `snapshot` parameter).
- Make helpers private when only their module uses them, and extract one only when that reduces complexity, not to
  pair it with a similar function.
- Order definitions in groups, with helpers before their callers: data models; fingerprints and keys; names and
  paths; cache helpers; remote operations; assembly; entry points.
- Start docstrings with what the function returns or does; put the exceptions and edge cases after.

# Type Analysis

- Run `uv run ty check <files>` before completing the task.
- Type all function signatures with builtin generics.
- Do not use quoted type annotations; `from __future__ import annotations` handles forward references.
- Do not use `Any`; model the required interface with a concrete type or protocol.

# Runtime Validation

- Use `pydantic.BaseModel` for config, state, and data crossing the CLI/file boundary.

# Tests

- Avoid mocks when possible: test on the test project, its packages and the upstream ones of `tests/upstream/`, in the
  real build environment. Add toy upstream packages there for the versions or architectures a test needs.
- Mock only what cannot happen for real in a test, like a race between two commands, or a file edited during a build.

# Emulation

- Use emulation only when it is impossible otherwise, like for the builds of another architecture. Run the rest
  natively: dnf for another architecture with `--forcearch`, or the `arch` substitutions of its API (see
  `container.run_natively()` and `container.run_dnf()`), and the tools whose output does not depend on the
  architecture, like createrepo_c, rpmsign or rpm2cpio, in the image of the host (`project.on_host()`).
