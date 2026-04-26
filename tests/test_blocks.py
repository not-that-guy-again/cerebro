from cerebro.runtime.blocks import (
    CommentSyntax,
    find_block,
    get_format,
    known_formats,
    register_format,
    remove_block,
    upsert_block,
)


def test_known_formats_includes_markdown_and_shell() -> None:
    formats = known_formats()
    assert "markdown" in formats
    assert "shell" in formats


def test_get_format_unknown_raises() -> None:
    try:
        get_format("toml")
    except KeyError as exc:
        assert "toml" in str(exc)
    else:
        raise AssertionError("expected KeyError")


def test_markdown_upsert_appends_to_empty() -> None:
    syntax = get_format("markdown")
    result = upsert_block("", "demo", syntax, "hello world")
    assert result == (
        "<!-- >>> cerebro:plugin:demo >>> -->\n"
        "hello world\n"
        "<!-- <<< cerebro:plugin:demo <<< -->\n"
    )


def test_markdown_upsert_appends_with_newline_separator() -> None:
    syntax = get_format("markdown")
    base = "# Title\n\nBody.\n"
    result = upsert_block(base, "demo", syntax, "hello")
    assert result.startswith(base)
    assert "<!-- >>> cerebro:plugin:demo >>> -->" in result


def test_shell_upsert_uses_hash_prefix() -> None:
    syntax = get_format("shell")
    result = upsert_block("", "demo", syntax, "export FOO=bar")
    assert result == (
        "# >>> cerebro:plugin:demo >>>\n"
        "export FOO=bar\n"
        "# <<< cerebro:plugin:demo <<<\n"
    )


def test_upsert_replaces_existing_block_in_place() -> None:
    syntax = get_format("shell")
    text = (
        "before\n"
        "# >>> cerebro:plugin:demo >>>\n"
        "old content\n"
        "# <<< cerebro:plugin:demo <<<\n"
        "after\n"
    )
    result = upsert_block(text, "demo", syntax, "new content")
    assert "old content" not in result
    assert "new content" in result
    assert result.startswith("before\n")
    assert result.endswith("after\n")


def test_upsert_only_touches_named_block() -> None:
    syntax = get_format("shell")
    text = (
        "# >>> cerebro:plugin:other >>>\n"
        "other body\n"
        "# <<< cerebro:plugin:other <<<\n"
    )
    result = upsert_block(text, "demo", syntax, "demo body")
    assert "other body" in result
    assert "demo body" in result


def test_find_block_returns_body_or_none() -> None:
    syntax = get_format("markdown")
    text = upsert_block("", "demo", syntax, "the body")
    assert find_block(text, "demo", syntax) == "the body"
    assert find_block(text, "missing", syntax) is None


def test_remove_block_drops_named_block_only() -> None:
    syntax = get_format("shell")
    base = "kept\n"
    text = upsert_block(base, "demo", syntax, "to drop")
    text = upsert_block(text, "other", syntax, "stays")
    result = remove_block(text, "demo", syntax)
    assert "to drop" not in result
    assert "stays" in result
    assert result.startswith("kept\n")


def test_remove_block_no_op_when_absent() -> None:
    syntax = get_format("markdown")
    text = "nothing here\n"
    assert remove_block(text, "demo", syntax) == text


def test_register_custom_format() -> None:
    register_format("test_lisp", CommentSyntax(prefix=";"))
    syntax = get_format("test_lisp")
    result = upsert_block("", "demo", syntax, "(defn x [])")
    assert result.startswith("; >>> cerebro:plugin:demo >>>\n")
    assert result.rstrip().endswith("; <<< cerebro:plugin:demo <<<")
