"""Synthetic agent plugin: binds itself to every installed IDE.

- ``install`` writes a marker, then runs ``configure`` to lay down the
  IDE-bindings file. ``CEREBRO_TEST_FAIL_INSTALL=demo-agent`` forces
  ``install`` to raise so tests can assert mid-transaction rollback.
- ``configure`` rewrites the bindings file from the current set of
  installed IDE plugins. Re-runs idempotently when a sibling IDE is
  added. ``CEREBRO_TEST_FAIL_CONFIGURE=demo-agent`` forces it to raise.
"""

from __future__ import annotations

import os
from pathlib import Path


def _fixture_dir() -> Path:
    return Path(os.environ["CEREBRO_TEST_FIXTURE_DIR"])


def _bindings_path() -> Path:
    return _fixture_dir() / "demo-agent" / "ide-bindings.md"


def _current_ide_names(ctx) -> list[str]:
    return sorted(p.name for p in ctx.plugins_of_type("ide"))


def install(ctx) -> None:
    if os.environ.get("CEREBRO_TEST_FAIL_INSTALL") == "demo-agent":
        ctx.fs.write_file(_fixture_dir() / "demo-agent" / "marker.txt", "partial\n")
        raise RuntimeError("forced demo-agent install failure")

    ctx.fs.write_file(
        _fixture_dir() / "demo-agent" / "marker.txt",
        "demo-agent installed\n",
    )
    _write_bindings(ctx)


def configure(ctx) -> None:
    if os.environ.get("CEREBRO_TEST_FAIL_CONFIGURE") == "demo-agent":
        # Touch a file before raising so we can prove the recorder rolls
        # back partial configure output.
        ctx.fs.write_file(
            _fixture_dir() / "demo-agent" / "configure-attempted.txt",
            "attempted\n",
        )
        raise RuntimeError("forced demo-agent configure failure")
    _write_bindings(ctx)


def _write_bindings(ctx) -> None:
    body = "\n".join(f"- {name}" for name in _current_ide_names(ctx)) or "- (none)"
    ctx.blocks.add(
        _bindings_path(),
        "demo-agent",
        body,
        format="markdown",
    )
