"""M1b additive migration in a dedicated, disposable local PostgreSQL database."""

import importlib.util
import os
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

TABLES = {
    "workspaces",
    "workspace_memberships",
    "workspace_accounts",
    "workspace_account_grants",
    "account_sync_jobs",
    "account_snapshots",

    "workspace_audit_events",
}


def test_additive_workspace_migration_preserves_unmapped_legacy_rows():
    url = os.environ["TEST_WORKSPACE_MIGRATION_URL"]
    parsed = urlparse(url)
    assert parsed.hostname in ("localhost", "127.0.0.1") and parsed.path.startswith(
        "/test_"
    )
    path = (
        Path(__file__).resolve().parents[2]
        / "alembic/versions/bw_workspace_001_access_sync.py"
    )
    assert path.exists(), "Additive workspace/sync migration is missing"
    spec = importlib.util.spec_from_file_location("test_workspace_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine(url)
    schema = "test_migration_" + uuid4().hex
    with engine.connect() as db:
        transaction = db.begin()
        try:
            db.execute(text(f"CREATE SCHEMA {schema}"))
            db.execute(text(f"SET LOCAL search_path TO {schema}"))
            db.execute(text("CREATE TABLE users (id varchar PRIMARY KEY)"))
            db.execute(
                text("CREATE TABLE meta_ads_connections (id varchar PRIMARY KEY)")
            )
            db.execute(
                text(
                    "CREATE TABLE brands (id varchar PRIMARY KEY, name varchar NOT NULL)"
                )
            )
            db.execute(
                text(
                    "INSERT INTO brands VALUES ('test-legacy-brand', 'test-unmapped-brand')"
                )
            )
            with Operations.context(MigrationContext.configure(db)):
                module.upgrade()
            assert TABLES <= set(inspect(db).get_table_names(schema=schema))
            assert (
                db.execute(text("SELECT name FROM brands")).scalar_one()
                == "test-unmapped-brand"
            )
            assert db.execute(text("SELECT count(*) FROM workspaces")).scalar_one() == 0
            assert not any(
                column["name"] == "workspace_id"
                for column in inspect(db).get_columns("brands", schema=schema)
            )
            assert module.down_revision == "bw_feedback_001"
            from app.models import Base

            for name in TABLES:
                actual = {
                    column["name"]: column["nullable"]
                    for column in inspect(db).get_columns(name, schema=schema)
                }
                expected = {
                    column.name: column.nullable
                    for column in Base.metadata.tables[name].columns if column.name != "available_at"
                }
                assert actual == expected, name
            active_index = next(
                index
                for index in inspect(db).get_indexes("account_sync_jobs", schema=schema)
                if index["name"] == "uq_active_account_sync"
            )
            assert (
                active_index["unique"]
                and "postgresql_where" in active_index["dialect_options"]
            )
            assert (
                len(
                    inspect(db).get_foreign_keys(
                        "workspace_account_grants", schema=schema
                    )
                )
                == 2
            )
        finally:
            transaction.rollback()
    engine.dispose()


def test_alembic_upgrades_old_schema_and_repeating_head_is_safe():
    import subprocess
    import sys
    from app.models import Base
    from sqlalchemy.engine import make_url

    url = os.environ["TEST_WORKSPACE_MIGRATION_URL"]
    parsed = urlparse(url)
    assert parsed.hostname in ("localhost", "127.0.0.1") and parsed.path.startswith(
        "/test_"
    )
    engine = create_engine(url)
    schema = "test_upgrade_" + uuid4().hex
    with engine.begin() as db:
        db.execute(text(f"CREATE SCHEMA {schema}"))
    scoped_url = make_url(url).update_query_dict({"options": "-csearch_path=" + schema})
    scoped = create_engine(scoped_url)
    try:
        Base.metadata.create_all(
            scoped,
            tables=[
                table
                for table in Base.metadata.sorted_tables
                if not table.name.startswith("analytics_") and table.name not in TABLES | {"installation_state", "provider_connections", "telemetry_events", "plugin_installations", "plugin_runs", "delivery_settings", "delivery_jobs", "managed_ads", "delivery_syncs", "ad_insights", "creative_assets", "creative_events", "delivery_api_requests", "delivery_api_cooldowns", "delivery_provider_cache", "delivery_staged_insights", "delivery_post_attempts", "delivery_notifications", "feedback_submissions"}
            ],
        )
        with scoped.begin() as db:
            for table in ['generated_ads','facebook_campaigns','facebook_adsets','facebook_ads']:
                db.execute(text(f'ALTER TABLE {table} DROP COLUMN created_by_id'))
            db.execute(text('ALTER TABLE generated_ads DROP COLUMN generation_context'))
            db.execute(
                text(
                    "INSERT INTO brands (id,name) VALUES ('test-legacy','test-unassigned')"
                )
            )
        env = {
            **os.environ,
            "DATABASE_URL": url,
            "PGOPTIONS": "-csearch_path=" + schema,
        }
        cwd = Path(__file__).resolve().parents[2]
        for args in [
            ("stamp", "bw_feedback_001"),
            ("upgrade", "head"),
            ("upgrade", "head"),
        ]:
            result = subprocess.run(
                [sys.executable, "-m", "alembic", *args],
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert result.returncode == 0, result.stderr
        with scoped.connect() as db:
            assert (
                db.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == "feedback_mirror_20261029"
            )
            assert (
                db.execute(text("SELECT name FROM brands")).scalar_one()
                == "test-unassigned"
            )
            assert (
                db.execute(text("SELECT count(*) FROM workspace_accounts")).scalar_one()
                == 0
            )
            assert TABLES <= set(inspect(db).get_table_names(schema=schema))
    finally:
        scoped.dispose()
        with engine.begin() as db:
            db.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        engine.dispose()
