# The checks run like the pre-commit hooks and the GitHub workflows, on all the files managed by git
.PHONY: install check fix test coverage clean ruff ruff-fix ruff-format ruff-format-fix ty typos typos-fix zizmor

install:
	uv sync --dev
	uv run prek install

# By default, only the hooks that do not modify the files
check: ruff ruff-format ty typos zizmor

# The hooks that fix what they can: ruff's fixes, like the order of the imports, the formatting, and the typos
fix: ruff-fix ruff-format-fix typos-fix

ruff ruff-fix ruff-format ruff-format-fix ty typos typos-fix zizmor:
	uv run prek -a $@

test:
	uv run pytest tests/ -x -q

coverage:
	uv run pytest tests/ --cov=xcraft --cov-report=term

clean:
	# keeps .xcraft/cache: the source archives are not available anywhere else yet
	rm -rf .xcraft/build .xcraft/repo .xcraft/logs .xcraft/tmp dist/ *.egg-info .coverage __pycache__/ .venv/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
