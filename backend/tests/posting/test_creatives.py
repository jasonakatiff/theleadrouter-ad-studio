import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.deps import get_current_active_user
from app.database import get_db
from app.models import (
    GeneratedAd,
    User,
    Role,
    Permission,
    FacebookCampaign,
    FacebookAdSet,
)
from app.creatives.api import router
from app.creatives.models import CreativeAsset, CreativeEvent
from app.creatives.service import register_generated
from app.delivery.api import router as delivery_router


@pytest.fixture
def creative_api(sessions, buyer, monkeypatch):
    application = FastAPI()
    from app.core.installation import InstallationError
    from fastapi.responses import JSONResponse

    @application.exception_handler(InstallationError)
    async def installation_error_handler(request, error):
        return JSONResponse(status_code=error.status_code, content=error.body())

    application.include_router(router, prefix="/api/v1/creatives")
    application.include_router(delivery_router, prefix="/api/v1/delivery")
    from app.api.v1.generated_ads import router as generated_router

    application.include_router(generated_router, prefix="/api/v1/generated-ads")

    def database():
        with sessions() as db:
            yield db

    application.dependency_overrides[get_db] = database
    application.dependency_overrides[get_current_active_user] = lambda: buyer
    from app.creatives import api

    monkeypatch.setattr(
        api, "store_upload", lambda file: "https://media.example.com/test.png"
    )
    monkeypatch.setattr(
        api,
        "analyze_media",
        lambda *args: {
            "background": "studio",
            "lighting": "soft",
            "color_scheme": "warm",
        },
    )
    with TestClient(application) as client:
        yield client, application


