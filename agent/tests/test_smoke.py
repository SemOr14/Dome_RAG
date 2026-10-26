"""Дымовой тест: пакет импортируется."""

from __future__ import annotations

import kupol
from kupol.main import build_parser


def test_version() -> None:
    assert kupol.__version__


def test_parser_has_all_commands() -> None:
    parser = build_parser()
    for cmd, extra in [
        ("simulate", ["--scenario", "s.yaml"]),
        ("run", []),
        ("replay", ["logs/x"]),
        ("report", ["logs/x"]),
        ("dashboard", []),
    ]:
        assert parser.parse_args([cmd, *extra]).command == cmd
