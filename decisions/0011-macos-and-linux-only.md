# ADR-0011: macOS and Linux First, Windows Out of Scope

## Status

Accepted.

## Context

Supporting three platforms triples the surface area of every plugin that touches package managers, scheduled tasks, or shell environments. The target user base for v1 skews heavily toward macOS and Linux.

## Decision

v1 supports macOS and Linux. Windows is explicitly out of scope. The core abstracts over package managers (`brew` on macOS, `apt` or `pacman` or similar on Linux) behind a small helper module. Plugins call this helper rather than invoking package managers directly.

Plugins declare their supported platforms in `plugin.yaml`. Installing a plugin on an unsupported platform fails with a clear message.

## Consequences

Cross-platform testing is halved.

Windows users are not served. This is acknowledged and accepted.

Linux distributions are heterogeneous; the package-manager abstraction has to handle several cases. Initial support is for distributions with `apt` (Debian, Ubuntu) and `pacman` (Arch). Others can be added as needed.