def upload(client):
    response = client.post(
        "/api/v1/creatives/uploads",
        files={"file": ("test.png", io.BytesIO(b"test-image"), "image/png")},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_upload_source_author_analysis_and_edit_revisions(
    creative_api, sessions, buyer
):
    client, _ = creative_api
    asset = upload(client)
    assert asset["source_type"] == "external_upload"
    assert asset["created_by_id"] == buyer.id
    assert asset["analysis_status"] == "pending"
    analyzed = client.post(f'/api/v1/creatives/{asset["id"]}/analyze')
    assert analyzed.status_code == 200, analyzed.text
    data = analyzed.json()
    assert data["metadata"]["lighting"] == "soft"
    assert data["metadata"]["talent_gender"] is None
    assert data["metadata_revision"] == 1
    edited = client.patch(
        f'/api/v1/creatives/{asset["id"]}',
        json={
            "expected_revision": 1,
            "metadata": {
                **data["metadata"],
                "lighting": "hard",
                "talent_gender": "female",
            },
        },
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["metadata_revision"] == 2
    assert (
        client.patch(
            f'/api/v1/creatives/{asset["id"]}',
            json={"expected_revision": 1, "metadata": data["metadata"]},
        ).status_code
        == 409
    )
    assert (
        client.patch(
            f'/api/v1/creatives/{asset["id"]}',
            json={
                "expected_revision": 2,
                "created_by_id": "spoof",
                "metadata": data["metadata"],
            },
        ).status_code
        == 422
    )
    with sessions() as db:
        events = db.scalars(
            select(CreativeEvent)
            .where(CreativeEvent.asset_id == asset["id"])
            .order_by(CreativeEvent.created_at)
        ).all()
        assert [event.action for event in events] == [
            "uploaded",
            "analyzed",
            "metadata_edited",
        ]
        assert all(event.actor_id == buyer.id for event in events)


def test_generated_registration_preserves_creator_and_unknown_history(
    creative_api, sessions, buyer
):
    client, _ = creative_api
    with sessions() as db:
        known = GeneratedAd(
            id="test-generated",
            image_url="https://media.example.com/test.png",
            created_by_id=buyer.id,
            prompt="test-prompt",
        )
        old = GeneratedAd(
            id="test-old", image_url="https://media.example.com/test-old.png"
        )
        db.add_all([known, old])
        db.flush()
        first = register_generated(db, known, buyer.id)
        second = register_generated(db, known, buyer.id)
        assert first.id == second.id
        db.commit()
    response = client.post("/api/v1/creatives/generated/test-old")
    assert response.status_code == 200, response.text
    assert response.json()["created_by_id"] is None
    assert response.json()["registered_by_id"] == buyer.id
    assert response.json()["source_type"] == "system_generated"
    assert (
        client.get("/api/v1/creatives?source_type=system_generated").json()[
            "pagination"
        ]["total"]
        == 2
    )


def test_analysis_failure_is_retryable_and_other_users_cannot_edit(
    creative_api, sessions, monkeypatch
):
    client, application = creative_api
    asset = upload(client)
    from app.creatives import api

    def fail(*args):
        raise RuntimeError("test-provider-secret-must-not-leak")

    monkeypatch.setattr(api, "analyze_media", fail)
    failure = client.post(f'/api/v1/creatives/{asset["id"]}/analyze')
    assert failure.status_code == 502
    assert "test-provider-secret" not in failure.text
    assert (
        client.get(f'/api/v1/creatives/{asset["id"]}').json()["analysis_status"]
        == "failed"
    )
    monkeypatch.setattr(api, "analyze_media", lambda *args: {"lighting": "soft"})
    assert client.post(f'/api/v1/creatives/{asset["id"]}/analyze').status_code == 200
    with sessions() as db:
        role = Role(
            id="test-creative-role",
            name="test-creative-role",
            permissions=[Permission(id="test-creative-write", name="campaigns:write")],
        )
        other = User(
            id="test-other-creative-user",
            email="test-other-creative@example.com",
            hashed_password="test-unused",
            is_active=True,
            roles=[role],
        )
        db.add(other)
        db.commit()
    application.dependency_overrides[get_current_active_user] = lambda: other
    assert client.get(f'/api/v1/creatives/{asset["id"]}').status_code == 200
    assert (
        client.patch(
            f'/api/v1/creatives/{asset["id"]}',
            json={"expected_revision": 1, "metadata": {}},
        ).status_code
        == 403
    )
    assert client.delete(f'/api/v1/creatives/{asset["id"]}').status_code == 403


def test_launch_requires_analyzed_asset_and_preserves_snapshot(
    creative_api, sessions, buyer
):
    client, _ = creative_api
    asset = upload(client)
    with sessions() as db:
        db.add(
            FacebookCampaign(
                id="test-creative-campaign",
                name="test-campaign",
                objective="OUTCOME_SALES",
                budget_type="ABO",
            )
        )
        db.flush()
        db.add(
            FacebookAdSet(
                id="test-creative-adset",
                campaign_id="test-creative-campaign",
                name="test-adset",
                optimization_goal="OFFSITE_CONVERSIONS",
                fb_adset_id="123",
            )
        )
        db.commit()
    payload = {
        "request_key": "test-creative-launch",
        "account_id": "act_123",
        "name": "test-launch",
        "local_adset_id": "test-creative-adset",
        "page_id": "456",
        "media_url": asset["media_url"],
        "media_type": "image",
        "primary_text": "test-body",
        "headline": "test-headline",
        "website_url": "https://example.com",
        "creative_asset_id": asset["id"],
    }
    assert client.post("/api/v1/delivery/launches", json=payload).status_code == 422
    ready = client.post(f'/api/v1/creatives/{asset["id"]}/analyze').json()
    mismatch = client.post(
        "/api/v1/delivery/launches",
        json={**payload, "media_url": "https://example.com/wrong.png"},
    )
    assert mismatch.status_code == 422
    launched = client.post("/api/v1/delivery/launches", json=payload)
    assert launched.status_code == 202, launched.text
    job = launched.json()
    assert job["creative_snapshot"]["metadata"]["lighting"] == "soft"
    assert job["owner_id"] == buyer.id
    client.patch(
        f'/api/v1/creatives/{asset["id"]}',
        json={
            "expected_revision": 1,
            "metadata": {**ready["metadata"], "lighting": "hard"},
        },
    )
    replay = client.post("/api/v1/delivery/launches", json=payload)
    assert replay.status_code == 202, replay.text
    assert replay.json()["creative_snapshot"] == job["creative_snapshot"]
    assert replay.json()["id"] == job["id"]
    assert client.delete(f'/api/v1/creatives/{asset["id"]}').status_code == 200
    assert client.get("/api/v1/creatives").json()["pagination"]["total"] == 0
    assert (
        client.get("/api/v1/delivery/jobs/" + job["id"]).json()["creative_snapshot"]
        == job["creative_snapshot"]
    )


def test_storage_validates_bytes_size_and_uses_unique_media_key(monkeypatch):
    from fastapi import UploadFile, HTTPException
    from starlette.datastructures import Headers
    from app.creatives.api import store_upload
    from app.api.v1 import uploads
    from app.core.config import settings

    saved = []

    class Storage:
        def upload_fileobj(self, media, bucket, key, ExtraArgs):
            saved.append((media.read(), key, ExtraArgs))

    monkeypatch.setattr(uploads, "get_s3_client", lambda: Storage())
    monkeypatch.setattr(settings, "R2_PUBLIC_URL", "https://media.example.com")

    def file(data, filename="test.png", mime="image/png"):
        return UploadFile(
            filename=filename,
            file=io.BytesIO(data),
            headers=Headers({"content-type": mime}),
        )

    with pytest.raises(HTTPException):
        store_upload(file(b"not-an-image"))
    with pytest.raises(HTTPException):
        store_upload(file(b""))
    with pytest.raises(HTTPException):
        store_upload(file(b"\x89PNG\r\n\x1a\n" + b"x" * (10 * 1024 * 1024)))
    with pytest.raises(HTTPException):
        store_upload(file(b"test", "test.svg", "image/svg+xml"))
    image = b"\x89PNG\r\n\x1a\ntest"
    first = store_upload(file(image))
    second = store_upload(file(image))
    assert first != second and first.startswith("https://media.example.com/creatives/")
    assert saved[0][0] == image
    assert saved[0][2] == {"ContentType": "image/png"}


def test_creative_upload_uses_installer_media_volume(monkeypatch, tmp_path):
    from fastapi import UploadFile
    from starlette.datastructures import Headers
    from app.api.v1 import uploads
    from app.core.config import settings
    from app.creatives.api import store_upload

    monkeypatch.setattr(uploads, "get_s3_client", lambda: None)
    monkeypatch.setattr(uploads, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(settings, "PUBLIC_API_URL", "https://api.example.com")
    original = b"\x89PNG\r\n\x1a\ntest-original"
    upload = UploadFile(file=io.BytesIO(original), filename="../../test.png", headers=Headers({"content-type": "image/png"}))
    url = store_upload(upload)
    assert url.startswith("https://api.example.com/uploads/creative-")
    assert ".." not in url
    stored = list(tmp_path.iterdir())
    assert len(stored) == 1 and stored[0].read_bytes() == original


@pytest.mark.parametrize("video", [False, True])
def test_analysis_sends_real_media_and_cleans_up_provider_video(
    monkeypatch, tmp_path, video
):
    import json
    import httpx
    from contextlib import contextmanager
    from app.creatives import analysis
    from app.creatives.schemas import CreativeMetadata

    path = tmp_path / ("test.mp4" if video else "test.png")
    path.write_bytes(b"test-original-media")

    @contextmanager
    def download(url, video=False):
        yield str(path)

    monkeypatch.setattr(analysis, "download_media", download)
    monkeypatch.setattr(analysis, "require_provider_key", lambda provider: "test-analysis-key")
    calls = []
    values = CreativeMetadata(lighting="soft").model_dump()
    values.pop("talent_gender")

    def handle(request):
        calls.append(request)
        if request.url.path == "/upload/v1beta/files":
            return httpx.Response(
                200,
                headers={"x-goog-upload-url": analysis.BASE_URL + "/upload/test-file"},
            )
        if request.url.path == "/upload/test-file":
            assert request.read() == b"test-original-media"
            return httpx.Response(
                200,
                json={
                    "file": {
                        "name": "files/test-media",
                        "state": "ACTIVE",
                        "uri": "https://generativelanguage.googleapis.com/v1beta/files/test-media",
                    }
                },
            )
        if request.method == "DELETE":
            return httpx.Response(200, json={})
        body = json.loads(request.content)
        media = body["contents"][0]["parts"][1]
        assert ("file_data" in media) if video else ("inline_data" in media)
        assert (
            "talent_gender"
            not in body["generationConfig"]["responseSchema"]["properties"]
        )
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": json.dumps(values)}]}}]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(analysis.httpx, "Client", lambda **kwargs: client)
    result = analysis.analyze_media(
        "https://example.com/" + path.name, "video" if video else "image"
    )
    assert result["lighting"] == "soft" and result["talent_gender"] is None
    if video:
        assert calls[-1].method == "DELETE"


def analyze_image_with(monkeypatch, tmp_path, responses):
    """Run image analysis against a scripted sequence of Gemini responses."""
    import httpx
    from contextlib import contextmanager
    from app.creatives import analysis

    path = tmp_path / "test.png"
    path.write_bytes(b"test-original-media")

    @contextmanager
    def download(url, video=False):
        yield str(path)

    monkeypatch.setattr(analysis, "download_media", download)
    monkeypatch.setattr(analysis, "require_provider_key", lambda provider: "test-analysis-key")
    monkeypatch.setattr(analysis, "RETRY_SECONDS", 0)
    calls = []

    def handle(request):
        calls.append(request)
        return responses.pop(0)

    client = httpx.Client(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(analysis.httpx, "Client", lambda **kwargs: client)
    return analysis.analyze_media("https://example.com/test.png", "image"), calls


def gemini_json(values):
    import httpx
    import json

    return httpx.Response(
        200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(values)}]}}]}
    )


