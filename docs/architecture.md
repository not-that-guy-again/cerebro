# Cerebro: Architectural Overview

## What Cerebro Is

Cerebro is a CLI setup tool that bootstraps an agentic development environment on a user's machine. It installs and configures an Obsidian vault, an IDE, an LLM coding agent, and the connective tissue between them. After initial setup, the user works in their normal tools; Cerebro is invoked occasionally to install new plugins or repair drift.

Cerebro is not an application the user lives in. It is closer in spirit to a package manager scoped to "agentic coding setup."

## Design Principles

Small abstract core, plugins for everything else. The core knows how to load plugins, resolve dependencies, run hooks, track state, and roll back failures. It knows nothing about VSCode, Claude Code, Obsidian, or any specific tool. All product-specific logic lives in plugins.

Set-and-forget. The user runs Cerebro to install or change something, then leaves. Daily operation does not route through Cerebro.

Reversible by default. Every plugin install records what it changed so that uninstall can undo it.

Loud about drift. State on disk is the source of truth, but Cerebro keeps a record of what it expected to be there. When the two disagree, the user is told.

## System Components

### Core

A Python 3.12 CLI installed into a venv at `~/.config/cerebro/venv`, exposed via a thin shim at `~/.local/bin/cerebro` (or equivalent). The core provides:

- Plugin discovery and loading
- Dependency resolution (a DAG over installed plus requested plugins)
- Hook execution (install, uninstall, configure, periodic, on-action)
- State tracking and drift detection
- Transactional install with rollback
- The `cerebro` CLI surface (install, uninstall, list, tap, init, doctor)

### Plugins

A plugin is a directory containing a `plugin.yaml` manifest and a Python module implementing some subset of hooks. Plugins have a declared type (`ide`, `agent`, `agent-extension`, `behavior`, `workflow`, etc.) and a list of dependencies expressed as semver ranges over other plugins.

Plugins fall into rough categories. IDE plugins ensure an IDE is installed and expose hooks that agent plugins use to install companion extensions. Agent plugins install LLM coding agents and wire them up to every installed IDE plugin. Behavior plugins inject configuration or rules into existing agents. Workflow plugins register slash commands or scheduled jobs.

The first-party plugins shipped with v1 are VSCode (IDE), Claude Code (agent), Coding Standards (behavior), Briefings (workflow), Claude Code Token Optimization (behavior, agent-specific).

### Taps

Plugins live either in-tree under `cerebro/plugins/` or in external git repositories registered as taps. `cerebro tap add <git-url>` clones a tap into `~/.config/cerebro/taps/<name>`. Plugins from any registered tap are then installable by name.

### State

Cerebro's state lives at `~/.config/cerebro/`. The structure is:

```
~/.config/cerebro/
  venv/                  # Python venv for cerebro itself
  state.yaml             # Installed plugins, versions, install timestamps
  manifests/<plugin>/    # Per-plugin install records (files written, ops performed)
  taps/<name>/           # Cloned tap repos
  logs/                  # Install/uninstall/hook logs
```

The Obsidian vault lives wherever the user wants it (default `~/ObsidianVault`); its location is recorded in `state.yaml`. The vault itself is not Cerebro state, it is user data.

### Comment-Block Convention

When multiple plugins write into a shared text file (markdown, shell rc, agent rules files), each plugin owns a block delimited by start and end comments. The exact comment syntax varies by file format; for markdown it looks like:

```
<!-- >>> cerebro:plugin:coding-standards >>> -->
... plugin-managed content ...
<!-- <<< cerebro:plugin:coding-standards <<< -->
```

Plugins only edit their own blocks. Cerebro enforces this on install and uninstall. JSON config files are out of scope for v1.

## How a Typical Install Works

The user runs `cerebro install briefings`. The core resolves the DAG, sees Briefings depends on at least one agent plugin being installed, and confirms Claude Code is present. It begins a transaction. It calls `briefings.install()`, which writes scheduled job definitions, registers slash commands with every installed agent, and records each operation to a manifest. If any step fails, the recorded operations are reversed in order. On success, `state.yaml` is updated.

## How Drift Detection Works

`cerebro doctor` walks `state.yaml`, asks each plugin to verify its install is intact, and compares the result against expectations. A plugin's verify hook can re-read the files it owns and confirm checksums or required content. When drift is found, the user is offered repair (re-running the relevant hook) or acceptance (updating state to match reality).

## Scheduling and Action Hooks

Time-based hooks register OS-level scheduled tasks (launchd on macOS, systemd timers on Linux) at install time and tear them down on uninstall. Action-based hooks are invoked by the agent itself, not by Cerebro, since "the user started work in a new repo" is observable to the agent but not to Cerebro. Cerebro provides the agent with the hook scripts and the configuration that points at them; the agent decides when to run them.

## Out of Scope for v1

Windows support, Claude Desktop installation, JSON config file management, GUI, plugin marketplace beyond the tap mechanism, multi-user setups.
