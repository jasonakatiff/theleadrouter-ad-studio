"""Read-only release diagnostics. Passing checks do not prove live Meta access."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

EXPECTED_REVISION = "feedback_mirror_20261029"
REQUIRED_TABLES = frozenset(
    {
        "users",
        'analytics_audit',
        'analytics_settings',
        'analytics_sources',
        'analytics_accounts',
        'analytics_ads',
        'analytics_staged_rows',
        'analytics_insights',

        "api_keys",
        "telemetry_events",
        "creative_assets",
        "creative_events",
        "delivery_api_requests",
        "delivery_api_cooldowns",
        "delivery_provider_cache",
        "delivery_staged_insights",
        "delivery_settings",
        "delivery_jobs",
        "delivery_post_attempts",
        "delivery_notifications",
        "managed_ads",
        "delivery_syncs",
        "ad_insights",

        "plugin_installations",
        "plugin_runs",
        "user_themes",
        "leadrouter_connections",
        "leadrouter_defaults",
        "meta_ads_connections",
        "workspaces",
        "workspace_memberships",
        "workspace_accounts",
        "workspace_account_grants",
        "account_sync_jobs",
        "account_snapshots",
        "workspace_audit_events",
        "alembic_version",
    }
)
REQUIRED_API = {
    "/api/v1/analytics/settings": {"get"},
    "/api/v1/analytics/patterns": {"get"},
    "/api/v1/analytics/report": {"get"},
    "/api/v1/analytics/sources": {"get", "post"},
    "/api/v1/creatives": {"get"},
    "/api/v1/creatives/uploads": {"post"},
    "/api/v1/delivery/settings": {"get", "put"},
    "/api/v1/delivery/jobs": {"get"},
    "/api/v1/delivery/jobs/{job_id}/retry": {"post"},
    "/api/v1/delivery/notifications": {"get"},
    "/api/v1/delivery/launches": {"post"},
    "/api/v1/delivery/syncs": {"get"},
    "/api/v1/delivery/report": {"get"},

    "/api/v1/plugins": {"get", "post"},
    "/api/v1/plugins/{plugin_id}": {"get", "patch", "delete"},
    "/api/v1/plugins/{plugin_id}/runs": {"post"},
    "/api/v1/plugins/runs/{run_id}": {"get"},
    "/api/v1/plugin-worker/jobs": {"get"},
    "/api/v1/plugin-worker/jobs/{run_id}/claim": {"post"},
    "/api/v1/plugin-worker/jobs/{run_id}/result": {"post"},
    "/api/v1/telemetry/capabilities": {"get"},
    "/api/v1/telemetry/health": {"get"},
    "/api/v1/telemetry/events": {"get"},
    "/api/v1/telemetry/errors": {"get"},
    "/api/v1/telemetry/metrics": {"get"},
    "/api/v1/telemetry/traces/{trace_id}": {"get"},
    "/api/v1/telemetry/feedback": {"post"},
    "/api/v1/telemetry/client-events": {"post"},
    "/api/v1/leadrouter/connection": {"get", "put", "delete"},
    "/api/v1/leadrouter/campaigns": {"get"},
    "/api/v1/api-keys": {"get", "post"},
    "/api/v1/api-keys/{key_id}": {"patch", "delete"},
    "/api/v1/themes": {"get", "post"},
    "/api/v1/themes/import-github": {"post"},
    "/api/v1/themes/{theme_id}/refresh-github": {"post"},
    "/api/v1/help/docs": {"get"},
    "/api/v1/help/download": {"get"},
    "/api/v1/facebook/oauth/start": {"get"},
    "/api/v1/facebook/connection": {"get"},
    "/api/v2/meta-connections": {"get"},
    "/api/v2/workspaces": {"get", "post"},
    "/api/v2/workspaces/{workspace_id}/connections": {"get", "post"},
    "/api/v2/workspaces/{workspace_id}/connections/{account_id}": {"put"},
    "/api/v2/workspaces/{workspace_id}/members": {"get"},
    "/api/v2/workspaces/{workspace_id}/member-candidates": {"get"},
    "/api/v2/workspaces/{workspace_id}/managed-connections": {"get"},
    "/api/v2/workspaces/{workspace_id}/members/{user_id}": {"put"},
    "/api/v2/workspaces/{workspace_id}/accounts/{account_id}/grants": {"get"},
    "/api/v2/workspaces/{workspace_id}/accounts/{account_id}/grants/{user_id}": {"put"},
    "/api/v2/accounts/{account_id}/snapshot": {"get"},
    "/api/v2/accounts/{account_id}/sync": {"post"},
    "/api/v2/sync-jobs/{job_id}": {"get"},
}
REQUEST_TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def origin(value):
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Invalid origin URL.") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or (
            parsed.scheme == "http"
            and parsed.hostname not in {"localhost", "127.0.0.1"}
        )
    ):
        raise argparse.ArgumentTypeError(
            "Use an HTTPS origin (or localhost HTTP) without credentials, paths or query strings."
        )
    return value.rstrip("/")


def read_url(url, *, json_document=True):
    request = Request(
        url, method="GET", headers={"User-Agent": "Breadwinner-release-check"}
    )
    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        if response.status != 200:
            raise ValueError("Unexpected HTTP status")
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("Response exceeds diagnostic limit")
    return json.loads(body) if json_document else body.decode("utf-8")


def public_checks(backend_url, frontend_url=None, *, fetch=read_url):
    backend_url = origin(backend_url)
    checks = []
    try:
        health = fetch(backend_url + "/health")
        checks.append(
            {
                "name": "backend_health",
                "passed": isinstance(health, dict)
                and health.get("status") == "healthy",
            }
        )
    except Exception:
        checks.append(
            {
                "name": "backend_health",
                "passed": False,
                "error": "Health request failed.",
            }
        )
    try:
        document = fetch(backend_url + "/api/v1/openapi.json")
        paths = document.get("paths") if isinstance(document, dict) else None
        if not isinstance(paths, dict):
            raise ValueError("Invalid API document")
        missing = [
            f"{method.upper()} {path}"
            for path, methods in REQUIRED_API.items()
            for method in sorted(methods)
            if not isinstance(paths.get(path), dict) or method not in paths[path]
        ]
        checks.append(
            {"name": "api_contract", "passed": not missing, "missing": missing}
        )
    except Exception:
        checks.append(
            {
                "name": "api_contract",
                "passed": False,
                "error": "API document request or validation failed.",
            }
        )
    if frontend_url:
        try:
            html = fetch(origin(frontend_url) + "/", json_document=False)
            checks.append(
                {
                    "name": "frontend_available",
                    "passed": isinstance(html, str) and "<html" in html.lower(),
                }
            )
        except Exception:
            checks.append(
                {
                    "name": "frontend_available",
                    "passed": False,
                    "error": "Frontend request failed.",
                }
            )
    return checks


def check_database(url, *, connect=None):
    result = {"name": "database_schema", "passed": False}
    engine = None
    try:
        from sqlalchemy import create_engine, inspect, text

        factory = connect or (
            lambda value: create_engine(
                value, connect_args={"connect_timeout": REQUEST_TIMEOUT_SECONDS}
            )
        )
        engine = factory(url)
        with engine.connect() as connection:
            with connection.begin():
                connection.execute(text("SET TRANSACTION READ ONLY"))
                result["read_only"] = (
                    connection.execute(text("SHOW transaction_read_only")).scalar_one()
                    == "on"
                )
                tables = set(inspect(connection).get_table_names())
                result["missing_tables"] = sorted(REQUIRED_TABLES - tables)
                result["revision"] = (
                    sorted(
                        connection.execute(
                            text("SELECT version_num FROM alembic_version")
                        ).scalars()
                    )
                    if "alembic_version" in tables
                    else []
                )
                result["passed"] = (
                    result["read_only"]
                    and not result["missing_tables"]
                    and result["revision"] == [EXPECTED_REVISION]
                )
    except Exception:
        result = {
            "name": "database_schema",
            "passed": False,
            "error": "Database connection or schema check failed; verify access and migration state.",
        }
    finally:
        if engine is not None:
            engine.dispose()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-url", required=True, type=origin)
    parser.add_argument("--frontend-url", type=origin)
    parser.add_argument(
        "--check-database",
        action="store_true",
        help="Read DATABASE_URL from the process environment and inspect it in a read-only transaction.",
    )
    args = parser.parse_args(argv)
    checks = public_checks(args.backend_url, args.frontend_url)
    if args.check_database:
        database_url = os.getenv("DATABASE_URL")
        checks.append(
            check_database(database_url)
            if database_url
            else {
                "name": "database_schema",
                "passed": False,
                "error": "DATABASE_URL is missing from the process environment.",
            }
        )
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": "passed" if all(row["passed"] for row in checks) else "blocked",
        "read_only": True,
        "checks": checks,
        "unverified": [
            "Railway source and deployed revision",
            "Worker deployment and job completion",
            "Pilot account ownership and effective grants",
            "Live Meta validity",
            "Authenticated production UI",
        ],
    }
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
