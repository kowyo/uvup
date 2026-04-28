"""Dependency extraction and collection utilities for pyproject.toml."""

import re

from packaging.requirements import Requirement
from tomlkit import TOMLDocument


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


def base_name_from_dep(dep: str) -> str:
    """Extract the bare package name (without extras or version specifiers).

    Args:
        dep: Dependency string like "requests[security]>=2.0.0"

    Returns:
        The base package name (e.g. "requests")
    """
    name, _ = extract_package_name(dep)
    return re.sub(r"\[.*\]", "", name)


def extract_marker(dep: str) -> str | None:
    """Extract PEP 508 marker from a dependency string, if present.

    Args:
        dep: A PEP 508 dependency string

    Returns:
        The marker string, or None if no marker is present
    """
    try:
        req = Requirement(dep)
        if req.marker:
            return str(req.marker)
    except Exception:
        pass
    return None


def collect_dependencies(
    doc: TOMLDocument,
) -> list[tuple[str, str, str | None, str | None]]:
    """Collect all dependencies from pyproject.toml.

    Searches in order:
      - ``project.dependencies`` (main dependencies)
      - ``project.optional-dependencies`` (extras)
      - ``dependency-groups`` (PEP 735 groups)
      - ``tool.uv.dev-dependencies`` (legacy, treated as "dev" group)

    Args:
        doc: Parsed TOML document

    Returns:
        List of (full_dep, package_name, version_spec, group) tuples.
        ``group`` is ``None`` for main dependencies; for dependency groups
        it is the group name; for extras it is ``"optional:<name>"``.
    """
    packages: list[tuple[str, str, str | None, str | None]] = []

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


def get_deps_dict(doc: TOMLDocument) -> dict[str, str]:
    """Build a flat mapping of base package name → full dependency string.

    Iterates over all sections of the TOML document that can hold
    dependencies (main, optional, groups, legacy dev) and returns a
    single dictionary keyed by the bare package name.

    Args:
        doc: Parsed TOML document

    Returns:
        Dictionary mapping bare package names to their full dependency
        strings (e.g. ``{"requests": "requests>=2.31.0"}``).
    """
    deps: dict[str, str] = {}
    project = doc.get("project")
    if project and isinstance(project, dict):
        for dep in project.get("dependencies", []):
            if isinstance(dep, str):
                deps[base_name_from_dep(dep)] = dep
        # Optional dependencies
        optional = project.get("optional-dependencies")
        if optional and isinstance(optional, dict):
            for extra_deps in optional.values():
                for dep in extra_deps:
                    if isinstance(dep, str):
                        deps[base_name_from_dep(dep)] = dep
    # Dependency groups
    dep_groups = doc.get("dependency-groups")
    if dep_groups and isinstance(dep_groups, dict):
        for group_deps in dep_groups.values():
            for dep in group_deps:
                if isinstance(dep, str):
                    deps[base_name_from_dep(dep)] = dep
    # Legacy dev-dependencies
    tool = doc.get("tool")
    if tool and isinstance(tool, dict):
        uv = tool.get("uv")
        if uv and isinstance(uv, dict):
            for dep in uv.get("dev-dependencies", []):
                if isinstance(dep, str):
                    deps[base_name_from_dep(dep)] = dep
    return deps
