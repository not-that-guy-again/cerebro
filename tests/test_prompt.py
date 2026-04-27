"""Tests for ``cerebro.runtime.prompt``.

The Prompt protocol exists so plugins can ask the user a question
without each plugin re-implementing input handling. NullPrompt is the
default and refuses to answer; StaticPrompt is the deterministic test
double; ClickPrompt is the production implementation.
"""

from __future__ import annotations

import pytest

from cerebro.runtime.prompt import ClickPrompt, NullPrompt, StaticPrompt


def test_null_prompt_yes_no_raises() -> None:
    with pytest.raises(RuntimeError, match="prompt is not configured"):
        NullPrompt().yes_no(question="anything?", default=False)


def test_null_prompt_choice_raises() -> None:
    with pytest.raises(RuntimeError, match="prompt is not configured"):
        NullPrompt().choice(question="x", choices=["a", "b"])


def test_static_prompt_returns_preset_yes_no() -> None:
    p = StaticPrompt(yes_no={"q1": True, "q2": False})
    assert p.yes_no(question="q1", default=False) is True
    assert p.yes_no(question="q2", default=True) is False


def test_static_prompt_yes_no_missing_key_raises() -> None:
    p = StaticPrompt(yes_no={"q1": True})
    with pytest.raises(KeyError, match="q2"):
        p.yes_no(question="q2", default=False)


def test_static_prompt_returns_preset_choice() -> None:
    p = StaticPrompt(choice={"branch": "main"})
    assert p.choice(question="branch", choices=["main", "trunk"]) == "main"


def test_static_prompt_choice_validates_against_choices() -> None:
    p = StaticPrompt(choice={"branch": "develop"})
    with pytest.raises(ValueError, match="not one of"):
        p.choice(question="branch", choices=["main", "trunk"])


def test_static_prompt_choice_missing_key_raises() -> None:
    p = StaticPrompt(choice={"branch": "main"})
    with pytest.raises(KeyError, match="other"):
        p.choice(question="other", choices=["a"])


def test_click_prompt_choice_requires_choices() -> None:
    with pytest.raises(ValueError, match="at least one"):
        ClickPrompt().choice(question="x", choices=[])


def test_click_prompt_yes_no_passes_through_to_click(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ClickPrompt.yes_no delegates to click.confirm with the right args."""
    seen: dict[str, object] = {}

    def fake_confirm(question: str, default: bool) -> bool:
        seen["question"] = question
        seen["default"] = default
        return True

    import click

    monkeypatch.setattr(click, "confirm", fake_confirm)
    cp = ClickPrompt()
    monkeypatch.setattr(cp, "_click", click)

    assert cp.yes_no(question="ok?", default=False) is True
    assert seen == {"question": "ok?", "default": False}


def test_click_prompt_choice_passes_through_to_click(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    class FakeChoice:
        def __init__(self, options: list[str]) -> None:
            self.options = list(options)

    def fake_prompt(question: str, **kwargs: object) -> str:
        seen["question"] = question
        seen.update(kwargs)
        return "two"

    import click

    monkeypatch.setattr(click, "Choice", FakeChoice)
    monkeypatch.setattr(click, "prompt", fake_prompt)
    cp = ClickPrompt()
    monkeypatch.setattr(cp, "_click", click)

    answer = cp.choice(question="pick", choices=["one", "two"], default="one")
    assert answer == "two"
    assert seen["question"] == "pick"
    assert isinstance(seen["type"], FakeChoice)
    assert seen["type"].options == ["one", "two"]
    assert seen["default"] == "one"
    assert seen["show_default"] is True