def test_analysis_fits_verbose_empty_and_unknown_labels_to_the_schema(monkeypatch, tmp_path):
    result, _ = analyze_image_with(
        monkeypatch,
        tmp_path,
        [gemini_json({"talent_type": "influencer", "background": "", "lighting": "soft", "messaging_angle": "x" * 900})],
    )
    assert result["talent_type"] == "unknown"
    assert result["background"] == "unknown"
    assert result["lighting"] == "soft"
    assert len(result["messaging_angle"]) == 500


def test_analysis_retries_a_transient_provider_failure(monkeypatch, tmp_path):
    import httpx

    result, calls = analyze_image_with(
        monkeypatch, tmp_path, [httpx.Response(503, json={}), gemini_json({"lighting": "soft"})]
    )
    assert result["lighting"] == "soft" and len(calls) == 2


@pytest.mark.parametrize(
    "responses,expected,settings",
    [
        ([{"promptFeedback": {"blockReason": "SAFETY"}}], "declined to analyze this creative (SAFETY)", False),
        ([{"candidates": [{"finishReason": "IMAGE_SAFETY"}]}], "(IMAGE_SAFETY)", False),
        ([(400, {"error": {"message": "Unsupported MIME type: image/gif"}})], "Unsupported MIME type: image/gif", False),
        ([(429, {}), (429, {})], "rate-limiting", False),
        ([(400, {"error": {"message": "API key not valid.", "details": [{"reason": "API_KEY_INVALID"}]}})], "Settings → Integrations", True),
        ([(403, {"error": {"message": "Permission denied"}})], "Settings → Integrations", True),
    ],
)
def test_analysis_failures_name_their_cause(monkeypatch, tmp_path, responses, expected, settings):
    import httpx
    from app.creatives.analysis import AnalysisError, failure_message

    scripted = [
        httpx.Response(item[0], json=item[1]) if isinstance(item, tuple) else httpx.Response(200, json=item)
        for item in responses
    ]
    with pytest.raises(AnalysisError) as failure:
        analyze_image_with(monkeypatch, tmp_path, scripted)
    message = failure_message(failure.value)
    assert expected in message
    assert ("Settings" in message) is settings
    assert "test-analysis-key" not in message


