# Authoring Cerebro Plugins

This document is the v1 contract for plugin authors. A plugin can be
written against just this guide, the public Cerebro CLI, and the source
of the helpers it uses (`cerebro.runtime.context`, `cerebro.runtime.recorder`).

For how to publish a plugin to other users, see
[authoring-taps.md](authoring-taps.md).

## Contents

- [What a plugin is](#what-a-plugin-is)
- [Directory layout](#directory-layout)
- [`plugin.yaml`](#pluginyaml)
- [Hooks](#hooks)
- [The `ctx` object](#the-ctx-object)
- [Plugin types and what each must publish](#plugin-types-and-what-each-must-publish)
- [The comment-block convention](#the-comment-block-convention)
- [Operation recording: what gets rolled back](#operation-recording-what-gets-rolled-back)
- [Testing your plugin](#testing-your-plugin)
- [Versioning, dependencies, and `min_core_version`](#versioning-dependencies-and-min_core_version)
- [Worked example: a behavior plugin from scratch](#worked-example-a-behavior-plugin-from-scratch)

## What a plugin is

A Cerebro plugin is a directory with two files: a YAML manifest and a
Python module that implements one or more lifecycle hooks. Cerebro
discovers plugins from the in-tree directory and from every registered
tap, validates the manifest, imports the module, and calls hooks at
well-defined points in the install/uninstall lifecycle.

Plugins are not Python packages on `sys.path`. The loader imports them
by file path under a synthetic module name; you cannot
`import cerebro_plugin_…` from another plugin or from user code, and
you should not try.

See ADR-0003 for the rationale behind this layout.

## Directory layout

```
my-plugin/
├── plugin.yaml          # required: metadata
├── plugin.py            # required: hooks (module form)
└── …                    # optional: assets, templates, scripts
```

A plugin may grow into a package — replace `plugin.py` with a directory
containing `__init__.py` that exposes the hook functions — but most
plugins stay single-file.

The directory name is the plugin's discovery handle. The manifest
`name` field must match it; the loader does not enforce this, but
mismatches produce confusing error messages and break tap conflict
resolution. See [Versioning, dependencies, and
`min_core_version`](#versioning-dependencies-and-min_core_version).

## `plugin.yaml`

Every field below is part of the public contract. Validation rejects
unknown fields outright (Pydantic `extra="forbid"`).

| Field                 | Required | Type                     | Notes |
| --------------------- | -------- | ------------------------ | ----- |
| `name`                | yes      | string                   | Unique within all installed taps + in-tree. |
| `version`             | yes      | strict semver 2.0.0      | E.g. `1.0.0`, `1.2.3-beta.1`. |
| `type`                | yes      | `ide` \| `agent` \| `behavior` \| `workflow` | See [plugin types](#plugin-types-and-what-each-must-publish). |
| `description`         | yes      | string                   | One sentence shown by `cerebro list --available`. |
| `dependencies`        | no       | `{name: semver-range}`   | E.g. `claude-code: ">=1.0.0"`. Defaults to `{}`. |
| `min_core_version`    | yes      | strict semver 2.0.0      | Cerebro core version this plugin requires. |
| `supported_platforms` | yes      | list of platforms        | Non-empty. Allowed: `macos`, `linux_apt`, `linux_pacman`. (`linux` is reserved for future use.) |
| `targets`             | no       | list of strings          | Names a plugin (`claude-code`) or a sentinel (`all agent plugins`). Overrides the default targeting rule from [ADR-0004](../decisions/0004-plugin-types-and-cross-type-config.md). |
| `schedule`            | no       | mapping                  | Reserved for plugins that schedule a single periodic hook (see [Hooks](#hooks)). |
| `hooks_declared`      | no       | set of hook names        | Listing a hook here makes the loader assert it exists in `plugin.py`. Recommended for every hook you implement. |

Manifest validation rejects:

- non-semver `version` or `min_core_version`
- empty `supported_platforms`
- platforms outside the allowed set
- empty dependency names or empty/non-string version ranges
- any unrecognized top-level field

Real examples from in-tree plugins:

```yaml
# cerebro/plugins/vscode/plugin.yaml
name: vscode
version: 1.0.0
type: ide
description: Installs and verifies VSCode.
dependencies: {}
min_core_version: 0.1.0
supported_platforms:
  - macos
  - linux_apt
  - linux_pacman
hooks_declared:
  - install
  - uninstall
  - verify
```

```yaml
# cerebro/plugins/claude-code-token-optimization/plugin.yaml
name: claude-code-token-optimization
version: 1.0.0
type: behavior
description: Token-saving rules and tooling for Claude Code.
dependencies:
  claude-code: ">=1.0.0"
min_core_version: 0.1.0
supported_platforms:
  - macos
  - linux_apt
  - linux_pacman
targets:
  - claude-code
hooks_declared:
  - install
  - uninstall
  - configure
  - verify
```

```yaml
# cerebro/plugins/briefings/plugin.yaml
name: briefings
version: 1.0.0
type: workflow
description: Slash commands and scheduled jobs for briefings and summaries.
dependencies: {}
min_core_version: 0.1.0
supported_platforms:
  - macos
  - linux_apt
  - linux_pacman
targets:
  - all agent plugins
hooks_declared:
  - install
  - uninstall
  - configure
  - verify
```

## Hooks

A plugin implements any subset of the hooks below. `install` and
`uninstall` are required; the rest are optional. List every hook you
implement in `hooks_declared` so the loader fails fast on a typo.

| Hook                            | Signature                                        | When it runs |
| ------------------------------- | ------------------------------------------------ | ------------ |
| `install(ctx)`                  | `(HookContext) -> None`                          | `cerebro install <plugin>`. Required. |
| `uninstall(ctx)`                | `(HookContext) -> None`                          | `cerebro uninstall <plugin>`. Required. |
| `configure(ctx)`                | `(HookContext) -> None`                          | After a sibling plugin this plugin targets is installed (or removed). |
| `verify(ctx)`                   | `(HookContext) -> None`                          | `cerebro doctor` calls this to check on-disk state. |
| `periodic(ctx)`                 | `(HookContext) -> None`                          | On the cadence declared in `plugin.yaml`. |
| `on_action(ctx, action_name)`   | `(HookContext, str) -> None`                     | Invoked by external actors (typically agents) for named actions. |

Hook contract:

- Hooks return nothing on success and raise on failure. The engine
  catches the exception, rolls the failing plugin's recorded operations
  back in reverse order, and propagates a `PluginInstallError`.
- Multiple plugins can be installed in one transaction (a target plus
  its missing dependencies). If any plugin in the transaction fails,
  every plugin already committed in that transaction is rolled back too
  (`TransactionRollbackError`).
- After a successful install, the engine re-runs the `configure` hook
  on every installed plugin that targets the new plugin's type. A
  failure here does **not** roll back the just-installed plugin
  (`ReconfigureError`); the caller must repair the targeting plugin.
- `uninstall` is almost always a no-op (`del ctx`) — the engine drives
  uninstall by replaying the install manifest's recorded inverses. The
  hook exists so plugins can perform extra teardown that does not fit
  the recorder's model. If you find yourself doing real work in
  `uninstall`, you are probably bypassing `ctx` somewhere in `install`.

### Why most `uninstall` hooks are empty

Every helper on `ctx` records an inverse alongside the operation it
performs. On uninstall, the engine reads the persisted install manifest
and replays the inverses in reverse. The plugin author does not write
the uninstall logic — they author each side effect through `ctx` and
the recorder owns the reversal. See [Operation recording](#operation-recording-what-gets-rolled-back)
for the full list.

## The `ctx` object

Every hook receives a single positional argument, conventionally named
`ctx`, of type `cerebro.runtime.context.HookContext`. It exposes:

| Attribute               | Type                              | Purpose |
| ----------------------- | --------------------------------- | ------- |
| `ctx.state`             | `CerebroState` (read-only)        | Current installed plugins, vault path, core version. |
| `ctx.installed_plugins()` | `list[InstalledPlugin]`         | Snapshot of installed plugins. |
| `ctx.plugins_of_type(t)` | `list[InstalledPlugin]`          | Filter by `"ide"` / `"agent"` / `"behavior"` / `"workflow"`. |
| `ctx.pkg`               | `PackageManagerHelper`            | Records: install / uninstall packages. |
| `ctx.fs`                | `FilesystemHelper`                | Records: write a file. |
| `ctx.blocks`            | `BlocksHelper`                    | Records: add a managed block to a shared file. |
| `ctx.tasks`             | `ScheduledTaskHelper`             | Records: register an OS-native scheduled task. |
| `ctx.cmd`               | `CommandHelper`                   | Records: run an arbitrary command (you supply the inverse). |
| `ctx.auth`              | `AuthHandoffHelper`               | **Not recorded.** Pauses for an interactive auth step. |
| `ctx.prompt`            | `Prompt`                          | **Not recorded.** Asks the user yes/no or choice questions. |
| `ctx.log`               | `logging.Logger`                  | Per-plugin logger; routes to the engine's structured log. |
| `ctx.manifest`          | `OperationRecorder`               | The recorder itself; you almost never touch this directly. |

The full surface is documented in source on the helper classes in
[`cerebro/runtime/recorder.py`](../cerebro/runtime/recorder.py) and
[`cerebro/runtime/context.py`](../cerebro/runtime/context.py).

### What every helper records and rolls back

| Call | Recorded as | Inverse |
| ---- | ----------- | ------- |
| `ctx.fs.write_file(path, content)` | `write_file` | Restore previous content (or delete file if it didn't exist) and remove any directories the helper created on the way to `path`. |
| `ctx.blocks.add(path, plugin_name, content, format=…)` | `add_block` | Strip the named block; if the file is now empty *and* we created it, delete the file. Sibling plugins' blocks in the same file are left intact. |
| `ctx.pkg.install(package)` | `run_pkg` | `package_manager.uninstall(package)`. |
| `ctx.pkg.install_cask(package)` (macOS only) | `run_pkg` (cask=true) | `package_manager.uninstall_cask(package)`. |
| `ctx.tasks.register(name, schedule, command)` | `register_task` | `scheduler.unregister(name)`. |
| `ctx.cmd.run(argv, inverse_argv=…)` | `run_command` | Run `inverse_argv` as a subprocess. |

### Calls that do *not* record

These exist because they have side effects outside Cerebro's reach;
there is nothing to roll back.

- `ctx.auth.request(message=…)` — pauses for the user to log in to a
  third-party service. The third party's state is not Cerebro's to
  unwind.
- `ctx.prompt.yes_no(...)` / `ctx.prompt.choice(...)` — collects an
  answer from the user. If you need to remember the answer past the
  install, persist it yourself via `ctx.fs.write_file` so the recorded
  write is reversed on uninstall.
- `ctx.pkg.is_installed(...)`, `ctx.pkg.is_cask_installed(...)`,
  `ctx.tasks.is_registered(...)` — read-only queries used by `verify`
  hooks.

### "Pre-existing" semantics

Operations whose target was already in the desired state at install
time are recorded with `pre_existing=True` and **skipped** during
rollback and uninstall. This is how Cerebro avoids removing things the
user had before Cerebro touched the system.

`ctx.pkg.install` and `ctx.pkg.install_cask` set this automatically:
they read the package manager's `already_installed` reply and pass it
through. `ctx.tasks.register` does the same with `is_registered`.
For `ctx.cmd.run` you must decide and pass `pre_existing=` yourself —
the helper has no way to know whether the underlying command would
have been a no-op. The VSCode plugin's `install_companion_extension`
is the canonical pattern: query the IDE for the extension list, set
`pre_existing=True` if it is already present.

### Bypassing `ctx` is a footgun

A hook that calls `subprocess.run` directly, writes through
`pathlib.Path.write_text`, or modifies `~/.bashrc` with sed loses
rollback for that side effect. There is no warning; the operation just
does not appear in the manifest and the engine cannot reverse it. Use
`ctx.cmd.run(..., inverse_argv=…)` even for one-off tools that have no
first-class helper. The only exceptions are read-only queries and
calls to other helpers that do their own recording.

## Plugin types and what each must publish

Cerebro has four plugin types; each is a kind of contract.

### `ide`

Installs and verifies an integrated development environment. Targeted
by `agent` plugins.

**Must publish:**

- `install_companion_extension(ctx, extension_id) -> bool` — install an
  extension on behalf of a sibling plugin. Records the install through
  `ctx.cmd.run` so it is reversed when the calling plugin is
  uninstalled. Returns `True` if the call was a no-op (extension was
  already installed), `False` if newly installed.
- `uninstall_companion_extension(ctx, extension_id) -> bool` — the
  inverse, used when a calling plugin no longer wants its extension.
  Returns `True` if an extension was removed, `False` if nothing to do.

Real example: [`cerebro/plugins/vscode/plugin.py`](../cerebro/plugins/vscode/plugin.py).

### `agent`

Installs an LLM coding agent. Targets `ide` plugins (re-runs companion
extension installs against every IDE). Targeted by `behavior` and
`workflow` plugins.

**Must publish:**

- `rules_file_path(ctx) -> Path` — the absolute path to this agent's
  rules file (e.g. `~/.claude/CLAUDE.md` for Claude Code). Behavior
  plugins write managed blocks to this file via `ctx.blocks.add`.
- `register_slash_command(ctx, name, script_path) -> Path` — install a
  slash command exposed as `/<name>` in the agent. The hook reads
  `script_path`'s contents and writes them through `ctx.fs.write_file`
  to wherever the agent expects slash command files. Returns the
  destination path.
- `unregister_slash_command(ctx, name) -> bool` — remove a previously
  registered slash command. Workflow plugins use this in their
  `configure` hook to prune commands they no longer want registered;
  the normal uninstall path lets the engine replay the recorded
  `register_slash_command` write_file op instead. Returns `True` if a
  file was removed, `False` if there was nothing to remove.

Real example: [`cerebro/plugins/claude-code/plugin.py`](../cerebro/plugins/claude-code/plugin.py).

### `behavior`

Injects rules or config into one or more agent plugins. Targets
`agent` plugins by default. Override with the manifest `targets` field
to specify a single agent (e.g. `claude-code-token-optimization`
targets just `claude-code`) or to target every agent
(`all agent plugins` sentinel).

**Conventions:**

- Use `ctx.blocks.add` against each targeted agent's `rules_file_path`
  with a unique block ID (the plugin's own name, by convention) so
  multiple behavior plugins coexist in the same file without clobbering
  each other.
- Reading config from the user (selections, prompts) is allowed in
  `install` via `ctx.prompt`. Persist selections via `ctx.fs.write_file`
  to a directory under `state_dir() / "plugins" / <plugin-name> /` so
  the engine can roll the file back, and so `configure` can re-read
  them on subsequent runs without re-prompting.
- `configure` is called by the engine after a new agent is installed.
  Re-run your block injection over every installed agent — the helper
  is idempotent (`ctx.blocks.add` upserts).
- Re-running `cerebro install` on an already-installed plugin is
  rejected by the engine. To update selections today, uninstall and
  reinstall; a `cerebro reconfigure` command is planned.

Real example: [`cerebro/plugins/coding-standards/plugin.py`](../cerebro/plugins/coding-standards/plugin.py).

### `workflow`

Registers slash commands, scheduled jobs, or both, against one or more
`agent` plugins. Default target is `agent`; override with `targets` if
you only support a specific agent.

**Conventions:**

- Use the agent's published `register_slash_command(ctx, name, script_path)`
  hook for every command you ship; do not write into the agent's
  command directory directly.
- Use `ctx.tasks.register(name, schedule, command)` for scheduled jobs.
  Cadence strings are parsed by `cerebro.runtime.scheduling.ScheduleSpec`
  and accept: `hourly`, `daily@HH:MM`, `weekly:<dow>@HH:MM`,
  `monthly:last-business-day@HH:MM`. Task names must be globally unique
  (they map to launchd labels / systemd unit names); prefix them with
  `cerebro-<plugin-name>-`.
- `configure` is called when a new agent is installed; re-register your
  slash commands so the new agent gets them too.

Real example: [`cerebro/plugins/briefings/plugin.py`](../cerebro/plugins/briefings/plugin.py).

## The comment-block convention

When two or more plugins write into the same file, they use named
comment blocks so each plugin owns exactly its own section. The
convention is described in [ADR-0006](../decisions/0006-comment-block-convention.md);
this is the format reference.

A block looks like:

```
<comment-prefix> >>> cerebro:plugin:<plugin-name> >>> <comment-suffix>
... content owned by this plugin ...
<comment-prefix> <<< cerebro:plugin:<plugin-name> <<< <comment-suffix>
```

The comment syntax depends on the file format. The two formats
shipping in v1:

| Format key | Prefix  | Suffix | Example open marker |
| ---------- | ------- | ------ | ------------------- |
| `markdown` | `<!--`  | `-->`  | `<!-- >>> cerebro:plugin:my-plugin >>> -->` |
| `shell`    | `#`     | (none) | `# >>> cerebro:plugin:my-plugin >>>` |

A markdown block:

```markdown
<!-- >>> cerebro:plugin:my-plugin >>> -->
Anything goes here, including ordinary `markdown`.

- Bullets
- Code fences
- Whatever the agent should read

<!-- <<< cerebro:plugin:my-plugin <<< -->
```

A shell block:

```sh
# >>> cerebro:plugin:my-plugin >>>
export PATH="$HOME/.my-plugin/bin:$PATH"
alias mp='my-plugin --quiet'
# <<< cerebro:plugin:my-plugin <<<
```

You write a block via `ctx.blocks.add(path, plugin_name, content, format=…)`.
The helper finds-or-creates the file, upserts the block in place
(replacing previous content), and records the operation. On
uninstall, the engine strips your block and leaves sibling plugins'
blocks intact. If your plugin was the one that originally created the
file *and* removing your block leaves it empty, the engine deletes the
file (and any directories it had to create on the way).

The default format is `markdown`. Pass `format="shell"` for shell rc
files. Reading uses the same key:

```python
from cerebro.runtime.blocks import find_block, get_format

text = path.read_text(encoding="utf-8")
body = find_block(text, "my-plugin", get_format("markdown"))
if body is None:
    raise RuntimeError(...)
```

Adding a new format is one call:

```python
from cerebro.runtime.blocks import CommentSyntax, register_format

register_format("lua", CommentSyntax(prefix="--"))
# now ctx.blocks.add(path, "my-plugin", body, format="lua") works
```

Files that do not support comments (notably JSON) are out of scope for
v1 (see [ADR-0014](../decisions/0014-json-config-deferred.md)).

## Operation recording: what gets rolled back

Two layers of transaction wrap your hook:

1. **Per-plugin.** Every operation your hook performs through `ctx` is
   appended to a private `OperationRecorder`. If the hook raises, that
   recorder replays the inverses in reverse order before the engine
   sees the exception. Operations marked `pre_existing=True` are
   skipped.
2. **Per-transaction.** A single `cerebro install <target>` may install
   the target plus missing dependencies. If a later plugin fails, the
   engine reverses every plugin already committed in this transaction,
   in reverse order, by replaying their persisted install manifests.

Uninstall is a special case of layer 2: the engine reads the install
manifest from disk and replays its inverses (skipping pre-existing
ops) without ever calling the plugin's `uninstall` hook for actual
work. The hook is called, but with a typically-empty body — that is
the convention all in-tree plugins follow.

The full operation kinds and their inverses are listed in [the helpers
table above](#what-every-helper-records-and-rolls-back).

## Testing your plugin

Plugins are pure Python modules. The recommended testing strategy
mirrors the in-tree test suite:

1. **Unit-test hooks against a real `HookContext`.** Use
   `cerebro.runtime.context.build_context` to construct one with fakes
   for the platform-touching pieces. Helpers under `tests/test_recorder.py`
   in the core repo are a usable starting point: `FakePackageManager`,
   `FakeScheduler`. The package manager and scheduler live behind
   protocols in `cerebro.runtime.platform` and `cerebro.runtime.scheduling`,
   so you can write tiny in-test fakes if you only need a subset.

2. **Use `CEREBRO_HOME` to redirect state.** The state directory
   honors the `CEREBRO_HOME` environment variable; pointing it at
   `tmp_path / "home"` keeps tests off the user's real config dir.
   The in-tree test suite has an autouse fixture in
   [`tests/conftest.py`](../tests/conftest.py) that does exactly this:

   ```python
   @pytest.fixture(autouse=True)
   def isolate_cerebro_home(tmp_path, monkeypatch):
       monkeypatch.setenv("CEREBRO_HOME", str(tmp_path))
   ```

3. **Use `StaticPrompt` for plugins that prompt.** Pass a
   `cerebro.runtime.prompt.StaticPrompt(yes_no=…, choice=…)` keyed by
   the question text the plugin issues; missing keys raise so a test
   cannot drift away from the real prompts.

4. **Use `NullAuthHandoff` (or a recording double) for plugins that
   call `ctx.auth.request`.** Auth handoffs are not recorded operations,
   so the only thing to assert about them is that the plugin called
   them with a sensible message.

5. **Test the engine path too.** Drive `cerebro.runtime.engine.install`
   and `cerebro.runtime.engine.uninstall` against a temporary in-tree
   root that contains your plugin (a copy of your plugin directory),
   with `default_in_tree_root` monkeypatched to point at it. This is
   the only way to exercise the manifest persistence and the rollback
   path end-to-end.

The example plugin's test in [`tests/test_plugin_example.py`](../tests/test_plugin_example.py)
demonstrates all five points and is short enough to use as a template.

## Versioning, dependencies, and `min_core_version`

- **`version`** is strict semver 2.0.0. Pre-release tags
  (`1.0.0-beta.1`) and build metadata (`1.0.0+ci.42`) are allowed; PEP
  440 (`1.0.0b1`) is not.
- **`dependencies`** map plugin names to ranges parsed by
  `semantic_version.SimpleSpec`. The operators you can use:
  `>=`, `<=`, `<`, `>`, `==`, `!=`, `~`, `^`, `*`, plus comma-separated
  clauses. Examples: `">=1.0.0"`, `"^1.2.3"`, `">=1.0.0,<2.0.0"`.
- **`min_core_version`** is a strict semver string naming the lowest
  Cerebro core version your plugin tolerates. The engine uses this to
  refuse installs against an older core.
- Cycles in `dependencies` are detected at install time and produce a
  `DependencyResolutionError` that names every node on the cycle.
- A dependency that is already installed at a satisfying version is
  not reinstalled. A dependency at a version that does *not* satisfy
  the range raises immediately rather than silently picking the wrong
  version — Cerebro does not currently support side-by-side versions.

## Worked example: a behavior plugin from scratch

The complete worked example lives in
[`cerebro/plugins/example/`](../cerebro/plugins/example/). It is a
real plugin, installed and uninstalled by the test suite on every CI
run via [`tests/test_plugin_example.py`](../tests/test_plugin_example.py).
If the example breaks, the test fails and these docs are updated
alongside the code — that is the only mechanism preventing
documentation drift.

The example is a `behavior` plugin that:

1. Writes a per-plugin greeting file under
   `state_dir() / "plugins" / "example" / "greeting.txt"`.
2. Adds a managed `<!-- >>> cerebro:plugin:example >>> -->` block to
   every installed agent's rules file.
3. Re-runs the block injection when a new agent is installed
   (`configure`).
4. Verifies the greeting file and the managed block are present and
   unmodified (`verify`).
5. Has an empty `uninstall` hook because the engine reverses
   everything by replaying the recorded operations.

### `plugin.yaml`

```yaml
name: example
version: 1.0.0
type: behavior
description: Worked example used by the plugin authoring guide.
dependencies: {}
min_core_version: 0.1.0
supported_platforms:
  - macos
  - linux_apt
  - linux_pacman
targets:
  - all agent plugins
hooks_declared:
  - install
  - uninstall
  - configure
  - verify
```

`type: behavior` and `targets: all agent plugins` together mean the
engine will call this plugin's `configure` hook every time a new
`agent` plugin is installed.

### `plugin.py`

```python
from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

from cerebro.plugins.loader import discover_plugins, load_plugin_module
from cerebro.runtime.blocks import find_block, get_format
from cerebro.state import state_dir

if TYPE_CHECKING:
    from cerebro.models import InstalledPlugin
    from cerebro.runtime.context import HookContext

_PLUGIN_NAME = "example"
_BLOCK_FORMAT = "markdown"
_BLOCK_CONTENT = (
    "Hello from the example Cerebro plugin.\n"
    "\n"
    "This block is managed by the `example` plugin and is the worked\n"
    "example referenced in `docs/authoring-plugins.md`."
)
_GREETING_FILENAME = "greeting.txt"
_GREETING_BODY = (
    "This file was written by the example plugin.\n"
    "Removing the plugin removes this file.\n"
)


def install(ctx: HookContext) -> None:
    ctx.fs.write_file(_greeting_path(), _GREETING_BODY)
    _add_block_to_all_agents(ctx)


def uninstall(ctx: HookContext) -> None:
    del ctx  # engine replays the recorded manifest


def configure(ctx: HookContext) -> None:
    _add_block_to_all_agents(ctx)


def verify(ctx: HookContext) -> None:
    greeting = _greeting_path()
    if not greeting.exists():
        raise RuntimeError(f"greeting file is missing at {greeting}")
    if greeting.read_text(encoding="utf-8") != _GREETING_BODY:
        raise RuntimeError(
            f"greeting file at {greeting} has been modified outside Cerebro"
        )

    syntax = get_format(_BLOCK_FORMAT)
    for record, rules_path in _agent_rules_files(ctx):
        text = rules_path.read_text(encoding="utf-8")
        block = find_block(text, _PLUGIN_NAME, syntax)
        if block is None:
            raise RuntimeError(
                f"managed {_PLUGIN_NAME!r} block missing from {rules_path}"
            )
        if block.strip() != _BLOCK_CONTENT.strip():
            raise RuntimeError(
                f"managed {_PLUGIN_NAME!r} block in {rules_path} has been "
                "modified outside Cerebro"
            )


def _greeting_path() -> Path:
    return state_dir() / "plugins" / _PLUGIN_NAME / _GREETING_FILENAME


def _add_block_to_all_agents(ctx: HookContext) -> None:
    for record, rules_path in _agent_rules_files(ctx):
        ctx.blocks.add(
            rules_path,
            _PLUGIN_NAME,
            _BLOCK_CONTENT,
            format=_BLOCK_FORMAT,
        )


def _agent_rules_files(ctx: HookContext) -> list[tuple[InstalledPlugin, Path]]:
    out: list[tuple[InstalledPlugin, Path]] = []
    agents = ctx.plugins_of_type("agent")
    if not agents:
        return out
    available = discover_plugins(ctx.state)
    for record in agents:
        discovered = available.get(record.name)
        if discovered is None:
            continue
        module = load_plugin_module(discovered)
        path = _resolve_rules_file_path(record.name, module, ctx)
        if path is not None:
            out.append((record, path))
    return out


def _resolve_rules_file_path(
    agent_name: str, module: ModuleType, ctx: HookContext
) -> Path | None:
    fn = getattr(module, "rules_file_path", None)
    if fn is None or not callable(fn):
        ctx.log.warning(
            "agent plugin %r exposes no rules_file_path() hook; skipping",
            agent_name,
        )
        return None
    return fn(ctx)
```

A few things to call out:

- **`install` records two kinds of operation.** `ctx.fs.write_file`
  records a `write_file` op whose inverse restores the file's
  pre-write content (or deletes it, if it didn't exist).
  `ctx.blocks.add` records an `add_block` op whose inverse strips
  exactly this plugin's block from the file. On `cerebro uninstall
  example` the engine replays both inverses in reverse order.

- **`uninstall` is empty.** The recorder owns the reversal. You write
  the install side effects through `ctx`; the engine writes the
  uninstall logic for you.

- **`configure` re-runs the block injection.** When a new agent is
  installed (or removed), the engine fires `configure` on every plugin
  whose `targets` matches. `ctx.blocks.add` upserts, so calling it
  again over agents that already have the block is a safe no-op for
  freshly-rendered content; new agents pick up the block.

- **`verify` reads, doesn't write.** It is called by `cerebro doctor`
  and must not mutate disk. Raise on drift; the doctor command
  surfaces the message and offers `--action repair` to re-run the
  install.

- **Discovering agent plugins is two steps.** `ctx.plugins_of_type("agent")`
  gives you the installed records, but to call an agent's published
  hooks you need to import its module. `discover_plugins(ctx.state)`
  finds the manifest, `load_plugin_module(discovered)` imports the
  module, and `getattr(module, "rules_file_path", None)` gets the
  hook. Forgive agents that do not publish the hook (log and skip);
  raising would block install for any user with a half-implemented
  agent plugin.

### Test that pins the contract

The test that matters lives in
[`tests/test_plugin_example.py`](../tests/test_plugin_example.py).
The CI guard is the engine round-trip:

```python
def test_engine_install_then_uninstall_full_lifecycle(tmp_path, monkeypatch):
    home = tmp_path / "cerebro-home"
    home.mkdir()
    monkeypatch.setenv("CEREBRO_HOME", str(home))

    import cerebro.runtime.platform as platform_mod
    monkeypatch.setattr(platform_mod, "current_platform", lambda: Platform.MACOS)

    in_tree = _materialize_example(tmp_path)
    import cerebro.plugins.loader as loader_mod
    monkeypatch.setattr(loader_mod, "default_in_tree_root", lambda: in_tree)

    components = PlatformComponents(
        platform=Platform.MACOS,
        package_manager=FakePackageManager(),
        scheduler=FakeScheduler(),
    )
    save_state(
        CerebroState(core_version="0.1.0", vault_path=tmp_path / "ObsidianVault"),
        home / "state.yaml",
    )

    engine_install("example", home=home, in_tree_root=in_tree,
                   taps_root=tmp_path / "taps", components=components)
    assert (home / "plugins" / "example" / "greeting.txt").exists()

    engine_uninstall("example", home=home, in_tree_root=in_tree,
                     taps_root=tmp_path / "taps", components=components)
    assert not (home / "plugins" / "example" / "greeting.txt").exists()
```

That round-trip exercises every layer the worked example references:
manifest validation, module loading, recorder commit, install manifest
persistence, and inverse replay on uninstall. If anything in this
guide drifts from the runtime, this test fails first.

### Where to go from here

- To publish the example to other users without merging into the
  Cerebro core, see [authoring-taps.md](authoring-taps.md).
- To inspect more involved real plugins, read
  [`cerebro/plugins/coding-standards/`](../cerebro/plugins/coding-standards/)
  (a behavior plugin that prompts the user and renders a Jinja
  template), [`cerebro/plugins/briefings/`](../cerebro/plugins/briefings/)
  (a workflow plugin that registers slash commands and scheduled
  tasks), and
  [`cerebro/plugins/claude-code/`](../cerebro/plugins/claude-code/)
  (an agent plugin that publishes the `rules_file_path`,
  `register_slash_command`, and `unregister_slash_command` surfaces).
