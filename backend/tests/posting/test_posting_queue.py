from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from app.delivery.models import DeliveryJob, DeliverySettings, ManagedAd
from app.delivery.queue import enqueue, posting_allowed, posting_tick, worker_session

NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)
PAYLOAD = {
    "name": "test-ad",
    "adset_id": "123",
    "creative_id": "456",
    "status": "PAUSED",
}


def test_request_budget_deferral_does_not_retry_or_mark_write_uncertain(
    sessions, engine, buyer
):
    from app.delivery.budget import RequestDeferred

    until = datetime.now(timezone.utc) + timedelta(minutes=1)
    with sessions() as db:
        identity = enqueue(db, buyer.id, "test-budget", "act_789", "ad", PAYLOAD).id
    provider = Mock()
    provider.create_ad.side_effect = RequestDeferred(until)
    posting_tick(engine, lambda: provider)
    posting_tick(engine, lambda: provider)
    with sessions() as db:
        job = db.get(DeliveryJob, identity)
        assert job.status == "queued" and job.available_at == until
        assert job.post_started_at is None
        from app.delivery.models import DeliveryPostAttempt

        assert (
            db.query(DeliveryPostAttempt).filter_by(job_id=job.id).count() == 0
            and job.read_failures == 0
        )
    assert provider.create_ad.call_count == 1


def test_deferral_after_an_sdk_write_requires_reconciliation(sessions, engine, buyer):
    from app.delivery.budget import RequestDeferred

    with sessions() as db:
        identity = enqueue(
            db, buyer.id, "test-partial-upload", "act_789", "ad", PAYLOAD
        ).id
    provider = Mock()
    provider.create_ad.side_effect = RequestDeferred(
        datetime.now(timezone.utc) + timedelta(minutes=1), may_have_written=True
    )
    posting_tick(engine, lambda: provider)
    with sessions() as db:
        assert db.get(DeliveryJob, identity).status == "needs_reconciliation"


def test_concurrent_duplicate_submission_is_one_job(sessions, buyer):
    def submit(_):
        with sessions() as db:
            return enqueue(db, buyer.id, "test-request", "act_789", "ad", PAYLOAD).id

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(submit, range(8)))
    assert len(set(ids)) == 1
    with sessions() as db:
        assert db.query(DeliveryJob).count() == 1
        with pytest.raises(ValueError, match="different"):
            enqueue(
                db,
                buyer.id,
                "test-request",
                "act_789",
                "ad",
                {**PAYLOAD, "name": "changed"},
            )


def test_cadence_and_rolling_window_count_failed_attempts(sessions, buyer):
    with sessions() as db:
        settings = DeliverySettings(
            id=1, min_interval_seconds=2, max_posts=2, window_seconds=60
        )
        db.add(settings)
        for i, seconds in enumerate([59, 3]):
            job = enqueue(db, buyer.id, "test-" + str(i), "act_789", "ad", PAYLOAD)
            job.post_started_at = NOW - timedelta(seconds=seconds)
            job.status = "needs_reconciliation"
        db.commit()
        assert not posting_allowed(db, settings, NOW)
        assert posting_allowed(db, settings, NOW + timedelta(seconds=1))
        settings.max_posts = 100
        settings.min_interval_seconds = 4
        assert not posting_allowed(db, settings, NOW)


def test_worker_lock_excludes_other_replica(engine):
    with worker_session(engine, "posting") as first:
        assert first is not None
        with worker_session(engine, "posting") as second:
            assert second is None


def test_remote_timeout_is_never_automatically_reposted(sessions, engine, buyer):
    with sessions() as db:
        job = enqueue(db, buyer.id, "test-timeout", "act_789", "ad", PAYLOAD)
        job_id = job.id
    provider = Mock()
    provider.create_ad.side_effect = TimeoutError("secret-token-must-not-persist")
    posting_tick(engine, lambda: provider)
    posting_tick(engine, lambda: provider)
    assert provider.create_ad.call_count == 1
    with sessions() as db:
        job = db.get(DeliveryJob, job_id)
        assert job.status == "needs_reconciliation"
        assert "secret-token" not in job.error_message


