"""Isolated browser QA only: real app/DB, simulated Meta. Never uses provider credentials."""

import os
import sys
import threading
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).parents[2]))
target = urlsplit(os.environ.get("DATABASE_URL", ""))
if (
    target.hostname not in {"localhost", "127.0.0.1"}
    or not target.path.startswith("/test_delivery_browser_")
    or os.environ.get("DELIVERY_DEMO") != "1"
):
    raise RuntimeError(
        "Demo requires explicit DELIVERY_DEMO=1 and isolated test_delivery_browser_* localhost database"
    )
if os.environ.get("DELIVERY_WORKER_ENABLED") != "false":
    raise RuntimeError("Real provider workers must be disabled for the demo")

from app.main import app
from app.database import Base, engine, SessionLocal
from app.models import User, Role, Permission, FacebookCampaign, FacebookAdSet
from app.core.security import get_password_hash
from app.delivery.models import DeliverySettings
from app.delivery.queue import posting_tick
from app.delivery.sync import sync_tick
from app.delivery import api
from app.delivery.provider import matches_job
from app.creatives import api as creatives_api
from app.creatives.models import CreativeAsset
from app.creatives.schemas import CreativeMetadata
from app.models import GeneratedAd
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

ads = {}
creatives = {}
serial = 910000
reports = {}

failed_once = set()


class FakeProvider:
    @classmethod
    def for_user(cls, db, owner_id):
        return cls()

    def upload_image(self, *args):
        return "test-image-hash"

    def upload_video(self, *args, **kwargs):
        return {"video_id": "912345", "status": "processing"}

    def video_status(self, *args):
        return {"status": {"video_status": "ready"}}

    def video_thumbnails(self, *args):
        return ["https://example.com/test-thumbnail.jpg"]

    def create_creative(self, data, account_id):
        global serial
        serial += 1
        identity = str(serial)
        creatives[identity] = {"id": identity, "name": data["name"]}
        return {"id": identity}

    def create_ad(self, data, account_id):
        global serial
        if (
            data["name"].startswith(("test-retry-", "test-rate-retry-"))
            and data["name"] not in failed_once
        ):
            from facebook_business.exceptions import FacebookRequestError

            failed_once.add(data["name"])
            code = 190 if data["name"].startswith("test-retry-") else 4
            raise FacebookRequestError(
                "test-simulated-rejection", {}, 400, {}, {"error": {"code": code}}
            )
        serial += 1
        identity = str(serial)
        ads[identity] = {
            "id": identity,
            "name": data["name"],
            "account_id": account_id.removeprefix("act_"),
            "adset_id": data["adset_id"],
            "creative": creatives.get(
                data["creative_id"], {"id": data["creative_id"], "name": ""}
            ),
            "effective_status": "PAUSED",
        }
        if data["name"].startswith("test-timeout"):
            raise TimeoutError("test-simulated-lost-response")
        return {"id": identity}

    def account_info(self, account_id):
        return {"id": account_id, "currency": "USD", "timezone_name": "UTC"}

    def ad_status(self, ad_id):
        return ads[ad_id]

    def ad_statuses(self, ad_ids):
        return {ad_id: ads[ad_id] for ad_id in ad_ids}

    def start_report(self, account_id, since, until):
        identity = str(990000 + len(reports))
        reports[identity] = (account_id, since, until)
        return identity

    def report_status(self, report_id):
        return {"id": report_id, "async_status": "Job Completed"}

    def report_page(self, report_id, cursor=None):
        account_id, since, until = reports[report_id]
        return (
            [
                {
                    "account_id": account_id,
                    "ad_id": ad_id,
                    "date_start": until.isoformat(),
                    "date_stop": until.isoformat(),
                    "impressions": "100",
                    "clicks": "3",
                    "spend": "0.29",
                    "actions": [],
                }
                for ad_id, ad in ads.items()
                if "act_" + ad["account_id"] == account_id
            ],
            None,
        )

    def reconciliation_candidates(self, job):
        return [
            {"id": ad["id"], "name": ad["name"]}
            for ad in ads.values()
            if matches_job(job, ad)
        ]


