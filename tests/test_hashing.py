"""Tests for SHA-256 content hashing and size bounds."""

import ast
from pathlib import Path

import pytest

from core.models.exceptions import ParseError
from core.utils.hashing import compute_symbol_content_hash, sha256_file, sha256_text


def test_sha256_text_properties() -> None:
    """sha256_text is deterministic, prefixed, and distinct across inputs."""
    h1 = sha256_text("hello")
    h2 = sha256_text("hello")
    h3 = sha256_text("world")
    empty = sha256_text("")

    assert h1.startswith("sha256:")
    assert len(h1) == 71
    assert h1 == h2
    assert h1 != h3
    assert empty.startswith("sha256:")


def test_sha256_file_basic_and_binary(tmp_path: Path) -> None:
    """sha256_file hashes text and binary files consistently."""
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.txt"
    f1.write_text("same content", encoding="utf-8")
    f2.write_text("same content", encoding="utf-8")
    assert sha256_file(f1) == sha256_file(f2)

    binary = tmp_path / "binary.bin"
    binary.write_bytes(b"\x00\x01\x02\x03")
    assert sha256_file(binary).startswith("sha256:")


def test_sha256_file_enforces_max_file_size_bytes(tmp_path: Path) -> None:
    """sha256_file raises ParseError when file size exceeds max_file_size_bytes."""
    target = tmp_path / "big.py"
    target.write_bytes(b"x = 1\n" * 100)  # 600 bytes

    assert sha256_file(target, max_file_size_bytes=600).startswith("sha256:")

    with pytest.raises(ParseError, match="max_file_size_bytes"):
        sha256_file(target, max_file_size_bytes=599)


def test_compute_symbol_content_hash() -> None:
    """compute_symbol_content_hash slices lines when valid and falls back to ast.unparse."""
    src = "@dec\ndef foo():\n    return 1\n"
    lines = src.splitlines()
    tree = ast.parse(src)
    fn_node = tree.body[0]

    h_lines = compute_symbol_content_hash(fn_node, lines, 1, 3)
    h_fallback = compute_symbol_content_hash(fn_node, None, 1, 3)

    assert h_lines == sha256_text(src.rstrip("\n"))
    assert h_fallback.startswith("sha256:")
