import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, text, union_all, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.delivery import config
from app.delivery.models import (
    DeliveryJob,
    DeliverySettings,
    ManagedAd,
    DeliverySync,
    DeliveryPostAttempt,
    new_id,
)
from app.delivery.errors import classify_write_error
from app.delivery.recovery import notify_failure
from app.delivery.provider import DeliveryProvider, ProviderError, job_marker
from app.delivery.budget import RequestDeferred
from app.models import FacebookAd, User


def utcnow():
    return datetime.now(timezone.utc)


def get_settings(db):
    db.execute(insert(DeliverySettings).values(id=1).on_conflict_do_nothing())
    return db.get(DeliverySettings, 1)


@contextmanager
def worker_session(engine, lane):
    with engine.connect() as connection:
        locked = connection.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": config.LOCK_KEYS[lane]}
        ).scalar()
        connection.commit()
        if not locked:
            yield None
            return
        try:
            with Session(bind=connection, expire_on_commit=False) as db:
                yield db
        finally:
            if not connection.invalidated:
                connection.rollback()
                connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": config.LOCK_KEYS[lane]},
                )
                connection.commit()


def enqueue(db, owner_id, request_key, account_id, kind, payload):
    fingerprint = hashlib.sha256(
        json.dumps(
            [
                account_id,
                kind,
                {
                    key: value
                    for key, value in payload.items()
                    if key != "creative_snapshot"
                },
            ],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    # Serializes a buyer's quota and submission key, including concurrent requests.
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:owner, 195558003))"),
        {"owner": owner_id},
    )
    existing = db.scalar(
        select(DeliveryJob).where(
            DeliveryJob.owner_id == owner_id, DeliveryJob.request_key == request_key
        )
    )
    if existing:
        if existing.request_hash != fingerprint:
            raise ValueError("This request key was already used with different input")
        db.commit()
        return existing
    pending = db.scalar(
        select(func.count())
        .select_from(DeliveryJob)
        .where(
            DeliveryJob.owner_id == owner_id,
            DeliveryJob.status.in_(["queued", "working"]),
        )
    )
    if pending >= config.MAX_PENDING_PER_BUYER:
        raise ValueError("Your posting queue is full")
    job = DeliveryJob(
        owner_id=owner_id,
        request_key=request_key,
        request_hash=fingerprint,
        account_id=account_id,
        kind=kind,
        name=payload["name"],
        payload=payload,
        results=(
            {"creative_id": payload["resume_creative_id"]}
            if payload.get("resume_creative_id")
            else {}
        ),
        generated_ad_id=payload.get("generated_ad_id"),
        creative_asset_id=payload.get("creative_asset_id"),
        creative_snapshot=payload.get("creative_snapshot"),
        stage=(
            ("video_upload" if payload.get("media_type") == "video" else "image")
            if kind == "launch" and not payload.get("resume_creative_id")
            else "ad"
        ),
    )
    db.add(job)
    db.flush()
    if job.creative_asset_id:
        from app.creatives.models import CreativeAsset
        from app.creatives.service import event

        event(
            db,
            db.get(CreativeAsset, job.creative_asset_id),
            owner_id,
            "launched",
            {
                "job_id": job.id,
                "metadata_revision": job.creative_snapshot["metadata_revision"],
            },
        )
    get_settings(db)
    db.commit()
    return job


def posting_allowed(db, settings, now):
    if settings.paused:
        return False
    # Include old workers' reservations during a rolling deployment.
    legacy = select(
        DeliveryJob.post_started_at.label("started_at"), DeliveryJob.finished_at
    ).where(
        DeliveryJob.post_started_at.is_not(None),
        ~select(DeliveryPostAttempt.id)
        .where(
            DeliveryPostAttempt.job_id == DeliveryJob.id,
            DeliveryPostAttempt.started_at == DeliveryJob.post_started_at,
        )
        .exists(),
    )
    attempts = union_all(
        select(DeliveryPostAttempt.started_at, DeliveryPostAttempt.finished_at), legacy
    ).subquery()
    latest, finished = db.execute(
        select(func.max(attempts.c.started_at), func.max(attempts.c.finished_at))
    ).one()
    gate = max([date for date in [latest, finished] if date is not None], default=None)
    if gate and now < gate + timedelta(seconds=settings.min_interval_seconds):
        return False
    count = db.scalar(
        select(func.count())
        .select_from(attempts)
        .where(attempts.c.started_at > now - timedelta(seconds=settings.window_seconds))
    )
    return count < settings.max_posts


