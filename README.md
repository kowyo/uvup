# uvup

A CLI tool to update uv dependencies in `pyproject.toml` like `pnpm update`.

## Why?

`uv` currently has `uv sync --upgrade` which updates the `uv.lock` file but **does not** update the version constraints in `pyproject.toml`. This is similar to how `pnpm update` works differently from other package managers.

`uvup` fills this gap by:

1. Scanning dependencies in `pyproject.toml`
2. Querying PyPI for the latest stable versions
3. Updating version constraints in `pyproject.toml`
4. Running `uv lock` to update `uv.lock`

## Installation

```bash
uv tool install git+https://github.com/kowyo/uvup.git@main
```

## License

MIT
