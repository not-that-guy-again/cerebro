# ADR-0007: Operation-Log Rollback

## Status

Accepted.

## Context

Installs touch many things: package manager invocations, files written, blocks injected into shared files, scheduled tasks registered. A failure midway should leave the system as it was before the install began. Two strategies were considered: full filesystem snapshots (heavy) and operation-log rollback (lighter).

## Decision

Each plugin install runs inside a transaction. The `ctx` object exposes recorder methods like `ctx.write_file(path, content)`, `ctx.add_block(path, block_id, content)`, `ctx.run_package_manager(...)`, `ctx.register_scheduled_task(...)`. Each call records the operation and a corresponding inverse.

On success, the recorded operation list is written to `~/.config/cerebro/manifests/<plugin>/install.yaml` and is the source of truth for `uninstall`.

On failure, the recorded operations are replayed in reverse and `state.yaml` is left unchanged.

Plugins that perform raw side effects (calling subprocess directly, writing files without going through `ctx`) are responsible for their own rollback and are discouraged.

## Consequences

Rollback is bounded by what plugins record. The contract is: if you went through `ctx`, the core will undo it; if you bypassed `ctx`, you are on your own.

The manifest also serves as documentation of what a plugin actually did, which helps debugging and `cerebro doctor`.

Some operations are not cleanly invertible (e.g. installing a brew package the user already had). The recorder marks operations as "skip on uninstall if pre-existing" where applicable.