def finish_attempt(db, job):
    db.execute(
        update(DeliveryPostAttempt)
        .where(
            DeliveryPostAttempt.job_id == job.id,
            DeliveryPostAttempt.finished_at.is_(None),
        )
        .values(finished_at=utcnow())
    )


def persist_ad(db, job, fb_ad_id):
    p, result = job.payload, job.results
    creative_id = result.get("creative_id") or p["creative_id"]
    adset_id = p["adset_id"]
    local_id = None
    if job.kind == "launch":
        local_id = job.id
        if not db.get(FacebookAd, local_id):
            db.add(
                FacebookAd(
                    id=local_id,
                    created_by_id=job.owner_id,
                    adset_id=p["local_adset_id"],
                    name=job.name,
                    creative_name=job.name,
                    media_type=p["media_type"],
                    image_url=p["media_url"] if p["media_type"] == "image" else None,
                    video_url=p["media_url"] if p["media_type"] == "video" else None,
                    video_id=result.get("video_id"),
                    thumbnail_url=p.get("thumbnail_url") or result.get("thumbnail_url"),
                    bodies=[p["primary_text"]],
                    headlines=[p["headline"]],
                    description=p.get("description"),
                    cta=p["cta"],
                    website_url=p["website_url"],
                    status=p["status"],
                    fb_ad_id=fb_ad_id,
                    fb_creative_id=creative_id,
                )
            )
            db.flush()
    db.execute(
        insert(ManagedAd)
        .values(
            job_id=job.id,
            owner_id=job.owner_id,
            creative_asset_id=job.creative_asset_id,
            creative_snapshot=job.creative_snapshot,
            account_id=job.account_id,
            fb_ad_id=fb_ad_id,
            fb_adset_id=adset_id,
            fb_creative_id=creative_id,
            local_ad_id=local_id,
            generated_ad_id=job.generated_ad_id,
            name=job.name,
            effective_status=None,
        )
        .on_conflict_do_nothing(index_elements=["job_id"])
    )
    for kind in ["status", "performance"]:
        db.execute(
            insert(DeliverySync)
            .values(owner_id=job.owner_id, account_id=job.account_id, kind=kind)
            .on_conflict_do_nothing()
        )
    job.results = {**result, "ad_id": fb_ad_id}
    job.status, job.stage, job.error_message = "succeeded", "complete", None
    job.finished_at = utcnow()
    job.error_code = None
    job.provider_error_code = job.provider_error_subcode = None
    job.retry_allowed = False
    finish_attempt(db, job)
    db.commit()


def advance(db, job, stage, results):
    job.results = {**job.results, **results}
    job.stage, job.status = stage, "queued"
    job.stage_started_at = None
    job.read_failures = 0
    job.error_code = job.error_message = None
    job.provider_error_code = job.provider_error_subcode = None
    db.commit()


def fail_job(db, job, message, uncertain=False, code=None, retry_allowed=False):
    job.status = "needs_reconciliation" if uncertain else "failed"
    job.error_message, job.finished_at = message, utcnow()
    job.error_code = code or ("META_WRITE_UNKNOWN" if uncertain else "POSTING_FAILED")
    job.retry_allowed = retry_allowed and not uncertain and not job.results.get("ad_id")
    job.failure_id = new_id()
    finish_attempt(db, job)
    notify_failure(db, job)
    db.commit()