def test_analysis_error_reaches_the_creative_without_blaming_settings(creative_api, monkeypatch):
    from app.creatives import api
    from app.creatives.analysis import AnalysisError

    client, _ = creative_api
    asset = upload(client)

    def blocked(*args):
        raise AnalysisError("Gemini declined to analyze this creative (SAFETY).")

    monkeypatch.setattr(api, "analyze_media", blocked)
    failure = client.post(f'/api/v1/creatives/{asset["id"]}/analyze')
    assert failure.status_code == 502
    stored = client.get(f'/api/v1/creatives/{asset["id"]}').json()["analysis_error"]
    assert stored == "Gemini declined to analyze this creative (SAFETY)."

    def crashed(*args):
        raise RuntimeError("test-provider-secret-must-not-leak")

    monkeypatch.setattr(api, "analyze_media", crashed)
    client.post(f'/api/v1/creatives/{asset["id"]}/analyze')
    stored = client.get(f'/api/v1/creatives/{asset["id"]}').json()["analysis_error"]
    assert stored == "Analysis failed unexpectedly. Retry analysis."


def test_reusing_another_creators_asset_keeps_both_users_and_report_link(
    creative_api, sessions, buyer
):
    from datetime import date
    from decimal import Decimal
    from app.delivery.models import DeliveryJob, ManagedAd, AdInsight
    from app.delivery.queue import persist_ad, utcnow
    from app.delivery import config

    client, _ = creative_api
    with sessions() as db:
        creator = User(
            id="test-strategist",
            email="test-strategist@example.com",
            name="test-strategist",
            hashed_password="test-unused",
            is_active=True,
        )
        db.add(creator)
        db.flush()
        generated = GeneratedAd(
            id="test-strategy-ad",
            image_url="https://media.example.com/test.png",
            created_by_id=creator.id,
        )
        db.add(generated)
        db.flush()
        asset = register_generated(db, generated, creator.id)
        asset.analysis_status = "ready"
        asset.metadata_revision = 1
        db.add(
            FacebookCampaign(
                id="test-attribution-campaign",
                name="test-campaign",
                objective="OUTCOME_SALES",
                budget_type="ABO",
            )
        )
        db.flush()
        db.add(
            FacebookAdSet(
                id="test-attribution-adset",
                campaign_id="test-attribution-campaign",
                name="test-adset",
                optimization_goal="OFFSITE_CONVERSIONS",
                fb_adset_id="123",
            )
        )
        db.commit()
        asset_id = asset.id
    payload = {
        "request_key": "test-reuse",
        "account_id": "act_123",
        "name": "test-reuse",
        "local_adset_id": "test-attribution-adset",
        "page_id": "456",
        "media_url": "https://media.example.com/test.png",
        "media_type": "image",
        "primary_text": "test-body",
        "headline": "test-headline",
        "website_url": "https://example.com",
        "creative_asset_id": asset_id,
    }
    response = client.post("/api/v1/delivery/launches", json=payload)
    assert response.status_code == 202, response.text
    with sessions() as db:
        job = db.get(DeliveryJob, response.json()["id"])
        job.results = {"creative_id": "789"}
        persist_ad(db, job, "987")
        managed = db.scalar(select(ManagedAd))
        assert managed.owner_id == buyer.id
        assert managed.creative_snapshot["created_by_id"] == "test-strategist"
        db.add(
            AdInsight(
                managed_ad_id=managed.id,
                report_date=date.today(),
                dataset=config.DATASET,
                currency="USD",
                account_timezone="UTC",
                impressions=100,
                clicks=3,
                spend=Decimal("0.29"),
                actions=[],
                imported_at=utcnow(),
            )
        )
        db.commit()
    report = client.get("/api/v1/delivery/report").json()["data"][0]
    assert report["creative_created_by_name"] == "test-strategist"
    assert report["launched_by_id"] == buyer.id
    assert (
        client.get(f"/api/v1/creatives/{asset_id}/events").json()["data"][0]["actor_id"]
        == buyer.id
    )


