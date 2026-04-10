# uvup

A CLI tool to update uv dependencies in `pyproject.toml`.

## Why?

`uv` currently has `uv sync --upgrade` which updates the `uv.lock` file but **does not** update the version constraints in `pyproject.toml`.

`uvup` fills this gap by:

1. Scanning dependencies in `pyproject.toml`
2. Using `uv remove` to remove all packages
3. Using `uv add` to reinstall them with the latest versions

## Installation

```bash
uv tool install git+https://github.com/kowyo/uvup.git@main
```

## Usage

```bash
uvup
```

## License

MIT
