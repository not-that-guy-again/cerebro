# ADR-0001: Python 3.12 with a Managed Venv

## Status

Accepted.

## Context

Cerebro needs an implementation language. The candidates considered were Python, Go, Rust, and shell. The work involves a lot of file manipulation, YAML parsing, subprocess orchestration, and string templating. Plugins will be authored by users and contributors; the language must be approachable.

## Decision

Cerebro is written in Python 3.12. The bootstrap installer creates a venv at `~/.config/cerebro/venv` and installs Cerebro and its dependencies into it. A shim script at `~/.local/bin/cerebro` invokes the venv's Python.

## Consequences

The user does not need to think about Python versions or pip. Cerebro owns its environment.

Plugins are also Python modules, which keeps the plugin authoring story consistent.

System Python is not assumed; the bootstrap must ensure Python 3.12 is available before creating the venv (using pyenv, the system package manager, or a clear error message).

Distribution as a single static binary is not possible. This is acceptable given the set-and-forget usage pattern.
