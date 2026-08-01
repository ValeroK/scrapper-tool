"""Unit tests for ``scrapper_tool.cli`` — the subcommand dispatcher.

Covers:
- Every registered command appears in the parser and dispatches to its module.
- ``canary.main`` still forwards, so editable installs and the documented
  public entry point keep working after the dispatcher moved.
- The pinned argparse behaviours survive the move: ``--help`` exits 0, no
  subcommand exits 2, and a handler's ``parser.error(...)`` exits 2.

The last one is the reason ``run_cli`` takes ``parser`` as an argument rather
than capturing it: argument validation that argparse can't express
declaratively still has to exit 2, and only ``parser.error`` does that.
"""

from __future__ import annotations

import pytest

from scrapper_tool import canary as canary_module
from scrapper_tool import cli as cli_module


class TestParser:
    def test_registers_every_declared_command(self) -> None:
        parser = cli_module._build_parser()
        args = parser.parse_args(["doctor", "--json"])
        assert args.command == "doctor"
        assert args.json is True

    def test_canary_subcommand_still_parses_its_flags(self) -> None:
        parser = cli_module._build_parser()
        args = parser.parse_args(["canary", "https://example.test/x", "--json"])
        assert args.command == "canary"
        assert args.url == "https://example.test/x"

    def test_every_command_module_satisfies_the_handler_contract(self) -> None:
        for name, module in cli_module._COMMANDS.items():
            assert callable(module.add_subparser), name
            assert callable(module.run_cli), name

    def test_help_exits_zero(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            cli_module.main(["--help"])
        assert excinfo.value.code == 0

    def test_no_subcommand_exits_two(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            cli_module.main([])
        assert excinfo.value.code == 2

    def test_unknown_subcommand_exits_two(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            cli_module.main(["definitely-not-a-command"])
        assert excinfo.value.code == 2


class TestDispatch:
    def test_calls_the_matching_handler_with_args_and_parser(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, object] = {}

        def _fake_run_cli(args: object, parser: object) -> int:
            seen["args"] = args
            seen["parser"] = parser
            return 7

        monkeypatch.setattr(cli_module._COMMANDS["doctor"], "run_cli", _fake_run_cli)
        assert cli_module.main(["doctor"]) == 7
        assert seen["parser"] is not None

    def test_handler_parser_error_exits_two(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The pinned --profiles behaviour: validation inside a handler still exits 2."""

        def _erroring(args: object, parser: object) -> int:
            parser.error("nope")  # type: ignore[attr-defined]
            return 0  # pragma: no cover

        monkeypatch.setattr(cli_module._COMMANDS["doctor"], "run_cli", _erroring)
        with pytest.raises(SystemExit) as excinfo:
            cli_module.main(["doctor"])
        assert excinfo.value.code == 2


class TestCanaryShim:
    def test_canary_main_forwards_to_the_dispatcher(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Editable installs point `scrapper-tool` here until reinstalled."""
        called: dict[str, object] = {}

        def _fake_cli_main(argv: object = None) -> int:
            called["argv"] = argv
            return 3

        monkeypatch.setattr(cli_module, "main", _fake_cli_main)
        assert canary_module.main(["canary", "https://example.test/x"]) == 3
        assert called["argv"] == ["canary", "https://example.test/x"]

    def test_canary_still_exports_its_public_names(self) -> None:
        for name in ("main", "run_canary", "probe_profile", "add_subparser", "run_cli"):
            assert name in canary_module.__all__, name