def test_generated_output_is_saved_by_server_before_return_and_has_no_mock_fallback(
    creative_api, sessions, buyer, monkeypatch
):
    from app.api.v1 import generated_ads

    client, _ = creative_api
    monkeypatch.setattr(generated_ads.settings, "FAL_AI_API_KEY", "")
    request = {
        "count": 1,
        "imageSizes": [{"name": "test-square", "width": 1080, "height": 1080}],
        "copy": {"headline": "test-server-saved", "body": "test-copy"},
    }
    assert (
        client.post("/api/v1/generated-ads/generate-image", json=request).status_code
        == 409
    )

    class Handler:
        async def get(self):
            return {"images": [{"url": "https://media.example.com/test-generated.png"}]}

    class Fal:
        def AsyncClient(self, *, key):
            assert key == "test-fal-key"
            return self

        async def submit(self, model, arguments):
            return Handler()

    async def save(url, prefix="generated"):
        return url

    monkeypatch.setattr(generated_ads, "fal_client", Fal())
    monkeypatch.setattr(generated_ads, "download_and_save_image", save)
    monkeypatch.setattr(generated_ads.settings, "FAL_AI_API_KEY", "test-fal-key")
    response = client.post("/api/v1/generated-ads/generate-image", json=request)
    assert response.status_code == 200, response.text
    identity = response.json()["images"][0]["id"]
    with sessions() as db:
        saved = db.get(GeneratedAd, identity)
        asset = db.scalar(
            select(CreativeAsset).where(CreativeAsset.generated_ad_id == identity)
        )
        assert saved.created_by_id == asset.created_by_id == buyer.id
        assert asset.source_type == "system_generated"
        assert asset.generation_context["headline"] == "test-server-saved"
    replay = client.post(
        "/api/v1/generated-ads/batch",
        json={"ads": [{"id": identity, "imageUrl": "https://example.com/spoof.png"}]},
    )
    assert replay.status_code == 200
    assert (
        client.get("/api/v1/creatives/" + asset.id).json()["media_url"]
        == "https://media.example.com/test-generated.png"
    )


