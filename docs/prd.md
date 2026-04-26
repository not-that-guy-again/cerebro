# Cerebro: Product Requirements Document

## Problem

Setting up an agentic coding environment from scratch is tedious and easy to get subtly wrong. Installing an LLM agent, an IDE, a memory system like Obsidian, and wiring them together involves many small decisions and configuration files. When a user wants to switch IDE or agent, or replicate their setup on a new machine, they redo most of it from memory. Obsidian vaults in particular accumulate cruft and structural drift over time, and most users do not maintain them well.

## Users

A single archetype for v1: an engineer who already uses or wants to use an LLM coding agent, is comfortable in a terminal, and wants their tools, memory layer, and code repos to work together coherently. They are setting up a new machine, switching tools, or formalizing what they currently do ad-hoc.

## Goals

A user can run a single curl-piped command on a fresh macOS or Linux machine and arrive at a working state with: Obsidian installed with a scaffolded vault, their chosen IDE installed, their chosen LLM agent installed and authenticated, and the agent configured to use the vault as a memory layer.

After initial install, the user can add or remove capabilities (plugins) one at a time without breaking the system.

The system surfaces drift between expected state and actual state, and offers to repair it.

The system can be uninstalled cleanly, leaving the user's code and vault contents intact.

## Non-Goals

Cerebro is not a daily-driver application. It is not an IDE, an agent, or an Obsidian replacement. It does not manage user code. It does not manage Obsidian vault contents at the note level (the agents do that). It does not synchronize state across machines.

## Requirements

### R1: Bootstrap

A single shell command clones the Cerebro repo, sets up its Python venv, and launches the interactive installer. The installer asks the user which plugins to install (starting from a minimal default of one IDE plus one agent), resolves the dependency DAG, and runs them in order.

### R2: Plugin Lifecycle

Plugins can be installed, uninstalled, enabled, disabled, listed, and inspected via the CLI. Installed plugins are recorded in `state.yaml`. Each plugin install produces a per-plugin manifest of operations performed.

### R3: Plugin Types and Dependencies

Plugins declare a type and a list of semver-ranged dependencies on other plugins. The core enforces that at least one plugin of type `ide` and one of type `agent` are installed for the system to be considered functional. Dependencies are resolved as a DAG; cycles are an error.

### R4: Cross-Plugin Configuration

When a new IDE or agent plugin is installed, plugins that target IDEs or agents (e.g. agent plugins target IDEs, behavior plugins target agents) re-run their configuration hooks against the new addition. Removing an IDE or agent triggers cleanup in plugins that were targeting it.

### R5: Shared File Editing

Plugins editing shared text files (e.g. agent rules files, shell rc files) own delimited blocks marked by comment fences. Plugins never edit blocks they do not own. The exact fence syntax may vary by file format.

### R6: Rollback

Every install operation is transactional. Failure at any step reverses operations performed so far in that transaction, leaving the system as it was before the command ran.

### R7: Drift Detection

`cerebro doctor` reports any difference between expected state (recorded in `state.yaml` and per-plugin manifests) and actual state on disk. The user can choose to repair or accept drift.

### R8: Scheduling

Plugins can register periodic hooks. The core translates these into OS-native scheduled tasks (launchd or systemd timers) and removes them on uninstall.

### R9: Auth Handoff

When a plugin install requires interactive authentication (Claude Code login, GitHub auth), the install pauses, prints clear instructions, hands control to the auth tool, and resumes when the user returns.

### R10: Taps

Users can register external git repositories as taps via `cerebro tap add <url>`. Plugins from any registered tap are installable by name. Tap removal removes the plugins it provided (or refuses if any are still installed).

### R11: Versioning

The core declares a version. Plugins declare a minimum core version they support. Installing a plugin against an incompatible core version fails loudly with a clear error.

### R12: Platforms

macOS and Linux are first-class. Windows is explicitly out of scope for v1. Plugins that wrap platform-specific install logic abstract the package manager (brew on macOS, apt or equivalent on Linux) behind a small helper layer in the core.

## Initial Plugin Set (v1)

- VSCode (`ide`)
- Claude Code (`agent`, depends on at least one `ide`)
- Coding Standards (`behavior`, applies to all `agent` plugins)
- Briefings (`workflow`, applies to all `agent` plugins)
- Claude Code Token Optimization (`behavior`, depends on Claude Code)

Claude Desktop and other IDE plugins are deferred.

## Success Criteria

A user with a fresh macOS machine and no existing tooling can run the bootstrap command, answer the interactive prompts, and within ten minutes have Obsidian, VSCode, and Claude Code installed, authenticated, configured to work together, and operating against a scaffolded Obsidian vault. They can then run `cerebro install briefings` and gain new functionality without anything else breaking. They can run `cerebro uninstall briefings` and return to the prior state.