def test_success_persists_identity_without_browser_callback(sessions, engine, buyer):
    with sessions() as db:
        job_id = enqueue(db, buyer.id, "test-success", "act_789", "ad", PAYLOAD).id
    provider = Mock()
    provider.create_ad.return_value = {"id": "987"}
    posting_tick(engine, lambda: provider)
    with sessions() as db:
        job = db.get(DeliveryJob, job_id)
        assert job.status == "succeeded"
        ad = db.query(ManagedAd).one()
        assert (ad.fb_ad_id, ad.account_id, ad.owner_id, ad.job_id) == (
            "987",
            "act_789",
            buyer.id,
            job_id,
        )


def test_interrupted_write_moves_to_reconciliation(sessions, engine, buyer):
    with sessions() as db:
        job = enqueue(db, buyer.id, "test-interrupted", "act_789", "ad", PAYLOAD)
        job.status = "working"
        job.stage = "ad"
        db.commit()
    provider = Mock()
    posting_tick(engine, lambda: provider)
    provider.create_ad.assert_not_called()
    with sessions() as db:
        assert db.query(DeliveryJob).one().status == "needs_reconciliation"


def test_full_launch_survives_between_stages_and_saves_local_ad(
    sessions, engine, buyer
):
    from app.models import FacebookAd, FacebookCampaign, FacebookAdSet

    payload = {
        "name": "test-image",
        "adset_id": "123",
        "local_adset_id": "test-adset",
        "page_id": "321",
        "media_url": "https://example.com/test.png",
        "media_type": "image",
        "primary_text": "test-body",
        "headline": "test-title",
        "description": "",
        "website_url": "https://example.com",
        "cta": "LEARN_MORE",
        "status": "PAUSED",
    }
    with sessions() as db:
        db.add(
            FacebookCampaign(
                id="test-campaign",
                name="test-campaign",
                objective="OUTCOME_SALES",
                budget_type="ABO",
            )
        )
        db.flush()
        db.add(
            FacebookAdSet(
                id="test-adset",
                campaign_id="test-campaign",
                name="test-adset",
                optimization_goal="OFFSITE_CONVERSIONS",
                fb_adset_id="123",
            )
        )
        db.commit()
        job_id = enqueue(db, buyer.id, "test-full", "act_789", "launch", payload).id
    provider = Mock()
    provider.upload_image.return_value = "test-image-hash"
    provider.create_creative.return_value = {"id": "456"}
    provider.create_ad.return_value = {"id": "987"}
    for _ in range(3):
        posting_tick(engine, lambda: provider)
    with sessions() as db:
        assert db.get(DeliveryJob, job_id).status == "succeeded"
        assert db.get(FacebookAd, job_id).fb_ad_id == "987"
        assert db.query(ManagedAd).one().local_ad_id == job_id
    assert (
        provider.upload_image.call_count
        == provider.create_creative.call_count
        == provider.create_ad.call_count
        == 1
    )


def test_permission_revocation_stops_queued_job(sessions, engine, buyer):
    from app.models import User

    with sessions() as db:
        enqueue(db, buyer.id, "test-revoked", "act_789", "ad", PAYLOAD)
        db.get(User, buyer.id).is_active = False
        db.commit()
    provider = Mock()
    posting_tick(engine, lambda: provider)
    provider.create_ad.assert_not_called()
    with sessions() as db:
        assert db.query(DeliveryJob).one().status == "failed"


def test_video_read_failure_respects_zero_retry_limit(sessions, engine, buyer):
    from app.delivery.provider import ProviderError

    with sessions() as db:
        db.add(DeliverySettings(id=1, max_read_retries=0))
        job = enqueue(db, buyer.id, "test-video-read", "act_789", "ad", PAYLOAD)
        job.stage = "video_ready"
        job.results = {"video_id": "432"}
        db.commit()
    provider = Mock()
    provider.video_status.side_effect = ProviderError("test-transient", retryable=True)
    posting_tick(engine, lambda: provider)
    posting_tick(engine, lambda: provider)
    assert provider.video_status.call_count == 1
    with sessions() as db:
        assert db.query(DeliveryJob).one().status == "failed"


