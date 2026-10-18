import importlib
import sys

import pytest

from solo_decisions import _sentry


def test_no_dsn_is_a_noop(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    calls = []
    monkeypatch.setitem(
        sys.modules,
        "sentry_sdk",
        type("Fake", (), {"init": lambda **kw: calls.append(kw)}),
    )
    _sentry.init_sentry()
    assert calls == []


def test_dsn_initialises_errors_only_without_pii(monkeypatch, capsys):
    calls, tags = [], {}

    class Fake:
        @staticmethod
        def init(**kw):
            calls.append(kw)

        @staticmethod
        def set_tag(key, value):
            tags[key] = value

    monkeypatch.setitem(sys.modules, "sentry_sdk", Fake)
    monkeypatch.setenv("SENTRY_DSN", "https://public@o0.ingest.sentry.io/0")
    monkeypatch.setenv("RAILWAY_SERVICE_NAME", "decisions-api")
    monkeypatch.delenv("SENTRY_ENVIRONMENT", raising=False)
    _sentry.init_sentry()
    assert calls[0]["environment"] == "production"
    assert calls[0]["traces_sample_rate"] == 0.0
    assert calls[0]["send_default_pii"] is False
    assert tags == {"service": "decisions-api"}
    assert "sentry: enabled for decisions-api" in capsys.readouterr().err


def test_real_sdk_installed():
    pytest.importorskip("sentry_sdk")
    assert importlib.import_module("sentry_sdk")
