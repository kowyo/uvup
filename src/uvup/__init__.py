"""uvup - A CLI tool to update uv dependencies like pnpm."""

import re
import subprocess
from pathlib import Path
from typing import Annotated

import tomlkit
import typer
from tomlkit import TOMLDocument

app = typer.Typer(help="Update uv dependencies in pyproject.toml like pnpm")


def extract_package_name(dep: str) -> tuple[str, str]:
    """Extract package name and version specifier from a dependency string.

    Args:
        dep: Dependency string like "requests>=2.0.0" or "httpx"

    Returns:
        Tuple of (package_name, version_spec)
    """
    # Handle extras like "requests[security]>=2.0.0"
    match = re.match(r"^([a-zA-Z0-9_-]+(?:\[[^\]]+\])?)(.*)$", dep)
    if match:
        return match.group(1), match.group(2)
    return dep, ""


def collect_dependencies(
    doc: TOMLDocument,
) -> list[tuple[str, str, str | None, str | None]]:
    """Collect all dependencies from pyproject.toml.

    Args:
        doc: Parsed TOML document

    Returns:
        List of (full_dep, package_name, version_spec, group) tuples.
        group is None for main dependencies, or the group name for dependency groups.
    """
    packages = []

    # Main project dependencies
    project = doc.get("project")
    if project and isinstance(project, dict):
        deps = project.get("dependencies", [])
        if deps:
            for dep in deps:
                if isinstance(dep, str):
                    package_name, version_spec = extract_package_name(dep)
                    packages.append(
                        (
                            dep,
                            package_name,
                            version_spec if version_spec else None,
                            None,
                        )
                    )

    # Optional dependencies (extras) - group is "optional" for tracking
    optional_deps = project.get("optional-dependencies") if project else None
    if optional_deps and isinstance(optional_deps, dict):
        for extra_name, extra_deps in optional_deps.items():
            for dep in extra_deps:
                if isinstance(dep, str):
                    package_name, version_spec = extract_package_name(dep)
                    packages.append(
                        (
                            dep,
                            package_name,
                            version_spec if version_spec else None,
                            f"optional:{extra_name}",
                        )
                    )

    # Dependency groups (PEP 735 style, used by uv)
    dep_groups = doc.get("dependency-groups")
    if dep_groups and isinstance(dep_groups, dict):
        for group_name, group_deps in dep_groups.items():
            for dep in group_deps:
                if isinstance(dep, str):
                    package_name, version_spec = extract_package_name(dep)
                    packages.append(
                        (
                            dep,
                            package_name,
                            version_spec if version_spec else None,
                            group_name,
                        )
                    )

    # Legacy tool.uv.dev-dependencies for backwards compatibility - treated as "dev" group
    tool = doc.get("tool")
    if tool and isinstance(tool, dict):
        uv = tool.get("uv")
        if uv and isinstance(uv, dict):
            dev_deps = uv.get("dev-dependencies", [])
            if dev_deps:
                for dep in dev_deps:
                    if isinstance(dep, str):
                        package_name, version_spec = extract_package_name(dep)
                        packages.append(
                            (
                                dep,
                                package_name,
                                version_spec if version_spec else None,
                                "dev",
                            )
                        )

    return packages


@app.command()
def update(
    packages: Annotated[
        list[str], typer.Argument(help="Specific packages to update (default: all)")
    ] = None,
    path: Annotated[
        Path, typer.Option("--file", "-f", help="Path to pyproject.toml")
    ] = Path("pyproject.toml"),
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", "-n", help="Show what would be updated without making changes"
        ),
    ] = False,
) -> None:
    """Update dependencies in pyproject.toml to their latest versions."""

    if not path.exists():
        typer.echo(f"Error: {path} not found", err=True)
        raise typer.Exit(1)

    # Store original dependencies for comparison
    def get_deps_dict(doc: TOMLDocument) -> dict[str, str]:
        """Get dictionary of base package name -> full dependency string."""
        deps = {}
        project = doc.get("project")
        if project and isinstance(project, dict):
            for dep in project.get("dependencies", []):
                if isinstance(dep, str):
                    base_name = re.sub(r"\[.*\]", "", extract_package_name(dep)[0])
                    deps[base_name] = dep
            # Optional dependencies
            optional = project.get("optional-dependencies")
            if optional and isinstance(optional, dict):
                for extra_deps in optional.values():
                    for dep in extra_deps:
                        if isinstance(dep, str):
                            base_name = re.sub(
                                r"\[.*\]", "", extract_package_name(dep)[0]
                            )
                            deps[base_name] = dep
        # Dependency groups
        dep_groups = doc.get("dependency-groups")
        if dep_groups and isinstance(dep_groups, dict):
            for group_deps in dep_groups.values():
                for dep in group_deps:
                    if isinstance(dep, str):
                        base_name = re.sub(r"\[.*\]", "", extract_package_name(dep)[0])
                        deps[base_name] = dep
        # Legacy dev-dependencies
        tool = doc.get("tool")
        if tool and isinstance(tool, dict):
            uv = tool.get("uv")
            if uv and isinstance(uv, dict):
                for dep in uv.get("dev-dependencies", []):
                    if isinstance(dep, str):
                        base_name = re.sub(r"\[.*\]", "", extract_package_name(dep)[0])
                        deps[base_name] = dep
        return deps

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
            if re.sub(r"\[.*\]", "", name) in target_packages
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
        base_name = re.sub(r"\[.*\]", "", name)
        if base_name not in seen and base_name != project_name:
            seen.add(base_name)
            unique_packages.append((dep, name, spec, group))

    if dry_run:
        for _dep, name, _spec, group in unique_packages:
            base_name = re.sub(r"\[.*\]", "", name)
            group_name = "main" if group is None else group
            typer.echo(f"Would update {base_name} ({group_name})")
        raise typer.Exit(0)

    # Group packages by their dependency group
    # Skip optional dependencies (extras) - they can't be updated with uv remove/add
    from collections import defaultdict

    grouped_packages: dict[str | None, list[str]] = defaultdict(list)

    for _dep, name, _spec, group in unique_packages:
        base_name = re.sub(r"\[.*\]", "", name)
        # Skip optional dependencies (extras) - they can't be updated with uv remove/add
        if group and group.startswith("optional:"):
            continue
        grouped_packages[group].append(base_name)

    if not grouped_packages:
        raise typer.Exit(0)

    cwd = path.parent

    # Step 1: Remove ALL packages from ALL groups first
    for group, package_names in sorted(
        grouped_packages.items(), key=lambda x: (x[0] is None, x[0] or "")
    ):
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

    # Step 2: Add all packages back using separate uv add commands per group
    # This ensures packages are added to the correct groups
    # Add main packages first (no --group flag)
    main_packages = grouped_packages.get(None, [])
    if main_packages:
        add_cmd = ["uv", "add"] + main_packages
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

    # Add packages for each group separately with --group flag
    for group, packages in sorted(
        grouped_packages.items(), key=lambda x: (x[0] is None, x[0] or "")
    ):
        if group is None or not packages:
            continue
        add_cmd = ["uv", "add", "--group", group] + packages
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

    # Compare and report updates
    new_content = path.read_text()
    new_doc = tomlkit.parse(new_content)
    new_deps = get_deps_dict(new_doc)

    updates: list[tuple[str, str, str]] = []  # (package_name, old_dep, new_dep)
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


def main() -> None:
    """Entry point for the CLI."""
    app()
