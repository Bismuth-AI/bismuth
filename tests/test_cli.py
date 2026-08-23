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


@pytest.mark.parametrize(
    "former_command",
    ["version", "doctor", "add", "scan", "tree", "replay", "status", "log", "undo", "serve"],
)
def test_former_subcommands_are_rejected(former_command: str) -> None:
    with pytest.raises(SystemExit) as stopped:
        _parser().parse_args([former_command])

    assert stopped.value.code == 2


def test_version_exits_without_starting_the_server(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        main(["--version"])

    assert stopped.value.code == 0
    assert capsys.readouterr().out.startswith("bismuth ")