def write_failed(db, job, settings, error):
    from app.delivery.sync import retry_delay

    failure = classify_write_error(error, job.stage)
    job.error_code, job.error_message = failure.code, str(failure)
    job.provider_error_code = failure.provider_code
    job.provider_error_subcode = failure.provider_subcode
    if not failure.safe_to_retry:
        fail_job(db, job, str(failure), True, failure.code)
        return
    job.write_failures += 1
    now = utcnow()
    job.retry_started_at = job.retry_started_at or now
    delay = retry_delay(job.write_failures, failure.retry_after)
    deadline = job.retry_started_at + timedelta(
        seconds=config.POST_RETRY_DEADLINE_SECONDS
    )
    finish_attempt(db, job)
    if (
        failure.automatic
        and job.write_failures <= settings.max_post_retries
        and now + timedelta(seconds=delay) < deadline
    ):
        job.status = "queued"
        job.available_at = now + timedelta(seconds=delay)
        job.error_message += " A retry is scheduled."
        db.commit()
        return
    message = str(failure)
    if failure.automatic:
        message += " Automatic retries stopped at the retry or time limit. Retry when the issue is resolved."
    fail_job(db, job, message, code=failure.code, retry_allowed=failure.retry_allowed)


def video_read_failed(db, job, settings, error):
    from app.delivery.sync import retry_delay

    job.read_failures += 1
    now = utcnow()
    delay = retry_delay(job.read_failures, getattr(error, "retry_after", 0))
    deadline = (job.stage_started_at or job.created_at) + timedelta(
        seconds=config.VIDEO_TIMEOUT_SECONDS
    )
    if (
        isinstance(error, ProviderError)
        and error.retryable
        and job.read_failures <= settings.max_read_retries
        and now + timedelta(seconds=delay) < deadline
    ):
        job.status = "queued"
        job.available_at = now + timedelta(seconds=delay)
        db.commit()
    else:
        fail_job(
            db,
            job,
            "Video status could not be read within its retry/time limit. Check the video in Meta before using a new draft.",
            code="VIDEO_READ_FAILED",
        )


