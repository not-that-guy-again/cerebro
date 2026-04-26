# ADR-0009: Brew-Style Taps for Out-of-Tree Plugins

## Status

Accepted.

## Context

Cerebro ships with a curated set of first-party plugins, but third parties should be able to publish their own without forking the core. Options included a centralized registry (heavy, requires hosting), npm-style package distribution (heavy, requires a publishing pipeline), and Homebrew-style taps (lightweight, just git clones).

## Decision

A tap is a git repository with a `plugins/` directory at its root. Users register taps with `cerebro tap add <git-url>`, which clones the repo into `~/.config/cerebro/taps/<n>`. Plugins from any registered tap are discoverable by name.

When two taps provide a plugin with the same name, install fails until the user disambiguates with `cerebro install <tap>/<plugin>`.

`cerebro tap remove <n>` removes the tap and its plugins, refusing if any of those plugins are currently installed.

## Consequences

No registry, no central infrastructure, no publishing pipeline. A plugin author shares a git URL.

Plugin authors are responsible for their own quality and security. The core makes no trust claims about tap contents.

Updates are a matter of `git pull` in the tap directory, exposed as `cerebro tap update`.
