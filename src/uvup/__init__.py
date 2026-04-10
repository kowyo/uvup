"""uvup - A CLI tool to update uv dependencies like pnpm."""

import asyncio
import re
import subprocess
from pathlib import Path
from typing import Annotated

import httpx
import tomlkit
import typer
from packaging.version import parse as parse_version
from tomlkit import TOMLDocument

app = typer.Typer(help="Update uv dependencies in pyproject.toml like pnpm")

# PyPI API base URL
PYPI_API_URL = "https://pypi.org/pypi/{package}/json"


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


def parse_version_spec(spec: str) -> tuple[str, str] | None:
    """Parse version specifier to extract operator and version.

    Args:
        spec: Version specifier like ">=2.0.0" or "==1.0.0"

    Returns:
        Tuple of (operator, version) or None if not parseable
    """
    if not spec:
        return None

    # Common version specifiers
    for op in [">=", "<=", "==", "!=", "~=", ">", "<", "==="]:
        if spec.startswith(op):
            version = spec[len(op) :].strip()
            return op, version

    return None


def build_new_spec(package_name: str, operator: str | None, new_version: str) -> str:
    """Build new dependency specification with updated version.

    Args:
        package_name: Package name (may include extras)
        operator: Version operator or None
        new_version: New version string

    Returns:
        New dependency specification string
    """
    if operator:
        return f"{package_name}{operator}{new_version}"
    return f"{package_name}>={new_version}"


async def fetch_latest_version(
    client: httpx.AsyncClient, package_name: str
) -> str | None:
    """Fetch the latest version of a package from PyPI.

    Args:
        client: HTTPX async client
        package_name: Package name to look up

    Returns:
        Latest version string or None if not found
    """
    # Strip extras from package name for API lookup
    base_name = re.sub(r"\[.*\]", "", package_name)

    try:
        response = await client.get(
            PYPI_API_URL.format(package=base_name), timeout=30.0, follow_redirects=True
        )

        if response.status_code == 404:
            return None

        response.raise_for_status()
        data = response.json()

        # Get releases and find the latest stable version
        releases = data.get("releases", {})
        if not releases:
            # Fallback to info.version if no releases
            return data.get("info", {}).get("version")

        # Filter out pre-releases, dev versions, and yanked releases
        valid_versions = []
        for v, release_info in releases.items():
            try:
                pv = parse_version(v)

                # Skip pre-releases and dev releases
                if pv.is_prerelease or pv.is_devrelease:
                    continue

                # Check if release is yanked (any file is yanked)
                if isinstance(release_info, list) and release_info:
                    is_yanked = any(f.get("yanked", False) for f in release_info)
                    if is_yanked:
                        continue

                valid_versions.append((pv, v))
            except Exception:
                continue

        if valid_versions:
            # Sort by version (highest first)
            valid_versions.sort(key=lambda x: x[0], reverse=True)
            return valid_versions[0][1]

        # If no stable versions found, try including pre-releases
        all_versions = []
        for v in releases.keys():
            try:
                pv = parse_version(v)
                all_versions.append((pv, v))
            except Exception:
                continue

        if all_versions:
            all_versions.sort(key=lambda x: x[0], reverse=True)
            return all_versions[0][1]

        return None

    except httpx.HTTPStatusError as e:
        typer.echo(f"HTTP error fetching {base_name}: {e}", err=True)
        return None
    except httpx.RequestError as e:
        typer.echo(f"Request error fetching {base_name}: {e}", err=True)
        return None
    except Exception as e:
        typer.echo(f"Error fetching {base_name}: {e}", err=True)
        return None


