# ADR-0004: Plugin Types and Cross-Type Configuration

## Status

Accepted.

## Context

Plugins do heterogeneous things. Some install IDEs, some install agents, some inject behavior into agents, some register slash commands. A flat plugin model would force every plugin to be aware of every other plugin's category.

## Decision

Plugins declare a `type` in their manifest. Initial types:

- `ide`: installs and verifies an IDE
- `agent`: installs an LLM coding agent and configures it against every installed `ide` plugin
- `behavior`: injects rules or config into one or more `agent` plugins (either a specific agent or all agents)
- `workflow`: registers slash commands, scheduled jobs, or both, against one or more `agent` plugins

Plugins of type `agent` discover installed `ide` plugins and call their published companion-installer hooks. Plugins of type `behavior` and `workflow` similarly discover and target `agent` plugins.

When a new `ide` or `agent` plugin is installed, Cerebro re-runs the configuration hooks of all plugins that target that type, so existing plugins are extended to cover the new addition automatically.

## Consequences

A user can have multiple IDEs and multiple agents installed simultaneously. Adding a JetBrains IDE plugin causes every installed agent and behavior plugin to re-run its configure hook against JetBrains.

Each plugin owns the logic for checking and installing its own dependencies. Agent plugins own the logic for "how do I install my companion in this IDE?", which keeps the matrix manageable.

New types can be added to the core over time. Old plugins continue to declare their original type.
