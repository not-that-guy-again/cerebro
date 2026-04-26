# ADR-0005: One Plugin Interface With Optional Hooks

## Status

Accepted.

## Context

Plugins do different things. Some install software, some only edit config files, some only register scheduled jobs. Multiple plugin classes with different contracts would balloon the type system.

## Decision

There is one plugin interface. A plugin implements any subset of these hooks:

- `install(ctx)`: perform the install. Required.
- `uninstall(ctx)`: reverse what `install` did. Required.
- `configure(ctx)`: re-run configuration without reinstalling. Called when a sibling plugin is added that this plugin targets.
- `verify(ctx)`: check that the install is intact. Used by `cerebro doctor`.
- `periodic(ctx)`: run on a schedule. Cadence declared in `plugin.yaml`.
- `on_action(ctx, action_name)`: invoked by external actors (typically agents) for specific named actions.

`ctx` is a Cerebro-supplied object exposing the state, the manifest recorder, the platform abstraction (package manager), the list of installed plugins, and a logger. Hooks return nothing on success and raise on failure.

## Consequences

Plugins can be very small (only `install` and `uninstall`) or rich (all hooks).

The core can introspect which hooks a plugin declares and skip ones it does not.

Adding a new hook type later is non-breaking: existing plugins simply do not declare it.