def test_video_upload_is_checkpointed_before_readiness(sessions, engine, buyer):
    payload = {
        **PAYLOAD,
        "media_type": "video",
        "media_url": "https://example.com/test.mp4",
    }
    with sessions() as db:
        job = enqueue(db, buyer.id, "test-video", "act_789", "launch", payload)
        job_id = job.id
    provider = Mock()
    provider.upload_video.return_value = {"video_id": "432"}
    provider.video_status.return_value = {"status": {"video_status": "ready"}}
    provider.video_thumbnails.return_value = ["https://example.com/test.jpg"]
    posting_tick(engine, lambda: provider)
    with sessions() as db:
        job = db.get(DeliveryJob, job_id)
        assert job.stage == "video_ready" and job.results["video_id"] == "432"
    provider.video_status.assert_not_called()
    posting_tick(engine, lambda: provider)
    with sessions() as db:
        assert db.get(DeliveryJob, job_id).stage == "creative"
    assert provider.upload_video.call_count == 1


def test_lost_database_connection_does_not_allow_stale_worker_to_repost(
    sessions, engine, buyer
):
    from sqlalchemy import text
    from app.delivery import config

    with sessions() as db:
        job_id = enqueue(
            db, buyer.id, "test-db-disconnect", "act_789", "ad", PAYLOAD
        ).id
    provider = Mock()

    def remote_create(*args):
        with engine.begin() as other:
            pid = other.execute(
                text(
                    "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND objid = :key AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
                ),
                {"key": config.LOCK_KEYS["posting"]},
            ).scalar_one()
            other.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
        return {"id": "987"}

    provider.create_ad.side_effect = remote_create
    with pytest.raises(Exception):
        posting_tick(engine, lambda: provider)
    posting_tick(engine, lambda: provider)
    assert provider.create_ad.call_count == 1
    with sessions() as db:
        assert db.get(DeliveryJob, job_id).status == "needs_reconciliation"


def test_interrupted_video_read_consumes_retry_budget(sessions, engine, buyer):
    with sessions() as db:
        job = enqueue(db, buyer.id, "test-video-interrupted", "act_789", "ad", PAYLOAD)
        job.status, job.stage, job.read_failures = "working", "video_ready", 3
        db.commit()
    provider = Mock()
    posting_tick(engine, lambda: provider)
    provider.video_status.assert_not_called()
    with sessions() as db:
        assert db.query(DeliveryJob).one().status == "failed"


def test_ad_name_stays_clean_and_the_launch_marker_moves_to_the_creative(
    sessions, engine, buyer
):
    """Ad names are reported as ad_name and feed buyers' tracking, so the
    [bw:<job>] launch marker belongs on the creative, which is never reported."""
    from app.delivery.provider import job_marker, matches_job

    payload = {**PAYLOAD, "name": "WC_Q4_Solar_v3"}
    with sessions() as db:
        job = enqueue(db, buyer.id, "test-wc-key", "act_789", "ad", payload)
        job_id, marker = job.id, job_marker(job)
    provider = Mock()
    provider.create_creative.return_value = {"id": "456"}
    provider.create_ad.return_value = {"id": "987"}
    posting_tick(engine, lambda: provider)

    sent = provider.create_ad.call_args.args[0]["name"]
    assert sent == "WC_Q4_Solar_v3", "the ad name must be exactly what the buyer typed"
    assert "[bw:" not in sent
    assert marker == "WC_Q4_Solar_v3 [bw:" + job_id + "]"

    with sessions() as db:
        job = db.get(DeliveryJob, job_id)
        remote = {
            "id": "987",
            "name": "WC_Q4_Solar_v3",
            "account_id": "789",
            "adset_id": "123",
            "creative": {"id": "456", "name": marker},
        }
        # recovery still identifies the job despite the clean ad name
        assert matches_job(job, remote)
        assert not matches_job(
            job, {**remote, "creative": {"id": "456", "name": "another job's creative"}}
        )
        assert not matches_job(job, {**remote, "creative": {"id": "999", "name": marker}})
