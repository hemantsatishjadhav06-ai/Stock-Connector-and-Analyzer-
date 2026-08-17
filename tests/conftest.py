import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from equity_analyst.config import RunConfig  # noqa: E402
from equity_analyst.pipeline import run  # noqa: E402

SAMPLE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "samples", "demo_industrials.json",
)


@pytest.fixture(scope="session")
def sample_path():
    if not os.path.exists(SAMPLE):
        pytest.skip("sample bundle not generated; run tools/make_sample_bundle.py")
    return SAMPLE


@pytest.fixture(scope="session")
def report(sample_path):
    """One full offline run, reused across the suite."""
    config = RunConfig(
        ticker="DEMO.SYNTH",
        market="US",
        offline_file=sample_path,
        allow_network=False,
        commodities=["CL=F"],
    )
    return run(config)


@pytest.fixture(scope="session")
def report_signals(sample_path):
    """A full offline run with §S signals and backtest enabled."""
    config = RunConfig(
        ticker="DEMO.SYNTH",
        market="US",
        offline_file=sample_path,
        allow_network=False,
        commodities=["CL=F"],
        with_signals=True,
        with_backtest=True,
    )
    return run(config)
