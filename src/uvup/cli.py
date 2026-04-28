"""CLI application for uvup – update uv dependencies like pnpm."""

import subprocess
from collections import defaultdict
from importlib.metadata import version
from pathlib import Path
from typing import Annotated

import tomlkit
import typer

from uvup.deps import (
    base_name_from_dep,
    collect_dependencies,
    extract_marker,
    get_deps_dict,
)

# ---------------------------------------------------------------------------
# Version callback
# ---------------------------------------------------------------------------


def version_callback(value: bool) -> None:
    """Print version and exit."""
    if value:
        typer.echo(f"uvup {version('uvup')}")
        raise typer.Exit()


# ---------------------------------------------------------------------------
# Typer application
# ---------------------------------------------------------------------------

app = typer.Typer(
    help="Update uv dependencies in pyproject.toml",
    add_completion=False,
)


# ---------------------------------------------------------------------------
# Helpers used by the update command
# ---------------------------------------------------------------------------


def _add_packages(
    packages_info: list[tuple[str, str | None]],
    group: str | None = None,
    cwd: Path = Path(),
) -> None:
    """Add packages back via ``uv add``, preserving markers.

    Packages without markers are batched together.  Packages sharing the
    same marker string are batched together.

    Args:
        packages_info: List of ``(package_name, marker_or_None)`` tuples.
        group: Dependency group name (``None`` for main dependencies).
        cwd: Working directory for the ``uv add`` command.
    """
    by_marker: dict[str | None, list[str]] = defaultdict(list)
    for pkg_name, marker in packages_info:
        by_marker[marker].append(pkg_name)

    for marker, pkgs in by_marker.items():
        add_cmd = ["uv", "add"]
        if group is not None:
            add_cmd.extend(["--group", group])
        if marker is not None:
            add_cmd.extend(["--marker", marker])
        add_cmd.extend(pkgs)
        try:
            subprocess.run(
                add_cmd,
                capture_output=True,
                text=True,
                check=True,
                cwd=cwd,
            )
        except subprocess.CalledProcessError as e:
            typer.echo(f"Failed to add packages: {e.stderr}", err=True)
            raise typer.Exit(1) from e


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command()
def update(
    packages: Annotated[
        list[str] | None,
        typer.Argument(help="Specific packages to update (default: all)"),
    ] = None,
    path: Annotated[
        Path, typer.Option("--file", "-f", help="Path to pyproject.toml")
    ] = Path("pyproject.toml"),
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            "-n",
            help="Show what would be updated without making changes",
        ),
    ] = False,
    show_version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-v",
            help="Show the version and exit",
            callback=version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Update dependencies in pyproject.toml to their latest versions.

    Uses ``uv remove`` / ``uv add`` under the hood to let uv resolve the
    latest compatible versions while respecting the project's dependency
    groups and platform markers.
    """
    if not path.exists():
        typer.echo(f"Error: {path} not found", err=True)
        raise typer.Exit(1)

    # Parse TOML
    content = path.read_text()
    doc = tomlkit.parse(content)
    original_deps = get_deps_dict(doc)

    # Collect all dependencies
    all_packages = collect_dependencies(doc)

    if not all_packages:
        typer.echo("No dependencies found in pyproject.toml")
        raise typer.Exit(0)

    # Filter to specific packages if provided
    if packages:
        target_packages = set(packages)
        filtered_packages = [
            (dep, name, spec, group)
            for dep, name, spec, group in all_packages
            if base_name_from_dep(name) in target_packages
        ]
        if not filtered_packages:
            typer.echo(f"None of the specified packages found: {', '.join(packages)}")
            raise typer.Exit(1)
        all_packages = filtered_packages

    # Remove duplicates (same base package name) and exclude self
    project_name = ""
    project = doc.get("project")
    if project and isinstance(project, dict):
        project_name = project.get("name", "")

    seen = set()
    unique_packages = []
    for dep, name, spec, group in all_packages:
        base_name = base_name_from_dep(name)
        if base_name not in seen and base_name != project_name:
            seen.add(base_name)
            unique_packages.append((dep, name, spec, group))

    if dry_run:
        for _dep, name, _spec, group in unique_packages:
            base_name = base_name_from_dep(name)
            group_name = "main" if group is None else group
            typer.echo(f"Would update {base_name} ({group_name})")
        raise typer.Exit(0)

    # Group packages by their dependency group
    # Skip optional dependencies (extras) – they can't be updated with
    # ``uv remove`` / ``uv add``.
    grouped_packages: dict[str | None, list[tuple[str, str | None]]] = defaultdict(list)

    for dep, name, _spec, group in unique_packages:
        base_name = base_name_from_dep(name)
        # Skip optional dependencies (extras)
        if group and group.startswith("optional:"):
            continue

        marker = extract_marker(dep)
        grouped_packages[group].append((base_name, marker))

    if not grouped_packages:
        raise typer.Exit(0)

    cwd = path.parent

    # Step 1: Remove ALL packages from ALL groups
    for group, package_info_list in sorted(
        grouped_packages.items(), key=lambda x: (x[0] is None, x[0] or "")
    ):
        package_names = [pkg_name for pkg_name, _marker in package_info_list]
        remove_cmd = ["uv", "remove"]
        if group is not None:
            remove_cmd.extend(["--group", group])

        try:
            subprocess.run(
                remove_cmd + package_names,
                capture_output=True,
                text=True,
                check=True,
                cwd=cwd,
            )
        except subprocess.CalledProcessError as e:
            typer.echo(f"Failed to remove packages: {e.stderr}", err=True)
            raise typer.Exit(1) from e
        except FileNotFoundError as e:
            typer.echo("'uv' command not found. Please install uv.", err=True)
            raise typer.Exit(1) from e

    # Step 2: Add all packages back, preserving markers
    main_packages = grouped_packages.get(None, [])
    if main_packages:
        _add_packages(main_packages, cwd=cwd)

    for group, packages_info in sorted(
        grouped_packages.items(), key=lambda x: (x[0] is None, x[0] or "")
    ):
        if group is None or not packages_info:
            continue
        _add_packages(packages_info, group=group, cwd=cwd)

    # Compare and report updates
    new_content = path.read_text()
    new_doc = tomlkit.parse(new_content)
    new_deps = get_deps_dict(new_doc)

    updates: list[tuple[str, str, str]] = []
    for base_name in sorted(set(original_deps.keys()) & set(new_deps.keys())):
        old_dep = original_deps[base_name]
        new_dep = new_deps[base_name]
        if old_dep != new_dep:
            updates.append((base_name, old_dep, new_dep))

    if updates:
        typer.echo(f"{len(updates)} package(s) updated:")
        for name, old, new in updates:
            typer.echo(f"  {name}: {old} -> {new}")
    else:
        typer.echo("No packages updated")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point for the CLI."""
    app()
