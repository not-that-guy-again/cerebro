# ADR-0006: Comment-Block Convention for Shared Files

## Status

Accepted.

## Context

Multiple plugins may need to write into the same file (a shared agent rules file, a shell rc, a global CLAUDE.md). Last-write-wins is destructive. A merge step is complex. Plugin-owned blocks are a well-understood pattern.

## Decision

When a plugin contributes content to a shared text file, the content is wrapped in a delimited block:

```
<comment-syntax> >>> cerebro:plugin:<plugin-name> >>>
... content owned by this plugin ...
<comment-syntax> <<< cerebro:plugin:<plugin-name> <<<
```

The comment syntax is per-format (`#` for shell, `<!-- ... -->` for markdown, etc.). Plugins only ever read or modify their own block. The core provides a helper to find, replace, append, or remove a plugin's block in a given file.

Files that do not support comments (notably JSON) are out of scope for v1. Plugins that need to write JSON config will be revisited in a future ADR.

## Consequences

Plugins compose cleanly when editing the same file.

A user inspecting an edited file can immediately see which plugin owns which section.

Uninstall is a matter of removing the named block.

If a user manually edits inside a plugin's block, drift detection will catch it on the next `cerebro doctor` run.