def posting_tick(engine, provider_factory=None):
    with worker_session(engine, "posting") as db:
        if db is None:
            return
        now = utcnow()
        settings = get_settings(db)
        settings.posting_heartbeat = now
        # A working row under an unowned advisory lock came from an interrupted process.
        for stale in db.scalars(
            select(DeliveryJob).where(DeliveryJob.status == "working")
        ).all():
            if stale.results.get("ad_id"):
                try:
                    persist_ad(db, stale, stale.results["ad_id"])
                except Exception as error:
                    if db.get_bind().invalidated or getattr(
                        error, "connection_invalidated", False
                    ):
                        raise
                    db.rollback()
                    db.refresh(stale)
                    fail_job(
                        db,
                        stale,
                        "Meta ad exists; local saving needs reconciliation.",
                        True,
                        "LOCAL_SAVE_FAILED",
                    )
            elif stale.stage == "video_ready":
                video_read_failed(
                    db,
                    stale,
                    settings,
                    ProviderError("Interrupted video read", retryable=True),
                )
            else:
                fail_job(
                    db,
                    stale,
                    "Worker interrupted during a Facebook write. Reconcile before posting again.",
                    True,
                )
        db.commit()
        if settings.paused:
            return
        candidates = db.scalars(
            select(DeliveryJob)
            .where(DeliveryJob.status == "queued", DeliveryJob.available_at <= now)
            .order_by(DeliveryJob.created_at, DeliveryJob.id)
            .limit(100)
            .with_for_update(skip_locked=True)
        ).all()
        job = next(
            (
                j
                for j in candidates
                if j.stage != "ad" or posting_allowed(db, settings, now)
            ),
            None,
        )
        if job is None:
            return
        if (
            job.write_failures
            and job.retry_started_at
            and now
            >= job.retry_started_at
            + timedelta(seconds=config.POST_RETRY_DEADLINE_SECONDS)
        ):
            fail_job(
                db,
                job,
                "The posting retry window expired. Retry when the issue is resolved.",
                code="POST_RETRY_EXHAUSTED",
                retry_allowed=True,
            )
            return
        owner = db.get(User, job.owner_id) if job.owner_id else None
        if (
            not owner
            or not owner.is_active
            or not owner.has_permission("campaigns:write")
        ):
            fail_job(
                db,
                job,
                "Restore this buyer’s posting permission, then retry this ad.",
                code="BUYER_PERMISSION",
                retry_allowed=True,
            )
            return
        try:
            provider = (
                provider_factory()
                if provider_factory
                else DeliveryProvider.for_user(db, job.owner_id)
            )
            if hasattr(provider, "configure_budget"):
                provider.configure_budget(engine, job.account_id, "interactive")
        except Exception:
            fail_job(
                db,
                job,
                "Reconnect the buyer’s Meta account or restore its configuration, then retry this ad.",
                code="META_CONNECTION",
                retry_allowed=True,
            )
            return
        p = job.payload
        if not job.stage_started_at:
            job.stage_started_at = now
        job.status = "working"
        if job.stage == "ad":
            job.post_started_at = now
            db.add(DeliveryPostAttempt(job_id=job.id, started_at=now))
        db.commit()
        try:
            if job.stage == "image":
                image_hash = provider.upload_image(p["media_url"], job.account_id)
                advance(db, job, "creative", {"image_hash": image_hash})
            elif job.stage == "video_upload":
                uploaded = provider.upload_video(
                    p["media_url"], job.account_id, wait_for_ready=False
                )
                advance(db, job, "video_ready", {"video_id": uploaded["video_id"]})
            elif job.stage == "video_ready":
                if now >= job.stage_started_at + timedelta(
                    seconds=config.VIDEO_TIMEOUT_SECONDS
                ):
                    fail_job(
                        db,
                        job,
                        "Video processing exceeded 10 minutes. Check the video in Meta before using a new draft.",
                        code="VIDEO_TIMEOUT",
                    )
                    return
                response = provider.video_status(job.results["video_id"])
                status = response.get("status", {}).get("video_status", "").lower()
                if status == "ready":
                    thumbnails = (
                        []
                        if p.get("thumbnail_url")
                        else provider.video_thumbnails(job.results["video_id"])
                    )
                    advance(
                        db,
                        job,
                        "creative",
                        {
                            "thumbnail_url": p.get("thumbnail_url")
                            or (thumbnails[0] if thumbnails else None)
                        },
                    )
                elif status == "error":
                    fail_job(
                        db,
                        job,
                        "Meta could not process the video. Replace the video in a new draft.",
                        code="VIDEO_PROCESSING",
                    )
                else:
                    job.status = "queued"
                    job.available_at = now + timedelta(
                        seconds=config.VIDEO_POLL_SECONDS
                    )
                    db.commit()
            elif job.stage == "creative":
                creative = provider.create_creative(
                    {**p, **job.results, "name": job_marker(job)},
                    job.account_id,
                )
                advance(db, job, "ad", {"creative_id": str(creative["id"])})
            elif job.stage == "ad":
                result = provider.create_ad(
                    {
                        **p,
                        "creative_id": job.results.get("creative_id")
                        or p["creative_id"],
                        "name": job.name,
                    },
                    job.account_id,
                )
                # Commit remote identity before any local bookkeeping that can fail.
                if not str(result["id"]).isdigit():
                    raise ValueError("Invalid ad identity")
                job.results = {**job.results, "ad_id": str(result["id"])}
                db.commit()
                persist_ad(db, job, str(result["id"]))
        except RequestDeferred as error:
            db.rollback()
            db.refresh(job)
            if error.may_have_written:
                fail_job(
                    db,
                    job,
                    "Facebook preparation paused after a write; reconcile before retrying",
                    True,
                )
            else:
                job.status, job.available_at = "queued", error.until
                if job.stage == "ad":
                    db.execute(
                        delete(DeliveryPostAttempt).where(
                            DeliveryPostAttempt.job_id == job.id,
                            DeliveryPostAttempt.started_at == job.post_started_at,
                            DeliveryPostAttempt.finished_at.is_(None),
                        )
                    )
                    job.post_started_at = None
                if job.stage == "video_ready":
                    job.stage_started_at += max(timedelta(0), error.until - utcnow())
                else:
                    job.stage_started_at = None
                job.error_message = "Waiting for shared Facebook request capacity"
                db.commit()
        except Exception as error:
            if db.get_bind().invalidated or getattr(
                error, "connection_invalidated", False
            ):
                raise
            db.rollback()
            db.refresh(job)
            if job.results.get("ad_id"):
                fail_job(
                    db,
                    job,
                    "Facebook ad exists; local persistence needs reconciliation",
                    True,
                )
            elif job.stage == "video_ready":
                video_read_failed(db, job, settings, error)
            else:
                write_failed(db, job, settings, error)
