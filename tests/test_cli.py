from typer.testing import CliRunner

from svarog_harness import __version__
from svarog_harness.cli.main import app

runner = CliRunner()


def test_version_command() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "svarog" in result.output.lower()
