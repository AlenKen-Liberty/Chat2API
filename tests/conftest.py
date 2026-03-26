import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "live: marks tests that make real API calls (deselect with '-m \"not live\"')")
