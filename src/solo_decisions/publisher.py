"""Deliver outbox events to the platform graph (at-least-once, ordered per row).

Runs two ways: inline after a write (the API's own workspace, best effort),
and as a Railway cron sweep across every workspace (``decisions-publish``)
that retries what the inline attempt missed. The graph ignores duplicates.

    decisions-publish [--batch N]

Needs ``DECISIONS_PUBLISHER_TOKEN`` (a delegated automation token with
``graph.decisions:write``) and ``EMPIRE_PLATFORM_URL``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import psycopg
from psycopg.rows import dict_row

from ._sentry import capture

MAX_BACKOFF_SECONDS = 600


def token_workspace(token: str) -> str:
    """The workspace a publisher token is for (read only to choose rows; the platform verifies the token)."""
    # nosemgrep: python.jwt.security.unverified-jwt-decode.unverified-jwt-decode -- reads only the routing claim (ws) of our own publisher token to pick rows; the platform verifies the signature
    return str(uuid.UUID(jwt.decode(token, options={"verify_signature": False})["ws"]))


def publish_pending(
    conn: psycopg.Connection[Any], platform: Any, token: str, limit: int = 100
) -> dict[str, int]:
    """Publish due rows of the token's workspace; the batch is claimed and marked in one transaction.

    A publisher token is bound to one workspace, so rows of other workspaces wait for their own
    publisher. Rows are locked with SKIP LOCKED, so concurrent sweeps never publish the same row twice
    at once; the graph ignores a duplicate delivery after a crash.
    """
    conn.row_factory = dict_row
    rows = conn.execute(
        "SELECT id, workspace_id, type, subject, execution_id, classification, payload, occurred_at,"
        " correlation_id, attempts FROM decisions.outbox"
        " WHERE published_at IS NULL AND next_attempt_at <= now() AND workspace_id = %s"
        " ORDER BY occurred_at, id LIMIT %s FOR UPDATE SKIP LOCKED",
        (token_workspace(token), limit),
    ).fetchall()
    counts = {"published": 0, "failed": 0}
    reported = False  # one Sentry event per sweep, not one per failed row
    for row in rows:
        event = {
            "id": str(row["id"]),
            "workspace_id": str(row["workspace_id"]),
            "type": row["type"],
            "subject": row["subject"],
            "version": int(row["payload"].get("version", 1)),
            "classification": row["classification"],
            "payload": row["payload"],
            "execution_id": str(row["execution_id"]) if row["execution_id"] else None,
            "occurred_at": row["occurred_at"].isoformat(),
            "correlation_id": row["correlation_id"],
        }
        try:
            platform.publish(token, event)
        except Exception as exc:  # noqa: BLE001 - any publish failure is retried with backoff; only the error class is kept
            if not reported:
                capture(exc)
                reported = True
            delay = min(MAX_BACKOFF_SECONDS, 5 * 2 ** min(row["attempts"], 10))
            conn.execute(
                "UPDATE decisions.outbox SET attempts = attempts + 1, next_attempt_at = %s, last_error = %s"
                " WHERE id = %s",
                (
                    datetime.now(UTC) + timedelta(seconds=delay),
                    type(exc).__name__,
                    row["id"],
                ),
            )
            counts["failed"] += 1
        else:
            conn.execute(
                "UPDATE decisions.outbox SET published_at = now(), attempts = attempts + 1, last_error = NULL"
                " WHERE id = %s",
                (row["id"],),
            )
            counts["published"] += 1
    conn.commit()
    return counts


def main(argv: list[str] | None = None) -> int:
    from solo_decisions.db import default_cluster
    from solo_decisions.platform import HttpPlatform

    parser = argparse.ArgumentParser(
        prog="decisions-publish", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--batch", type=int, default=200)
    args = parser.parse_args(argv)
    token = os.environ.get("DECISIONS_PUBLISHER_TOKEN")
    url = os.environ.get("EMPIRE_PLATFORM_URL")
    if not token or not url:
        print("set DECISIONS_PUBLISHER_TOKEN and EMPIRE_PLATFORM_URL", file=sys.stderr)
        return 2
    with default_cluster().connect("decisions_publisher") as conn:
        counts = publish_pending(conn, HttpPlatform(url), token, limit=args.batch)
    print(json.dumps({"event": "decisions.publish", **counts}))
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
