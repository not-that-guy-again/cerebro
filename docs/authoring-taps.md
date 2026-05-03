# Authoring Cerebro Taps

A **tap** is a git repository that publishes one or more Cerebro
plugins. Users register your tap with `cerebro tap add <git-url>` and
your plugins immediately become installable by name.

There is no central registry, no review queue, no publishing pipeline.
A tap author shares a git URL.

For how to write the plugins inside a tap, see
[authoring-plugins.md](authoring-plugins.md).

## Contents

- [Repo layout](#repo-layout)
- [Registering a tap](#registering-a-tap)
- [Updating a tap](#updating-a-tap)
- [Removing a tap](#removing-a-tap)
- [Naming conflicts](#naming-conflicts)
- [Trust model: no central review](#trust-model-no-central-review)
- [Worked example: publishing the `example` plugin in a tap](#worked-example-publishing-the-example-plugin-in-a-tap)

## Repo layout

A tap is a git repository with a `plugins/` directory at its root.
Each plugin is a subdirectory of `plugins/` containing a
`plugin.yaml` and a `plugin.py` (or a package). The directory name
should match the plugin's `name` field.

```
my-cerebro-tap/                # the git repo
├── README.md                  # optional but encouraged
├── LICENSE                    # encouraged
└── plugins/
    ├── my-plugin/
    │   ├── plugin.yaml
    │   └── plugin.py
    └── my-other-plugin/
        ├── plugin.yaml
        ├── plugin.py
        └── templates/...
```

The repo can contain anything else you like at the root (CI config,
docs, scripts) — Cerebro only reads `plugins/`.

## Registering a tap

Users add your tap with the `cerebro tap` subcommand:

```sh
cerebro tap add https://github.com/you/my-cerebro-tap.git
```

This shell-clones the repo into `<state-dir>/taps/<name>` (typically
`~/.config/cerebro/taps/<name>`). The tap name is derived from the
URL by default — `my-cerebro-tap` in the example above — so users do
not need to invent one. They can override with `--name`:

```sh
cerebro tap add https://github.com/you/my-cerebro-tap.git --name acme
```

Once registered, every plugin under your tap's `plugins/` directory
becomes discoverable. `cerebro list --available` includes them, and
`cerebro install <plugin-name>` works without any further qualification
provided there is no name conflict.

`cerebro tap list` shows registered taps, their on-disk paths, their
git remote URLs, and a count of plugins each provides.

## Updating a tap

Tap "publishing" is just `git push`. Users pull updates with:

```sh
cerebro tap update            # update every registered tap
cerebro tap update <name>     # update one tap
```

Under the hood this is `git pull --ff-only` in the tap's clone. There
is no version negotiation; the plugins available after `tap update`
are exactly what is in the latest commit on the tap's default branch.

Plugin authors decide their own release cadence. Bump the `version`
field in `plugin.yaml` whenever you change behavior — the engine reads
the bumped version when an existing user reinstalls, and other plugins'
dependency ranges (e.g. `my-plugin: ">=2.0.0"`) are checked against it.

## Removing a tap

```sh
cerebro tap remove <name>
```

This refuses to remove a tap any of whose plugins are currently
installed. The user must `cerebro uninstall <plugin>` first. This is
deliberate: removing a tap while one of its plugins is installed
would orphan the install (the install manifest references a plugin
source that no longer exists), and there is no good way to recover.

## Naming conflicts

Two taps can ship plugins with the same `name`. Cerebro detects this
at discovery time and refuses to proceed until the user disambiguates.
The error names every source that declares the conflicting name and
suggests the qualified `<source>/<plugin>` syntax:

```
plugin name conflicts: 'my-plugin' declared by acme, in-tree;
qualify as one of acme/my-plugin, in-tree/my-plugin
```

Users disambiguate at install time:

```sh
cerebro install acme/my-plugin
```

In-tree plugins (those shipped with Cerebro core) use the source name
`in-tree`. A user can therefore shadow an in-tree plugin with a tap
plugin of the same name and pick which one they want explicitly.

As a tap author you should still try to avoid name collisions with
the in-tree set — they are listed in
[`cerebro/plugins/`](../cerebro/plugins/) — and with widely-used
community taps. There is no global namespace, but a unique name is
the most user-friendly choice.

## Trust model: no central review

There is no Cerebro review process for tap contents. The core makes
no trust claims about your tap, your plugins, or anything they
install. Concretely:

- **Authors own their security.** A user installing a plugin from
  your tap is running your `install` hook on their machine with
  their privileges. They are trusting you the same way they would
  trust an arbitrary `pip install` from a random repository.
- **Authors own their quality.** There is no Cerebro QA. If your
  plugin breaks, your users see the breakage; the core team will not
  triage it for you. CI on your own tap repo is the only quality gate
  in the system.
- **Authors own updates.** When a user runs `cerebro tap update`,
  whatever you have on the default branch ships. Treat the default
  branch like a release branch.

This is the same posture Homebrew takes with third-party taps and the
same posture `git clone && pip install -e .` already implies. We
documented it here so that nobody is surprised.

## Worked example: publishing the `example` plugin in a tap

This walks through the minimum repo structure to publish the
`cerebro/plugins/example/` plugin from
[authoring-plugins.md](authoring-plugins.md) as a third-party tap.

### Repo structure

Create a new git repository — call it `example-cerebro-tap` — with the
following layout:

```
example-cerebro-tap/
├── README.md
└── plugins/
    └── example/
        ├── plugin.yaml
        └── plugin.py
```

Copy `plugin.yaml` and `plugin.py` from the worked example in
[authoring-plugins.md](authoring-plugins.md#worked-example-a-behavior-plugin-from-scratch).
That is the entire mechanical step. There is no `setup.py`, no
manifest registry, no metadata file the tap itself needs.

A minimal `README.md`:

```markdown
# example-cerebro-tap

A Cerebro tap providing the `example` plugin.

## Install

    cerebro tap add https://github.com/you/example-cerebro-tap.git
    cerebro install example
```

Push the repo to a git remote your users can clone (GitHub, GitLab,
your own server — anything `git clone` accepts).

### How a user installs from your tap

```sh
cerebro tap add https://github.com/you/example-cerebro-tap.git
cerebro install example
```

The flow is:

1. `cerebro tap add` clones the repo to `~/.config/cerebro/taps/example-cerebro-tap/`.
2. `cerebro install example` discovers `plugins/example/` inside that
   clone, validates the manifest, imports the module, and runs the
   `install` hook. The plugin's recorded operations are persisted to
   `~/.config/cerebro/manifests/example/install.yaml` and the plugin
   is added to `~/.config/cerebro/state.yaml` with `source: example-cerebro-tap`.

### Publishing an update

Bump the `version` field in `plugin.yaml`, commit, and push:

```sh
# in the tap repo
$EDITOR plugins/example/plugin.yaml   # 1.0.0 -> 1.1.0
git add plugins/example/plugin.yaml
git commit -m "Bump example to 1.1.0"
git push
```

Users pull the update with:

```sh
cerebro tap update example-cerebro-tap
cerebro uninstall example
cerebro install example
```

(`cerebro reconfigure` for in-place upgrades is a planned future
addition; today the upgrade flow is uninstall + reinstall.)

### Renaming or shadowing

If the in-tree set later ships an `example` plugin, your tap's
`example` and the in-tree `example` collide. Existing users see a
`PluginConflictError` until they qualify:

```sh
cerebro install example-cerebro-tap/example     # your tap
cerebro install in-tree/example                  # the in-tree one
```

Avoid the collision in the first place by picking a unique plugin
name.

### Removing the tap

```sh
cerebro uninstall example
cerebro tap remove example-cerebro-tap
```

The order matters: `cerebro tap remove` refuses while any of the
tap's plugins are still installed.
