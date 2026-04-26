# ADR-0010: OS-Native Scheduling Over a Daemon

## Status

Accepted.

## Context

Some plugins need to run periodic work (e.g. a daily Obsidian vault hygiene pass). Options were: a Cerebro daemon, OS-native scheduled tasks, or expecting the user to invoke a `cerebro tick` command manually.

## Decision

Periodic plugin hooks are translated into OS-native scheduled tasks at install time. macOS uses launchd plists in `~/Library/LaunchAgents/`. Linux uses systemd user timers in `~/.config/systemd/user/`. The core provides a small abstraction so plugins declare cadence in `plugin.yaml` (e.g. `schedule: daily@06:00`) and do not write platform-specific files themselves.

Action-based hooks (e.g. "agent started work in a new repo") are not Cerebro's responsibility to trigger. The agent observes the action and invokes the plugin-supplied script directly. Cerebro's job is to ensure the script is in the agent's known location.

## Consequences

No daemon means no extra long-running process to manage, monitor, or recover.

Schedules survive reboots without Cerebro running.

Uninstall must remove the registered tasks; this is part of the operation log.

Linux distributions without systemd are not supported in v1. (cron could be added later as a fallback.)
