# ADR-0003: Plugins Are Directories With `plugin.yaml` Plus a Python Module

## Status

Accepted.

## Context

A plugin needs metadata (name, version, type, dependencies, supported platforms) and executable hooks. Options included a single Python file with metadata in dunder variables, a TOML manifest, a YAML manifest, or a JSON manifest.

## Decision

Each plugin is a directory containing:

- `plugin.yaml` with metadata
- `plugin.py` (or a package directory) implementing hooks
- Optional auxiliary files (templates, scripts, scheduled job definitions)

The manifest declares: name, version (semver), type, description, dependencies (as a map of plugin name to semver range), minimum core version, supported platforms.

## Consequences

YAML is human-friendly and matches the rest of Cerebro's user-facing config. Metadata is readable without executing code.

Hooks are plain Python functions in a known module, easy to test in isolation with mocks.

Plugins can ship templates, shell snippets, and other static assets alongside the code.

Loading a plugin requires reading the YAML, importing the Python module, and validating that declared hooks exist.
