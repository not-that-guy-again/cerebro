from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from cerebro.models import (
    CerebroState,
    InstalledPlugin,
    InstallManifest,
    Operation,
    PluginManifest,
)


def _example_manifest() -> PluginManifest:
    return PluginManifest(
        name="demo",
        version="1.2.3",
        type="agent",
        description="A demo agent plugin.",
        dependencies={"other": ">=1.0.0,<2.0.0"},
        min_core_version="0.1.0",
        supported_platforms=["macos", "linux"],
        schedule={"cron": "0 * * * *"},
        targets=["all agent plugins"],
        hooks_declared={"install", "configure", "verify"},
    )


def _example_installed_plugin() -> InstalledPlugin:
    return InstalledPlugin(
        name="demo",
        source="in-tree",
        version="1.2.3",
        installed_at=datetime(2026, 1, 15, 9, 30, 0, tzinfo=UTC),
        enabled=True,
    )


def _example_operation() -> Operation:
    return Operation(
        kind="write_file",
        parameters={"path": "/tmp/x", "content": "hello"},
        inverse={"path": "/tmp/x", "previous_content": None},
    )


def test_plugin_manifest_round_trip() -> None:
    m = _example_manifest()
    assert PluginManifest.model_validate(m.model_dump(mode="json")) == m


def test_plugin_manifest_round_trip_with_optionals_unset() -> None:
    m = PluginManifest(
        name="bare",
        version="0.0.1",
        type="ide",
        description="",
        min_core_version="0.0.1",
        supported_platforms=["linux"],
    )
    assert PluginManifest.model_validate(m.model_dump(mode="json")) == m


def test_installed_plugin_round_trip() -> None:
    p = _example_installed_plugin()
    assert InstalledPlugin.model_validate(p.model_dump(mode="json")) == p


def test_cerebro_state_round_trip() -> None:
    s = CerebroState(
        core_version="0.1.0",
        vault_path=Path("/tmp/vault"),
        installed_plugins=[_example_installed_plugin()],
    )
    assert CerebroState.model_validate(s.model_dump(mode="json")) == s


def test_operation_round_trip() -> None:
    o = _example_operation()
    assert Operation.model_validate(o.model_dump(mode="json")) == o


def test_install_manifest_round_trip() -> None:
    m = InstallManifest(
        plugin_name="demo",
        version="1.2.3",
        operations=[
            _example_operation(),
            Operation(
                kind="register_task",
                parameters={"name": "nightly"},
                inverse={"name": "nightly"},
            ),
        ],
        installed_at=datetime(2026, 1, 15, 9, 30, 0, tzinfo=UTC),
    )
    assert InstallManifest.model_validate(m.model_dump(mode="json")) == m


def test_bad_semver_raises() -> None:
    with pytest.raises(ValidationError) as exc:
        PluginManifest(
            name="x",
            version="not-a-version",
            type="ide",
            description="",
            min_core_version="0.0.1",
            supported_platforms=["macos"],
        )
    assert "semver" in str(exc.value).lower()


def test_unknown_plugin_type_raises() -> None:
    with pytest.raises(ValidationError):
        PluginManifest(
            name="x",
            version="1.0.0",
            type="bogus",  # type: ignore[arg-type]
            description="",
            min_core_version="0.0.1",
            supported_platforms=["macos"],
        )


def test_unknown_operation_kind_raises() -> None:
    with pytest.raises(ValidationError):
        Operation(kind="not-a-real-kind")  # type: ignore[arg-type]


def test_unsupported_platform_raises() -> None:
    with pytest.raises(ValidationError):
        PluginManifest(
            name="x",
            version="1.0.0",
            type="ide",
            description="",
            min_core_version="0.0.1",
            supported_platforms=["windows"],  # type: ignore[list-item]
        )


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        PluginManifest.model_validate(
            {
                "name": "x",
                "version": "1.0.0",
                "type": "ide",
                "description": "",
                "min_core_version": "0.0.1",
                "supported_platforms": ["macos"],
                "rogue_field": "nope",
            }
        )


def test_empty_supported_platforms_raises() -> None:
    with pytest.raises(ValidationError):
        PluginManifest(
            name="x",
            version="1.0.0",
            type="ide",
            description="",
            min_core_version="0.0.1",
            supported_platforms=[],
        )
