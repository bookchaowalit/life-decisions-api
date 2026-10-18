"""Error reporting to Sentry.

A no-op unless SENTRY_DSN is set, so tests and local runs never send anything. Imported by the
package ``__init__``, so every entry point (API, publisher, DB tool, MCP server) reports errors.
"""

from __future__ import annotations

import os
import sys


def init_sentry() -> None:
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        return
    import sentry_sdk

    sentry_sdk.init(
        dsn=dsn,
        environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
        release=os.environ.get("RAILWAY_GIT_COMMIT_SHA")
        or os.environ.get("SENTRY_RELEASE")
        or None,
        traces_sample_rate=0.0,  # errors only: the free plan's quota is for errors
        send_default_pii=False,
    )
    service = os.environ.get("RAILWAY_SERVICE_NAME", "").strip()
    if service:
        sentry_sdk.set_tag("service", service)
    print(
        f"sentry: enabled for {service or 'unknown service'}",
        file=sys.stderr,
        flush=True,
    )
