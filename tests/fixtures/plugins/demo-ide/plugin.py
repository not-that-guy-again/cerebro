"""Synthetic IDE plugin: writes a marker file to the configured directory.

The directory is taken from ``CEREBRO_TEST_FIXTURE_DIR`` so tests can
point each scenario at a fresh location without cross-talk.
"""

from __future__ import annotations

import os
from pathlib import Path


def _fixture_dir() -> Path:
    raw = os.environ["CEREBRO_TEST_FIXTURE_DIR"]
    return Path(raw)


def install(ctx) -> None:
    target = _fixture_dir() / "demo-ide" / "marker.txt"
    ctx.fs.write_file(target, "demo-ide installed\n")
