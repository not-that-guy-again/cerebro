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

## CLI

```sh
cerebro --help        # list commands
cerebro --version     # print core version
cerebro init          # interactive setup: vault location + plugin selection
cerebro install <plugin>
cerebro uninstall <plugin>
cerebro list                # installed plugins
cerebro list --available    # every discoverable plugin (in-tree + every tap)
cerebro enable <plugin>
cerebro disable <plugin>
cerebro tap add <git-url> [--n <name>]
cerebro tap remove <name>
cerebro tap list
cerebro tap update [<name>]
cerebro doctor        # drift detection (stub; full implementation lands later)
cerebro self-update   # git pull + reinstall in Cerebro's venv
```

Global flags:

- `--verbose` mirrors the engine's debug log to stderr in addition to the
  log file under `<state-dir>/logs/`.
- `--json` emits machine-readable JSON for commands that support it
  (e.g. `cerebro --json list`, `cerebro --json tap list`). Goes before
  the subcommand.

### Exit codes

| Code | Meaning                                                                          |
| ---- | -------------------------------------------------------------------------------- |
| 0    | Success.                                                                         |
| 1    | Generic runtime failure (engine error, reconfigure failure, IO error, etc.).     |
| 2    | Usage error (unknown option/argument; produced by Click).                        |
| 3    | Plugin not found or could not be loaded.                                         |
| 4    | Precondition failed (dependency in use, tap missing, tap still has installs).    |
| 5    | Multi-plugin install transaction rolled back after a mid-flight failure.         |

Errors print a short message to stderr; full stack traces go to the log
file unless `--verbose` is set.
