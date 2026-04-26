import cerebro
from cerebro.cli import main


def test_package_imports() -> None:
    assert cerebro.__version__


def test_cli_runs() -> None:
    assert main([]) == 0