def test_legacy_submission_replays_existing_job_but_cannot_create_new_untracked_launch(
    creative_api, sessions, buyer
):
    from app.delivery.queue import enqueue

    client, _ = creative_api
    with sessions() as db:
        db.add(
            FacebookCampaign(
                id="test-legacy-campaign",
                name="test-legacy",
                objective="OUTCOME_LEADS",
                budget_type="ABO",
            )
        )
        db.flush()
        db.add(
            FacebookAdSet(
                id="test-legacy-adset",
                campaign_id="test-legacy-campaign",
                name="test-legacy",
                optimization_goal="OFFSITE_CONVERSIONS",
                fb_adset_id="123",
            )
        )
        db.commit()
        from app.delivery.schemas import LaunchRequest

        data = LaunchRequest(
            request_key="test-legacy",
            account_id="act_123",
            name="test-legacy",
            local_adset_id="test-legacy-adset",
            page_id="123",
            media_url="https://example.com/test.png",
            media_type="image",
            primary_text="test",
            headline="test",
            website_url="https://example.com/",
        )
        payload = data.model_dump(
            mode="json", exclude={"request_key", "account_id", "creative_asset_id"}
        )
        payload["adset_id"] = "123"
        job = enqueue(
            db, buyer.id, data.request_key, data.account_id, "launch", payload
        )
        job_id = job.id
    body = data.model_dump(mode="json", exclude={"creative_asset_id"})
    replay = client.post("/api/v1/delivery/launches", json=body)
    assert replay.status_code == 202, replay.text
    assert replay.json()["id"] == job_id
    assert (
        client.post(
            "/api/v1/delivery/launches", json={**body, "name": "changed"}
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/v1/delivery/launches", json={**body, "request_key": "test-new"}
        ).status_code
        == 422
    )
