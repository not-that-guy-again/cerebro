# Cerebro

Cerebro is a CLI setup tool that bootstraps an agentic development environment on a developer's machine. It installs and configures an Obsidian vault, an IDE, an LLM coding agent, and the connective tissue between them, and then gets out of the way. After initial setup the developer works in their normal tools; Cerebro is invoked occasionally to install new plugins or repair drift.

This repository currently contains the planning artifacts and an empty Python project skeleton. None of the Cerebro feature logic is implemented yet.

## Status

Planning. See [`decisions/`](decisions/) for accepted ADRs.

## Requirements

- Python 3.12 or newer (see [ADR-0001](decisions/0001-python-3-12-with-managed-venv.md))
- macOS or Linux (see [ADR-0011](decisions/0011-macos-and-linux-only.md))

## Development

```sh
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

pytest
ruff check
mypy cerebro
```

The CLI is wired up but does nothing yet beyond reporting its version:

```sh
cerebro --version
```
