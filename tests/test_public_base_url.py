"""Every recovery message links to PUBLIC_BASE_URL.

A deployed instance that never had it set produced messages linking to
http://localhost:5173 -- delivered, recorded as sent, and completely useless to
whoever received them. These pin the resolution order that prevents it.
"""

import importlib

import pytest


def _config_with(env: dict, monkeypatch):
    """Import app.config against a chosen environment.

    load_dotenv is neutralised: otherwise the developer's own .env decides the
    outcome of the test, which is precisely the confusion being tested.
    """
    import app.config as config

    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None, raising=False)
    for key in ("PUBLIC_BASE_URL", "RENDER_EXTERNAL_URL"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    return importlib.reload(config)


@pytest.fixture(autouse=True)
def _restore():
    yield
    import app.config

    importlib.reload(app.config)


class TestResolutionOrder:
    def test_an_explicit_setting_wins(self, monkeypatch):
        config = _config_with(
            {
                "PUBLIC_BASE_URL": "https://revive.example.com/",
                "RENDER_EXTERNAL_URL": "https://ignored.onrender.com",
            },
            monkeypatch,
        )
        assert config.PUBLIC_BASE_URL == "https://revive.example.com"
        assert config.PUBLIC_BASE_URL_IS_LOCAL is False

    def test_a_render_deployment_falls_back_to_its_own_url(self, monkeypatch):
        """The deployed case: nobody set PUBLIC_BASE_URL, and the platform
        supplies its own public address. Better a working link on the API's
        domain than a localhost one."""
        config = _config_with(
            {"RENDER_EXTERNAL_URL": "https://revive-revenue.onrender.com"}, monkeypatch
        )
        assert config.PUBLIC_BASE_URL == "https://revive-revenue.onrender.com"
        assert config.PUBLIC_BASE_URL_IS_LOCAL is False

    def test_local_development_still_points_locally(self, monkeypatch):
        config = _config_with({}, monkeypatch)
        assert config.PUBLIC_BASE_URL == "http://localhost:5173"
        assert config.PUBLIC_BASE_URL_IS_LOCAL is True
