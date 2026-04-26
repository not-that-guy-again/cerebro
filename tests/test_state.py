from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from cerebro.models import (
    CerebroState,
    InstalledPlugin,
    InstallManifest,
    Operation,
    PluginManifest,
)
from cerebro.state import (
    load_install_manifest,
    load_plugin_manifest,
    load_state,
    save_install_manifest,
    save_state,
    state_dir,
)


def test_state_dir_honors_cerebro_home(tmp_path: Path) -> None:
    # The autouse fixture sets CEREBRO_HOME to tmp_path.
    assert state_dir() == tmp_path


def test_state_dir_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CEREBRO_HOME", raising=False)
    assert state_dir() == Path("~/.config/cerebro").expanduser()


def test_state_dir_expands_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEREBRO_HOME", "~/somewhere")
    assert state_dir() == Path("~/somewhere").expanduser()


def test_state_round_trip_to_disk(tmp_path: Path) -> None:
    s = CerebroState(
        core_version="0.1.0",
        vault_path=tmp_path / "vault",
        installed_plugins=[
            InstalledPlugin(
                name="foo",
                source="in-tree",
                version="1.0.0",
                installed_at=datetime(2026, 1, 1, tzinfo=UTC),
                enabled=True,
            ),
            InstalledPlugin(
                name="bar",
                source="my-tap",
                version="2.0.0-beta.1",
                installed_at=datetime(2026, 2, 1, 12, 0, tzinfo=UTC),
                enabled=False,
            ),
        ],
    )
    path = tmp_path / "state.yaml"
    save_state(s, path)
    assert path.exists()
    assert load_state(path) == s


def test_install_manifest_round_trip_to_disk(tmp_path: Path) -> None:
    m = InstallManifest(
        plugin_name="foo",
        version="1.0.0",
        operations=[
            Operation(
                kind="add_block",
                parameters={"path": "~/.zshrc", "block_id": "foo"},
                inverse={"path": "~/.zshrc", "block_id": "foo"},
            ),
            Operation(
                kind="run_pkg",
                parameters={"manager": "brew", "package": "ripgrep"},
                inverse={"manager": "brew", "package": "ripgrep", "skip_if_pre_existing": True},
            ),
        ],
        installed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    path = tmp_path / "manifests" / "foo" / "install.yaml"
    save_install_manifest(m, path)
    assert load_install_manifest(path) == m


def test_save_creates_parent_dirs(tmp_path: Path) -> None:
    s = CerebroState(core_version="0.1.0", vault_path=tmp_path / "v")
    nested = tmp_path / "deeply" / "nested" / "state.yaml"
    save_state(s, nested)
    assert nested.exists()


def test_load_plugin_manifest_from_directory(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "plugins" / "demo"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        "\n".join(
            [
                "name: demo",
                "version: 0.1.0",
                "type: behavior",
                "description: A demo plugin.",
                "dependencies: {}",
                "min_core_version: 0.0.1",
                "supported_platforms:",
                "  - macos",
                "hooks_declared:",
                "  - install",
                "  - verify",
                "",
            ]
        )
    )
    m = load_plugin_manifest(plugin_dir)
    assert m.name == "demo"
    assert m.type == "behavior"
    assert m.hooks_declared == {"install", "verify"}


def test_plugin_manifest_round_trip_via_disk(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "plugins" / "rt"
    plugin_dir.mkdir(parents=True)
    original = PluginManifest(
        name="rt",
        version="3.0.0",
        type="workflow",
        description="round trip",
        dependencies={"core": ">=0.1.0"},
        min_core_version="0.1.0",
        supported_platforms=["macos", "linux"],
        schedule={"cron": "*/15 * * * *"},
        targets=["all agent plugins"],
        hooks_declared={"install", "configure"},
    )
    manifest_path = plugin_dir / "plugin.yaml"
    with manifest_path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(original.model_dump(mode="json"), fh, sort_keys=False)
    assert load_plugin_manifest(plugin_dir) == original


def test_load_invalid_manifest_raises(tmp_path: Path) -> None:
    path = tmp_path / "manifests" / "bad" / "install.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "plugin_name: x\n"
        "version: not-a-semver\n"
        "operations: []\n"
        "installed_at: 2026-01-01T00:00:00Z\n"
    )
    with pytest.raises(ValidationError):
        load_install_manifest(path)


def test_load_non_mapping_raises(tmp_path: Path) -> None:
    path = tmp_path / "state.yaml"
    path.write_text("- just\n- a\n- list\n")
    with pytest.raises(ValueError, match="mapping"):
        load_state(path)


def test_load_empty_file_treated_as_empty_mapping(tmp_path: Path) -> None:
    path = tmp_path / "manifests" / "x" / "install.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("")
    # Empty mapping is invalid for InstallManifest (missing required fields), so
    # it should surface a clear pydantic ValidationError rather than a YAML or
    # AttributeError.
    with pytest.raises(ValidationError):
        load_install_manifest(path)


def test_save_then_yaml_is_human_readable(tmp_path: Path) -> None:
    s = CerebroState(core_version="0.1.0", vault_path=tmp_path / "vault")
    path = tmp_path / "state.yaml"
    save_state(s, path)
    text = path.read_text()
    # Block style by default — no flow-style braces around the top-level mapping.
    assert "core_version: 0.1.0" in text
    assert "{" not in text.splitlines()[0]


def test_no_writes_under_real_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Belt-and-suspenders: even if a caller ignores the env var and uses a
    # bare relative path, our state_dir() must point under tmp_path here.
    real_home_marker = Path.home() / ".config" / "cerebro" / "__should_not_exist__"
    s = CerebroState(core_version="0.1.0", vault_path=tmp_path / "vault")
    save_state(s, state_dir() / "state.yaml")
    assert not real_home_marker.exists()
