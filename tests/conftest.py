"""Keep cross-project regressions explicitly opt-in."""
import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-cross-project", action="store_true", default=False,
        help="Run integration tests against CODE_EVAL_ROOT (default: sibling code-eval)",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-cross-project"):
        return
    disabled = pytest.mark.skip(reason="Cross-project integration: enable --run-cross-project")
    for item in items:
        if item.get_closest_marker("cross_project"):
            item.add_marker(disabled)