api.DeliveryProvider = FakeProvider
app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.environ["DELIVERY_DEMO_FRONTEND"]],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)
Base.metadata.create_all(engine)
with SessionLocal() as db:
    permission = Permission(id="test-delivery-write", name="campaigns:write")
    role = Role(id="test-delivery-buyer-role", name="test-delivery-buyer")
    role.permissions = [permission]
    admin = User(
        id="test-delivery-admin",
        email="test-delivery-admin@example.com",
        name="test-delivery-admin",
        hashed_password=get_password_hash("test-delivery-password"),
        is_active=True,
        is_superuser=True,
    )
    buyer = User(
        id="test-delivery-buyer",
        email="test-delivery-buyer@example.com",
        name="test-delivery-buyer",
        hashed_password=get_password_hash("test-delivery-password"),
        is_active=True,
        roles=[role],
    )
    db.add_all(
        [
            admin,
            buyer,
            DeliverySettings(id=1),
            FacebookCampaign(
                id="test-delivery-campaign",
                name="test-delivery-campaign",
                objective="OUTCOME_SALES",
                budget_type="ABO",
                fb_campaign_id="911111",
            ),
        ]
    )
    db.flush()
    db.add(
        CreativeAsset(
            id="test-delivery-legacy-asset",
            source_type="external_upload",
            created_by_id=admin.id,
            registered_by_id=admin.id,
            name="test-delivery-legacy-asset",
            media_url="https://example.com/test.png",
            media_type="image",
            analysis_status="ready",
            metadata_revision=1,
            metadata_values=CreativeMetadata().model_dump(),
        )
    )
    db.add(
        FacebookAdSet(
            id="test-delivery-adset",
            campaign_id="test-delivery-campaign",
            name="test-delivery-adset",
            optimization_goal="OFFSITE_CONVERSIONS",
            fb_adset_id="912222",
        )
    )
    db.commit()

if os.environ.get("CREATIVE_DEMO") == "1":
    from app.api.v1 import facebook as facebook_api
    from fastapi.responses import Response
    from uuid import uuid4
    import base64

    media = {}
    sample = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jA1sAAAAASUVORK5CYII="
    )
    media["test-generated.png"] = (sample, "image/png")
    media_root = os.environ["DELIVERY_DEMO_FRONTEND"] + "/api/v1/test-creative-media/"

    @app.get("/api/v1/test-creative-media/{name}")
    def test_creative_media(name: str):
        content, mime = media[name]
        return Response(content, media_type=mime)

    def fake_store(file):
        # Only this isolated harness bypasses R2; production validation has unit coverage.
        if Path(file.filename).suffix.lower() not in creatives_api.MEDIA_TYPES:
            creatives_api.problem(
                422, "MEDIA_TYPE", "Select a supported image or video"
            )
        name = "test-" + str(uuid4()) + Path(file.filename).suffix
        media[name] = (file.file.read(), file.content_type)
        return media_root + name

    creatives_api.store_upload = fake_store
    creatives_api.analyze_media = lambda *args: CreativeMetadata(
        talent_type="single_presenter",
        background="studio",
        camera_angle="eye level",
        lighting="soft",
        color_scheme="warm",
        visual_style="testimonial",
        messaging_angle="product demonstration",
        composition="centered",
    ).model_dump()
    FakeProvider.video_thumbnails = lambda *args: [media_root + "test-generated.png"]

    class FakeFacebook:
        def get_ad_accounts(self):
            return [
                {
                    "id": "act_919999",
                    "account_id": "919999",
                    "name": "test-creative-account",
                    "currency": "USD",
                    "account_status": 1,
                }
            ]

        def get_campaigns(self, *args, **kwargs):
            return [
                {
                    "id": "911111",
                    "name": "test-creative-campaign",
                    "objective": "OUTCOME_SALES",
                    "status": "PAUSED",
                }
            ]

        def get_adsets(self, *args, **kwargs):
            return [
                {
                    "id": "912222",
                    "name": "test-creative-adset",
                    "optimization_goal": "OFFSITE_CONVERSIONS",
                    "status": "PAUSED",
                    "daily_budget": "1000",
                }
            ]

        def get_pages(self, *args, **kwargs):
            return [{"id": "919888", "name": "test-creative-page"}]

        def get_pixels(self, *args, **kwargs):
            return []

    app.dependency_overrides[facebook_api.get_facebook_service] = FakeFacebook
    with SessionLocal() as db:
        from app.creatives.service import register_generated

        generated = GeneratedAd(
            id="test-generated-creative",
            headline="test-generated-creative",
            image_url=media_root + "test-generated.png",
            created_by_id="test-delivery-admin",
        )
        db.add(generated)
        db.flush()
        asset = register_generated(db, generated, "test-delivery-admin")
        asset.analysis_status = "ready"
        asset.metadata_revision = 1
        asset.metadata_values = CreativeMetadata(
            lighting="natural", background="outdoors"
        ).model_dump()
        db.commit()

stop = threading.Event()
threads = []


@app.on_event("startup")
def start_demo_workers():
    def run(tick):
        while not stop.is_set():
            tick(engine, FakeProvider)
            stop.wait(0.1)

    for tick in [posting_tick, sync_tick]:
        thread = threading.Thread(target=run, args=(tick,), daemon=True)
        threads.append(thread)
        thread.start()


@app.on_event("shutdown")
def stop_demo_workers():
    stop.set()
    for thread in threads:
        thread.join(timeout=5)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ["DELIVERY_DEMO_PORT"]))
