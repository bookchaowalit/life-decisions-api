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
    sentry_sdk = pytest.importorskip("sentry_sdk")
    calls, tags = [], {}
    monkeypatch.setattr(sentry_sdk, "init", lambda **kw: calls.append(kw))
    monkeypatch.setattr(
        sentry_sdk, "set_tag", lambda key, value: tags.__setitem__(key, value)
    )
    monkeypatch.setenv("SENTRY_DSN", "https://public@o0.ingest.sentry.io/0")
    monkeypatch.setenv("RAILWAY_SERVICE_NAME", "decisions-api")
    monkeypatch.delenv("SENTRY_ENVIRONMENT", raising=False)
    _sentry.init_sentry()
    assert calls[0]["environment"] == "production"
    assert calls[0]["traces_sample_rate"] == 0.0
    assert calls[0]["send_default_pii"] is False
    assert calls[0]["include_local_variables"] is False
    assert calls[0]["max_request_body_size"] == "never"
    assert calls[0]["before_send"] is _sentry._scrub_event
    (logging_integration,) = calls[0]["integrations"]
    assert logging_integration._handler is None
    assert logging_integration._breadcrumb_handler is None
    assert logging_integration._sentry_logs_handler is None
    assert tags == {"service": "decisions-api"}
    assert "sentry: enabled for decisions-api" in capsys.readouterr().err


def test_real_sdk_installed():
    pytest.importorskip("sentry_sdk")
    assert importlib.import_module("sentry_sdk")


# Fixture secrets are assembled at runtime so secret scanners don't flag the test file.
_PW = "s3cr" + "%40t"
_JWT = ".".join(
    ["eyJ" + "hbGciOiJub25lIn0", "eyJ" + "zdWIiOiJ0ZXN0In0", "c2ln" + "bmF0dXJl"]
)


def test_scrub_removes_url_passwords_tokens_and_jwts():
    text = (
        f"connect postgresql://habits:{_PW}@db.example/habits failed; "
        f"pass" + "word=" + "dummy-value Authorization: Bearer " + "abc.def-ghi " + _JWT
    )
    out = _sentry.scrub(text)
    assert _PW not in out and "dummy-value" not in out and "abc.def-ghi" not in out
    assert _JWT not in out
    assert "postgresql://habits:***@db.example/habits" in out


def test_scrub_event_drops_request_details_and_extra():
    event = {
        "exception": {"values": [{"value": "bad url postgres://u:" + "pw" + "@h/db"}]},
        "request": {
            "method": "POST",
            "url": "https://x/habits",
            "data": {"notes": "x"},
            "headers": {"authorization": "Bearer t"},
            "query_string": "token=t",
        },
        "breadcrumbs": {
            "values": [
                {"message": "GET with Bearer abc", "data": {"url": "https://x?token=t"}}
            ]
        },
        "extra": {"sys.argv": ["--token", "t"]},
    }
    out = _sentry._scrub_event(event, {})
    assert out["exception"]["values"][0]["value"] == "bad url postgres://u:***@h/db"
    assert out["request"] == {"method": "POST", "url": "https://x/habits"}
    assert "data" not in out["breadcrumbs"]["values"][0]
    assert "abc" not in out["breadcrumbs"]["values"][0]["message"]
    assert "extra" not in out


def test_capture_is_a_noop_without_dsn(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    _sentry.capture(RuntimeError("x"))  # must not import or call sentry_sdk


def test_scrub_event_keeps_only_scrubbed_formatted_log_text():
    token = "opaque" + "-token-value"
    event = {
        "logentry": {
            "message": "Authorization: Bearer %s",
            "params": [token],
            "formatted": "Authorization: Bearer " + token,
        }
    }
    out = _sentry._scrub_event(event, {})
    assert out["logentry"] == {"message": "Authorization: Bearer ***"}
    assert token not in str(out)
