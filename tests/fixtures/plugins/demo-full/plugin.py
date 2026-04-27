"""Synthetic plugin: writes a file, contributes a block, installs a
package, and registers a scheduled task. Used by drift-detection tests
to exercise every operation kind through one install run.
"""

from __future__ import annotations

import os
from pathlib import Path


def _fixture_dir() -> Path:
    return Path(os.environ["CEREBRO_TEST_FIXTURE_DIR"])


def install(ctx) -> None:
    target = _fixture_dir() / "demo-full"
    ctx.fs.write_file(target / "marker.txt", "demo-full installed\n")
    ctx.blocks.add(
        target / "shared.md",
        "demo-full",
        "owned by demo-full",
        format="markdown",
    )
    ctx.pkg.install("demo-pkg")
    ctx.tasks.register("demo-full-nightly", "daily@06:00", "demo-full run")
