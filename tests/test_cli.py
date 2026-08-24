"""Tests for the single-command launcher."""

from __future__ import annotations

import pytest

from bismuth.cli.main import _parser, main


def test_bare_cli_has_only_launch_options() -> None:
    args = _parser().parse_args([])

    assert args.vault is None
    assert args.host is None
    assert args.port is None
    assert args.open_browser is True


def test_no_open_is_supported() -> None:
    assert _parser().parse_args(["--no-open"]).open_browser is False


def test_positional_commands_are_rejected() -> None:
    with pytest.raises(SystemExit) as stopped:
        _parser().parse_args(["unexpected-command"])

    assert stopped.value.code == 2


def test_version_exits_without_starting_the_server(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        main(["--version"])

    assert stopped.value.code == 0
    assert capsys.readouterr().out.startswith("bismuth ")
