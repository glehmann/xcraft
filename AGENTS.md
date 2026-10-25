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
