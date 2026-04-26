# ADR-0012: Curl-Piped Bootstrap That Clones the Repo

## Status

Accepted.

## Context

Users need a frictionless way to get from zero to working Cerebro. Homebrew and rustup popularized the curl-pipe-bash pattern. Alternatives were a downloadable installer binary (requires building and hosting per platform) or expecting users to clone manually (worse UX).

## Decision

The Cerebro website (or the GitHub repo's README) provides a single command:

```
curl -fsSL https://cerebro.example/install.sh | bash
```

The install script:

1. Verifies platform is macOS or Linux
2. Verifies or installs Python 3.12
3. Clones the Cerebro repo to `~/.local/share/cerebro` (or another fixed location, not `~/repos/`)
4. Creates `~/.config/cerebro/venv` and installs Cerebro into it
5. Symlinks the `cerebro` shim into `~/.local/bin/`
6. Launches `cerebro init` to begin interactive plugin setup

## Consequences

No cloning into `~/repos/`. The user's working repo directory stays untouched.

The clone location is owned by Cerebro, so re-running the bootstrap is idempotent and safe.

Updates to Cerebro itself are a `git pull` in the clone directory, wrapped as `cerebro self-update`.

Users uncomfortable with curl-pipe-bash can read `install.sh` first and run it manually; it is a normal shell script.
