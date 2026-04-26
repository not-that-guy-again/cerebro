# ADR-0013: Single Global Obsidian Vault With Repo References

## Status

Accepted.

## Context

The vault could live one-per-repo (high coupling, hard to query across projects), one global (easy queries, risk of leaking into a repo), or both. The risk of accidentally checking the vault into a code repo is real and bad.

## Decision

Cerebro sets up one global Obsidian vault, default location `~/ObsidianVault`, configurable at install time. The vault is never inside any tracked code repo. Each code repo the user works in gets a corresponding note in the vault under a `repos/` directory; that note links to the repo's filesystem path and accumulates architectural decisions, context, and agent-generated memory.

Architectural Decision Records that live inside a repo (its `decisions/` directory) are the source of truth for that repo's intentional choices. The vault's notes about that repo are derived context. Drift between the two is surfaced for the user to resolve.

## Consequences

Cross-repo queries (briefings, summaries) work because everything is in one vault.

Repos remain self-contained; nothing Cerebro does pollutes them.

The vault is a single point of failure for memory; backup is the user's responsibility (though Cerebro can git-init the vault as a convenience).
