"""Database access: a local throwaway cluster for tests, or the hosted Neon database.

Roles: ``decisions_migrator`` (owns the schema, runs migrations),
``decisions_api`` (runtime, RLS-bound), ``decisions_publisher`` (outbox sweep).

Hosted connection URLs come from ``DECISIONS_DB_URL_<ROLE>`` (Railway
variables) or the owner-only file ``~/.config/solo-decisions/db.env``; nothing
secret is printed or stored in the repository.

    decisions-db migrate        apply pending migrations as the migrator
    decisions-db provision      create the Neon project and roles (NEON_API_KEY), then migrate
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.client import HTTPMessage
from pathlib import Path
from typing import IO, Any

import psycopg
from psycopg import sql

REPO_ROOT = Path(__file__).resolve().parents[2]
# The image installs the package (site-packages) and copies migrations/ and db/ to /app.
APP_ROOT = Path(
    os.environ.get("DECISIONS_APP_ROOT")
    or (REPO_ROOT if (REPO_ROOT / "migrations").is_dir() else "/app")
)
MIGRATIONS = APP_ROOT / "migrations"
ROLES_SQL = APP_ROOT / "db" / "roles.sql"
DATABASE = "decisions"
SUPERUSER = "postgres"
MIGRATOR = "decisions_migrator"
RUNTIME_ROLES = ("decisions_api", "decisions_publisher")
PORT = 5433
SECRETS = Path(
    os.environ.get(
        "DECISIONS_DB_SECRETS", Path.home() / ".config" / "solo-decisions" / "db.env"
    )
)


def pg_bin_dir() -> Path:
    override = os.environ.get("DECISIONS_PG_BIN")
    if override:
        return Path(override)
    try:
        import pgserver

        bundled = Path(pgserver.__file__).parent / "pginstall" / "bin"
        if (bundled / "pg_ctl").exists():
            return bundled
    except ImportError:
        pass
    found = shutil.which("pg_ctl")
    if found:
        return Path(found).parent
    raise SystemExit(
        "PostgreSQL binaries not found; install the dev extra or set DECISIONS_PG_BIN"
    )


@dataclass
class LocalCluster:
    """A throwaway PostgreSQL for tests (trust auth over a private socket)."""

    data_dir: Path
    socket_dir: Path

    def _run(self, *args: str) -> None:
        subprocess.run(
            [str(pg_bin_dir() / args[0]), *args[1:]],
            check=True,
            capture_output=True,
            text=True,
        )

    def init(self) -> None:
        self._run(
            "initdb",
            "-D",
            str(self.data_dir),
            "-U",
            SUPERUSER,
            "--auth=trust",
            "--encoding=UTF8",
            "--locale=C",
        )

    def start(self) -> None:
        self.socket_dir.mkdir(parents=True, exist_ok=True)
        options = f"-c listen_addresses='' -k {self.socket_dir} -p {PORT}"
        self._run(
            "pg_ctl",
            "-D",
            str(self.data_dir),
            "-o",
            options,
            "-w",
            "-l",
            str(self.data_dir / "server.log"),
            "start",
        )

    def stop(self) -> None:
        if (self.data_dir / "postmaster.pid").exists():
            self._run("pg_ctl", "-D", str(self.data_dir), "-m", "fast", "-w", "stop")

    def connect(
        self, role: str, dbname: str = DATABASE, **kwargs: Any
    ) -> psycopg.Connection[Any]:
        return psycopg.connect(
            f"host={self.socket_dir} port={PORT} user={role} dbname={dbname}", **kwargs
        )

    def bootstrap(self) -> None:
        with self.connect(SUPERUSER, "postgres", autocommit=True) as conn:
            conn.execute(ROLES_SQL.read_text(encoding="utf-8"))
            if not conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (DATABASE,)
            ).fetchone():
                # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- psycopg sql.SQL composition: names are sql.Identifier, the password is sql.Literal
                conn.execute(
                    sql.SQL("CREATE DATABASE {} OWNER {}").format(
                        sql.Identifier(DATABASE), sql.Identifier(MIGRATOR)
                    )
                )
            # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- psycopg sql.SQL composition: names are sql.Identifier, the password is sql.Literal
            conn.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(DATABASE),
                    sql.SQL(", ").join(map(sql.Identifier, RUNTIME_ROLES)),
                )
            )


@dataclass
class HostedCluster:
    """Neon: one URL per role, from the environment or the owner-only secrets file."""

    urls: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, secrets_file: Path = SECRETS) -> HostedCluster:
        urls: dict[str, str] = {}
        if secrets_file.is_file():
            if secrets_file.stat().st_mode & 0o077:
                raise SystemExit(
                    f"{secrets_file} must be readable by its owner only (chmod 600)"
                )
            for line in secrets_file.read_text(encoding="utf-8").splitlines():
                key, sep, value = line.strip().partition("=")
                if sep and key.startswith("DECISIONS_DB_URL_"):
                    urls[key.removeprefix("DECISIONS_DB_URL_").lower()] = value.strip()
        for key, value in os.environ.items():
            if key.startswith("DECISIONS_DB_URL_") and value:
                urls[key.removeprefix("DECISIONS_DB_URL_").lower()] = value
        return cls(urls)

    def connect(
        self, role: str, dbname: str = DATABASE, **kwargs: Any
    ) -> psycopg.Connection[Any]:
        key = {
            MIGRATOR: "migrator",
            "decisions_api": "api",
            "decisions_publisher": "publisher",
        }.get(role, role)
        if key not in self.urls:
            raise SystemExit(
                f"no connection URL for {role}: set DECISIONS_DB_URL_{key.upper()}"
            )
        # Short connect timeout; Neon wakes a suspended compute within a few seconds.
        return psycopg.connect(self.urls[key], connect_timeout=15, **kwargs)


def default_cluster() -> LocalCluster | HostedCluster:
    if os.environ.get("DECISIONS_DB_MODE") == "hosted":
        return HostedCluster.from_env()
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir())
    return LocalCluster(REPO_ROOT / ".runtime" / "pgdata", runtime / "solo-decisions")


def migrate(cluster: LocalCluster | HostedCluster) -> list[str]:
    """Apply pending migrations as the migrator; refuse edited ones."""
    applied: list[str] = []
    with cluster.connect(MIGRATOR) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS public.schema_migrations ("
            " name text PRIMARY KEY, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        conn.commit()
        done = dict(
            conn.execute("SELECT name, sha256 FROM public.schema_migrations").fetchall()
        )
        files = sorted(MIGRATIONS.glob("*.sql"))
        if not files:
            raise SystemExit(
                f"no migrations found in {MIGRATIONS}; set DECISIONS_APP_ROOT"
            )
        for path in files:
            text = path.read_text(encoding="utf-8")
            digest = hashlib.sha256(text.encode()).hexdigest()
            if path.name in done:
                if done[path.name] != digest:
                    raise SystemExit(f"applied migration was edited: {path.name}")
                continue
            with conn.transaction():
                conn.execute(text)
                conn.execute(
                    "INSERT INTO public.schema_migrations (name, sha256) VALUES (%s, %s)",
                    (path.name, digest),
                )
            applied.append(path.name)
    return applied


# --- Neon provisioning -------------------------------------------------------------------------

NEON_API = "https://console.neon.tech/api/v2"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:  # never forward the API key
        return None


def _neon(
    key: str, method: str, path: str, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{NEON_API}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    opener = urllib.request.build_opener(_NoRedirect)
    for attempt in range(15):
        try:
            with opener.open(request, timeout=30) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as error:
            if (
                error.code == 423 and attempt < 14
            ):  # a running operation holds the project
                time.sleep(2)
                continue
            raise SystemExit(
                f"neon {method} {path.split('?')[0]}: {error.code}"
            ) from None
    raise SystemExit("neon project stayed locked")


def _url(host: str, role: str, password: str) -> str:
    return (
        f"postgresql://{urllib.parse.quote(role)}:{urllib.parse.quote(password, safe='')}@{host}/{DATABASE}"
        "?sslmode=require&channel_binding=require"
    )


def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def provision(project_name: str, region: str) -> str:
    """Create or reuse the Neon project, database and roles; write URLs owner-only. Returns the project id."""
    key = os.environ.get("NEON_API_KEY")
    if not key:
        raise SystemExit("NEON_API_KEY is not set (inject it from the secret store)")
    found = [
        p
        for p in _neon(
            key, "GET", f"/projects?search={urllib.parse.quote(project_name)}"
        ).get("projects", [])
        if p["name"] == project_name
    ]
    if found:
        project = str(found[0]["id"])
    else:
        created = _neon(
            key,
            "POST",
            "/projects",
            {"project": {"name": project_name, "region_id": region, "pg_version": 16}},
        )
        project = str(created["project"]["id"])
        time.sleep(5)
    branch = next(
        b
        for b in _neon(key, "GET", f"/projects/{project}/branches")["branches"]
        if b.get("default") or b.get("primary")
    )["id"]
    host = next(
        e
        for e in _neon(key, "GET", f"/projects/{project}/endpoints")["endpoints"]
        if e["branch_id"] == branch and e["type"] == "read_write"
    )["host"]
    base = f"/projects/{project}/branches/{branch}"
    if MIGRATOR not in {r["name"] for r in _neon(key, "GET", f"{base}/roles")["roles"]}:
        _neon(key, "POST", f"{base}/roles", {"role": {"name": MIGRATOR}})
        time.sleep(3)
    password = _neon(key, "GET", f"{base}/roles/{MIGRATOR}/reveal_password")["password"]
    if DATABASE not in {
        d["name"] for d in _neon(key, "GET", f"{base}/databases")["databases"]
    }:
        _neon(
            key,
            "POST",
            f"{base}/databases",
            {"database": {"name": DATABASE, "owner_name": MIGRATOR}},
        )
        time.sleep(3)
    existing = HostedCluster.from_env().urls
    urls = {"migrator": _url(host, MIGRATOR, password)}
    with psycopg.connect(urls["migrator"], autocommit=True, connect_timeout=30) as conn:
        for role in RUNTIME_ROLES:
            short = role.removeprefix("decisions_")
            known = existing.get(short)
            role_password = (
                urllib.parse.unquote(urllib.parse.urlsplit(known).password or "")
                if known
                else ""
            )
            role_password = role_password or secrets.token_urlsafe(32)
            present = conn.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
            ).fetchone()
            # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- psycopg sql.SQL composition: names are sql.Identifier, the password is sql.Literal
            conn.execute(
                sql.SQL(
                    "{} ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT PASSWORD {}"
                ).format(
                    sql.SQL("ALTER" if present else "CREATE"),
                    sql.Identifier(role),
                    sql.Literal(role_password),
                )
            )
            urls[short] = _url(host, role, role_password)
        # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- psycopg sql.SQL composition: names are sql.Identifier, the password is sql.Literal
        conn.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
                sql.Identifier(DATABASE)
            )
        )
        # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- psycopg sql.SQL composition: names are sql.Identifier, the password is sql.Literal
        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(DATABASE),
                sql.SQL(", ").join(map(sql.Identifier, RUNTIME_ROLES)),
            )
        )
        conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    lines = ["# solo-decisions hosted database; owner-only, never commit or print\n"]
    lines += [
        f"DECISIONS_DB_URL_{name.upper()}={value}\n"
        for name, value in sorted(urls.items())
    ]
    _write_private(SECRETS, "".join(lines))
    migrate(HostedCluster(urls))
    return project


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="decisions-db", description=__doc__.splitlines()[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    prov = sub.add_parser("provision")
    prov.add_argument("--project", default="solo-decisions")
    prov.add_argument("--region", default="aws-ap-southeast-1")
    args = parser.parse_args(argv)
    if args.command == "provision":
        project = provision(args.project, args.region)
        print(f"neon project {project} ready; URLs in {SECRETS} (owner-only)")
        return 0
    applied = migrate(default_cluster())
    print(f"{len(applied)} migrations applied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
