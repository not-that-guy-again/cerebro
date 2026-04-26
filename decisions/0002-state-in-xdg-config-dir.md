# ADR-0002: State Lives in `~/.config/cerebro/`

## Status

Accepted.

## Context

Cerebro tracks installed plugins, per-plugin install manifests, cloned taps, logs, and its own venv. This state must live somewhere persistent and outside any user code repository. An earlier idea was to store state inside the cloned Cerebro repo (`~/repos/cerebro/.config/`, gitignored), but this conflates Cerebro the codebase with Cerebro the installation.

## Decision

All Cerebro state lives at `~/.config/cerebro/`. The cloned repo is just source code; reinstalling Cerebro from the same clone does not lose user state, and uninstalling Cerebro means removing this directory.

## Consequences

Standard XDG-ish layout that tools like backup utilities, dotfile managers, and the user already understand.

State survives `git clean -fdx` on the repo and survives moving or re-cloning the repo.

The bootstrap installer must create this directory if absent and migrate state from prior versions if the layout changes.
