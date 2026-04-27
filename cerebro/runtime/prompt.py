"""Interactive-prompt interface used by ``ctx.prompt``.

Plugins that need to ask the user a question during ``install`` (or
during ``configure``) call ``ctx.prompt`` rather than touching ``click``
or ``input()`` directly. The interface lets the engine hand a click-
backed prompt to plugins in production while tests inject a deterministic
double — no patching of stdin required.

Like :mod:`cerebro.runtime.auth`, prompt calls are not recorded
operations. The user's answers are persisted by the calling plugin (in
its own state file) when they need to outlive the install.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable


@runtime_checkable
class Prompt(Protocol):
    def yes_no(self, *, question: str, default: bool) -> bool: ...

    def choice(
        self,
        *,
        question: str,
        choices: Sequence[str],
        default: str | None = None,
    ) -> str: ...


class NullPrompt:
    """Default prompt that refuses to answer.

    Wired in by ``build_context`` when the caller did not provide one.
    Plugins that try to prompt without an installed prompt helper raise
    immediately; this is preferable to silently picking a default.
    """

    def yes_no(self, *, question: str, default: bool) -> bool:  # noqa: D401
        del question, default
        raise RuntimeError(
            "ctx.prompt is not configured for this install; the caller "
            "must inject a Prompt to run interactive plugins."
        )

    def choice(
        self,
        *,
        question: str,
        choices: Sequence[str],
        default: str | None = None,
    ) -> str:
        del question, choices, default
        raise RuntimeError(
            "ctx.prompt is not configured for this install; the caller "
            "must inject a Prompt to run interactive plugins."
        )


class StaticPrompt:
    """Test/init double: returns a fixed answer for each labelled question.

    The plugin asks ``yes_no(question="commits.conventional", ...)`` and
    the test pre-populates ``yes_no={"commits.conventional": True}``.
    Missing keys raise so a test cannot accidentally drift away from the
    real prompt the plugin issues.
    """

    def __init__(
        self,
        *,
        yes_no: dict[str, bool] | None = None,
        choice: dict[str, str] | None = None,
    ) -> None:
        self._yes_no = dict(yes_no or {})
        self._choice = dict(choice or {})

    def yes_no(self, *, question: str, default: bool) -> bool:
        del default
        if question not in self._yes_no:
            raise KeyError(
                f"StaticPrompt has no yes_no answer for {question!r}"
            )
        return self._yes_no[question]

    def choice(
        self,
        *,
        question: str,
        choices: Sequence[str],
        default: str | None = None,
    ) -> str:
        del default
        if question not in self._choice:
            raise KeyError(
                f"StaticPrompt has no choice answer for {question!r}"
            )
        answer = self._choice[question]
        if answer not in choices:
            raise ValueError(
                f"StaticPrompt answer {answer!r} for {question!r} is not "
                f"one of {list(choices)!r}"
            )
        return answer


class ClickPrompt:
    """Production prompt that delegates to ``click.prompt`` / ``click.confirm``.

    Defined here rather than in the CLI module so the runtime layer does
    not depend on a CLI module choosing it implicitly. The CLI's engine
    invocations pass an instance of this class explicitly.
    """

    def __init__(self) -> None:
        # Imported lazily so the runtime package does not pay click import
        # cost when callers (notably tests) never use ClickPrompt.
        import click

        self._click = click

    def yes_no(self, *, question: str, default: bool) -> bool:
        return bool(self._click.confirm(question, default=default))

    def choice(
        self,
        *,
        question: str,
        choices: Sequence[str],
        default: str | None = None,
    ) -> str:
        if not choices:
            raise ValueError("choice() requires at least one option")
        choice_type = self._click.Choice(list(choices))
        if default is None:
            return str(self._click.prompt(question, type=choice_type))
        return str(
            self._click.prompt(
                question,
                type=choice_type,
                default=default,
                show_default=True,
            )
        )


__all__ = ["ClickPrompt", "NullPrompt", "Prompt", "StaticPrompt"]