async def get_updates_for_packages(
    packages: list[
        tuple[str, str, str | None, str | None]
    ],  # (full_dep, package_name, version_spec, group)
    dry_run: bool = False,
) -> dict[str, tuple[str, str, str | None]]:
    """Get version updates for a list of packages.

    Args:
        packages: List of (full_dep, package_name, version_spec, group) tuples
        dry_run: If True, don't actually fetch versions

    Returns:
        Dictionary mapping base package names to (old_dep, new_dep, group) tuples
    """
    updates = {}

    async with httpx.AsyncClient() as client:
        tasks = []
        package_info = []

        for full_dep, package_name, version_spec, group in packages:
            base_name = re.sub(r"\[.*\]", "", package_name)

            # Always fetch versions, but don't apply changes in dry-run mode
            task = fetch_latest_version(client, package_name)
            tasks.append(task)
            package_info.append(
                (full_dep, package_name, version_spec, base_name, group)
            )

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for (
            full_dep,
            package_name,
            version_spec,
            base_name,
            group,
        ), latest_version in zip(package_info, results, strict=True):
            if isinstance(latest_version, Exception):
                typer.echo(f"Error checking {base_name}: {latest_version}", err=True)
                continue

            if latest_version is None:
                typer.echo(f"Could not find version for {base_name}", err=True)
                continue

            # Determine version operator and current version
            spec_info = parse_version_spec(version_spec) if version_spec else None
            operator = spec_info[0] if spec_info else ">="
            current_version = spec_info[1] if spec_info else None

            # Compare versions and skip if current is already >= latest
            try:
                if current_version:
                    current_v = parse_version(current_version)
                    latest_v = parse_version(latest_version)
                    if current_v >= latest_v:
                        continue
            except Exception:
                pass  # If parsing fails, proceed with update

            # Build new spec
            new_dep = build_new_spec(package_name, operator, latest_version)

            updates[base_name] = (full_dep, new_dep, group)

            # Show the update (with [DRY RUN] prefix if in dry-run mode)
            prefix = "[DRY RUN] " if dry_run else ""
            typer.echo(f"  {prefix}{base_name}: {full_dep} -> {new_dep}")

    return updates


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

    typer.echo(f"Reading dependencies from {path}...")

    # Parse TOML
    content = path.read_text()
    doc = tomlkit.parse(content)

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

    typer.echo(f"Found {len(unique_packages)} unique package(s) to check")

    if dry_run:
        typer.echo("\nDry run mode - no changes will be made")

    typer.echo("\nFetching latest versions...")

    # Get updates
    updates = asyncio.run(get_updates_for_packages(unique_packages, dry_run=dry_run))

    if not updates:
        typer.echo("No updates available")
        raise typer.Exit(0)

    typer.echo(f"\n{len(updates)} package(s) can be updated")

    if dry_run:
        typer.echo("\nDry run complete. No changes made.")
        raise typer.Exit(0)

    # Group updates by their dependency group
    # updates is dict[str, tuple[str, str, str | None]] -> base_name: (old_dep, new_dep, group)
    from collections import defaultdict

    grouped_updates: dict[str | None, dict[str, tuple[str, str]]] = defaultdict(dict)

    for base_name, (old_dep, new_dep, group) in updates.items():
        # Skip optional dependencies (extras) - they can't be updated with uv remove/add
        if group and group.startswith("optional:"):
            typer.echo(
                f"  Skipping {base_name} (optional dependency - manual update required)"
            )
            continue
        grouped_updates[group][base_name] = (old_dep, new_dep)

    if not grouped_updates:
        typer.echo("\nNo packages to update (all may be optional dependencies)")
        raise typer.Exit(0)

    cwd = path.parent
    total_updated = sum(len(group) for group in grouped_updates.values())

    # Step 1: Remove ALL packages from ALL groups first
    typer.echo(f"\nRemoving {total_updated} package(s) from all groups...")

    for group, group_updates in sorted(
        grouped_updates.items(), key=lambda x: (x[0] is None, x[0] or "")
    ):
        group_name = "main" if group is None else group
        package_names = list(group_updates.keys())

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
            typer.echo(f"  ✓ Removed from '{group_name}'")
        except subprocess.CalledProcessError as e:
            typer.echo(f"  ✗ Failed to remove from '{group_name}':", err=True)
            typer.echo(e.stderr, err=True)
            raise typer.Exit(1) from e
        except FileNotFoundError as e:
            typer.echo("✗ 'uv' command not found. Please install uv.", err=True)
            raise typer.Exit(1) from e

    # Step 2: Add all packages back using separate uv add commands per group
    # This ensures packages are added to the correct groups
    typer.echo(f"\nAdding {total_updated} package(s) back with 'uv add'...")

    # Collect package names by group
    main_packages = []
    group_packages: dict[str, list[str]] = defaultdict(list)

    for group, group_updates in grouped_updates.items():
        for base_name, _ in group_updates.items():
            if group is None:
                main_packages.append(base_name)
            else:
                group_packages[group].append(base_name)

    # Add main packages first (no --group flag)
    if main_packages:
        typer.echo("  Adding to main dependencies...")
        add_cmd = ["uv", "add"] + main_packages
        try:
            subprocess.run(
                add_cmd,
                capture_output=True,
                text=True,
                check=True,
                cwd=cwd,
            )
            typer.echo(f"    ✓ Added {len(main_packages)} package(s) to main")
        except subprocess.CalledProcessError as e:
            typer.echo("✗ Failed to add packages to main:", err=True)
            typer.echo(e.stderr, err=True)
            raise typer.Exit(1) from e

    # Add packages for each group separately with --group flag
    for group, packages in sorted(group_packages.items()):
        if not packages:
            continue
        typer.echo(f"  Adding to '{group}' group...")
        add_cmd = ["uv", "add", "--group", group] + packages
        try:
            subprocess.run(
                add_cmd,
                capture_output=True,
                text=True,
                check=True,
                cwd=cwd,
            )
            typer.echo(f"    ✓ Added {len(packages)} package(s) to '{group}'")
        except subprocess.CalledProcessError as e:
            typer.echo(f"✗ Failed to add packages to '{group}':", err=True)
            typer.echo(e.stderr, err=True)
            raise typer.Exit(1) from e

    typer.echo(f"\n✓ Updated {total_updated} package(s) successfully")
    typer.echo("✓ pyproject.toml and uv.lock refreshed")


def main() -> None:
    """Entry point for the CLI."""
    app()
