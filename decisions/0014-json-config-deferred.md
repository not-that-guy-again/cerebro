# ADR-0014: JSON Config File Editing Deferred Past v1

## Status

Accepted.

## Context

Some tools store config in JSON (Claude Desktop's `claude_desktop_config.json`, VSCode's `settings.json`). JSON does not tolerate the comment-block convention used elsewhere. Solutions include round-tripping through a JSON-with-comments parser, generating the file from plugin-contributed fragments, or owning the file outright.

## Decision

v1 does not edit JSON config files. The implications:

- Claude Desktop is not installed by Cerebro in v1.
- VSCode is installed and verified, but its `settings.json` is not edited. VSCode extensions are managed via the `code` CLI (which does not require touching `settings.json`).
- Plugins requiring JSON config edits are deferred until a follow-up ADR defines the strategy.

## Consequences

Scope for v1 stays small.

Users who want Claude Desktop configured automatically have to wait or do it themselves.

A future ADR will likely propose a "fragment merge" approach: each plugin contributes a JSON fragment, Cerebro produces the final file as the merge of fragments plus the user's manual additions, tracked separately.
