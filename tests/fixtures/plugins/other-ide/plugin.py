"""Second synthetic IDE plugin: writes its own marker file."""

from __future__ import annotations

import os
from pathlib import Path


def _fixture_dir() -> Path:
    return Path(os.environ["CEREBRO_TEST_FIXTURE_DIR"])


def install(ctx) -> None:
    target = _fixture_dir() / "other-ide" / "marker.txt"
    ctx.fs.write_file(target, "other-ide installed\n")
