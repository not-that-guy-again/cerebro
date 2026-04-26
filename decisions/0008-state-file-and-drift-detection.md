# ADR-0008: State File and Drift Detection

## Status

Accepted.

## Context

Cerebro needs to know what it installed, when, and at what version. Without a state file, every operation would have to scan the entire system. The state file also enables drift detection: comparing what Cerebro thinks should be present against what is actually present.

## Decision

`~/.config/cerebro/state.yaml` tracks:

- Cerebro core version
- Vault location
- For each installed plugin: name, version, source (in-tree or tap name), install timestamp, enabled flag

Per-plugin install manifests at `~/.config/cerebro/manifests/<plugin>/install.yaml` are the detailed record of operations performed.

`cerebro doctor` walks `state.yaml`, calls each plugin's `verify` hook (if declared) and re-reads the install manifest to confirm files and blocks still exist. Differences are reported with options to repair (re-run `configure` or `install`) or accept (rewrite the manifest to match reality).

## Consequences

The user can come back six months later, run `cerebro doctor`, and get a clear picture of what has changed.

Manual edits inside plugin-owned blocks surface as drift rather than being silently overwritten on the next run.

`state.yaml` is human-readable and editable in emergencies, though doing so is discouraged.
