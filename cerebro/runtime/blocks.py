"""Comment-block reading and writing for shared text files (ADR-0006).

Plugins contribute content to shared text files (a global rules file, a
shell rc, a CLAUDE.md) inside delimited blocks owned by exactly one
plugin. The block markers use the host file's comment syntax:

    <!-- >>> cerebro:plugin:foo >>> -->
    ... markdown content owned by foo ...
    <!-- <<< cerebro:plugin:foo <<< -->

    # >>> cerebro:plugin:foo >>>
    ... shell content owned by foo ...
    # <<< cerebro:plugin:foo <<<

This module is the platform-independent core of ``ctx.blocks``: pure
string functions plus a small registry of comment syntaxes. New formats
can register themselves via ``register_format`` without changing core
code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class CommentSyntax:
    """How a file format introduces a one-line comment.

    ``prefix`` is required; ``suffix`` is empty for line comments (``#``)
    and non-empty for delimited comments (``<!--`` / ``-->``).
    """

    prefix: str
    suffix: str = ""


_FORMATS: dict[str, CommentSyntax] = {
    "markdown": CommentSyntax(prefix="<!--", suffix="-->"),
    "shell": CommentSyntax(prefix="#"),
}


def register_format(name: str, syntax: CommentSyntax) -> None:
    _FORMATS[name] = syntax


def get_format(name: str) -> CommentSyntax:
    try:
        return _FORMATS[name]
    except KeyError as exc:
        raise KeyError(
            f"unknown comment-block format {name!r}; "
            f"known: {sorted(_FORMATS)}"
        ) from exc


def known_formats() -> list[str]:
    return sorted(_FORMATS)


def _markers(syntax: CommentSyntax, plugin_name: str) -> tuple[str, str]:
    suffix_part = f" {syntax.suffix}" if syntax.suffix else ""
    open_marker = f"{syntax.prefix} >>> cerebro:plugin:{plugin_name} >>>{suffix_part}"
    close_marker = f"{syntax.prefix} <<< cerebro:plugin:{plugin_name} <<<{suffix_part}"
    return open_marker, close_marker


def _block_pattern(syntax: CommentSyntax, plugin_name: str) -> re.Pattern[str]:
    open_marker, close_marker = _markers(syntax, plugin_name)
    # Match the block plus a trailing newline if present so removal does not
    # leave a stray blank line.
    return re.compile(
        re.escape(open_marker)
        + r"\n(?P<body>.*?)\n"
        + re.escape(close_marker)
        + r"\n?",
        re.DOTALL,
    )


def find_block(text: str, plugin_name: str, syntax: CommentSyntax) -> str | None:
    """Return the inner content of the plugin's block, or ``None`` if absent."""
    match = _block_pattern(syntax, plugin_name).search(text)
    return None if match is None else match.group("body")


def upsert_block(
    text: str,
    plugin_name: str,
    syntax: CommentSyntax,
    content: str,
) -> str:
    """Insert or replace the plugin's block. Append to ``text`` if absent."""
    open_marker, close_marker = _markers(syntax, plugin_name)
    block = f"{open_marker}\n{content}\n{close_marker}\n"
    pattern = _block_pattern(syntax, plugin_name)
    if pattern.search(text):
        return pattern.sub(block, text, count=1)
    if text and not text.endswith("\n"):
        text = text + "\n"
    return text + block


def remove_block(text: str, plugin_name: str, syntax: CommentSyntax) -> str:
    """Remove the plugin's block. Returns ``text`` unchanged if absent."""
    return _block_pattern(syntax, plugin_name).sub("", text)


__all__ = [
    "CommentSyntax",
    "find_block",
    "get_format",
    "known_formats",
    "register_format",
    "remove_block",
    "upsert_block",
]
