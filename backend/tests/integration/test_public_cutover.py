"""Both previously released database histories remain upgradeable."""

from pathlib import Path
import os
import subprocess
import sys

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import create_engine, inspect, text


def test_public_cutover_joins_both_released_migration_histories():
    backend = Path(__file__).resolve().parents[2]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == ["feedback_mirror_20261029"]
    ancestors = {revision.revision for revision in scripts.walk_revisions()}
    assert {"analytics_20260911", "bw_install_001"} <= ancestors
    # The cutover merge is no longer the head, but it must still join both histories.
    cutover = scripts.get_revision("public_cutover_20260912")
    assert set(cutover.down_revision) == {"analytics_20260911", "bw_install_001"}


@pytest.mark.parametrize("baseline,revision", [("installer", "bw_install_001"), ("hosted", "analytics_20260911")])
def test_released_schema_upgrade_preserves_existing_records(baseline, revision):
    backend = Path(__file__).resolve().parents[2]
    url = os.environ["TEST_CUTOVER_UPGRADE_URL"]
    engine = create_engine(url)
    assert engine.url.host in {"localhost", "127.0.0.1"}
    assert engine.url.database.startswith("test_")
    fixture = backend / "tests/integration/fixtures/cutover" / f"{baseline}.sql"
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
        connection.exec_driver_sql(fixture.read_text())
        connection.execute(text("INSERT INTO users (id,email,name,hashed_password,is_active,is_superuser) VALUES ('test-owner','test-owner@example.com','test-owner','test-preserved-password-hash',true,true)"))
        connection.execute(text("INSERT INTO generated_ads (id,image_url,media_type) VALUES ('test-ad','https://media.example.com/test-original.png','image')"))
        connection.execute(text("INSERT INTO google_ads_connections (id,user_id,customer_id,encrypted_refresh_token,is_active) VALUES ('test-google','test-owner','123','test-preserved-ciphertext',true)"))
        if baseline == "installer":
            connection.execute(text("INSERT INTO installation_state (id,installation_id,initialized,setup_status,setup_step) VALUES (1,'test-installation',true,'complete','welcome')"))
            connection.execute(text("INSERT INTO provider_connections (provider,encrypted_key,key_hint,disabled,version,status) VALUES ('fal','test-provider-ciphertext','test',false,1,'saved_unverified')"))
        config = Config(str(backend / "alembic.ini"))
        config.set_main_option("script_location", str(backend / "alembic"))
        config.attributes["connection"] = connection
        command.stamp(config, revision)
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, "-c", "from startup import bootstrap_database; bootstrap_database()"],
            cwd=backend,
            env={**os.environ, "DATABASE_URL": url, "ADMIN_EMAIL": "", "ADMIN_PASSWORD": ""},
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "feedback_mirror_20261029"
        assert connection.execute(text("SELECT id,hashed_password FROM users")).one() == ("test-owner", "test-preserved-password-hash")
        assert connection.execute(text("SELECT image_url FROM generated_ads WHERE id='test-ad'")).scalar_one() == "https://media.example.com/test-original.png"
        assert connection.execute(text("SELECT encrypted_refresh_token FROM google_ads_connections WHERE id='test-google'")).scalar_one() == "test-preserved-ciphertext"
        assert connection.execute(text("SELECT initialized,setup_status FROM installation_state")).one() == (True, "complete")
        assert {"analytics_ads", "creative_assets", "provider_connections"} <= set(inspect(connection).get_table_names())
        if baseline == "installer":
            assert connection.execute(text("SELECT encrypted_key FROM provider_connections WHERE provider='fal'")).scalar_one() == "test-provider-ciphertext"
            assert connection.execute(text("SELECT count(*) FROM creative_assets WHERE generated_ad_id='test-ad'")).scalar_one() == 1
    engine.dispose()
