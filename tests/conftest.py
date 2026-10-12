import pytest


@pytest.fixture(autouse=True)
def disable_weather_network(monkeypatch):
    monkeypatch.setenv("OFFICE_TERMINAL_WEATHER_ENABLED", "0")
