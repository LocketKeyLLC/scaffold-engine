"""Throwaway: exercise the full PR CI pipeline on a fresh PR (§17.589 / #102
pull-path verification). This file's PR is closed unmerged — delete if it lands.
"""
import pytest


@pytest.mark.smoke
def test_ci_pipeline_alive():
    assert True
