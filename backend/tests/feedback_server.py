"""Local browser fixture: real application/PostgreSQL, mocked external Meta transport.

Run only with DATABASE_URL pointing to a localhost test database and
TEST_EMAIL / TEST_PASSWORD set. No production API routes are replaced.
"""

import json
import os
from urllib.parse import urlparse
from facebook_business.api import FacebookAdsApi, FacebookResponse

database = urlparse(os.environ["DATABASE_URL"])
if database.hostname not in ("127.0.0.1", "localhost") or not database.path.startswith(
    "/test_"
):
    raise RuntimeError("Browser fixture requires a localhost test database.")

meta_calls = []
meta_objects = {}
account = {
    "id": "act_123",
    "account_id": "123",
    "name": "test-feedback-account",
    "account_status": 1,
    "currency": "USD",
    "timezone_name": "America/New_York",
    "min_daily_budget": 500,
}


def meta_transport(self, method, path, params=None, **kwargs):
    path = "/".join(path) if isinstance(path, (tuple, list)) else str(path)
    params = params or {}
    meta_calls.append({"method": method, "path": path, "params": params})
    edge = path.rstrip("/").split("/")[-1]
    if method == "POST":
        object_id = str(9000 + len(meta_objects))
        meta_objects[object_id] = {"id": object_id, **params, "account_id": "123"}
        if edge == "campaigns" and params.get("name", "").startswith("test-interrupt"):
            raise ConnectionError("test-Meta connection lost after creating campaign")
        if edge == "adimages":
            result = {"images": {"test-creative.png": {"hash": "test-image-hash"}}}
        else:
            result = {"id": object_id}
    elif edge == "adaccounts":
        result = {"data": [account]}
    elif edge == "act_123":
        result = account
    elif edge in ("promote_pages", "accounts"):
        result = {
            "data": [
                {"id": "111", "name": "test-Zulu Page"},
                {"id": "112", "name": "test-Alpha Page"},
            ]
        }
    elif edge == "instagram_accounts":
        result = {"data": [{"id": "222", "username": "test_brand"}]}
    elif edge == "adspixels":
        result = {"data": [{"id": "333", "name": "test-Pixel"}]}
    elif edge == "customaudiences":
        result = {
            "data": [
                {"id": "444", "name": "test-Lookalike", "subtype": "LOOKALIKE"},
                {"id": "445", "name": "test-Customers", "subtype": "CUSTOM"},
            ]
        }
    elif edge == "search" and params.get("type") == "adgeolocation":
        result = {
            "data": [
                {
                    "key": "3843",
                    "name": "California",
                    "type": "region",
                    "country_code": "US",
                    "country_name": "United States",
                }
            ]
        }
    elif edge in ("campaigns", "adsets"):
        result = {"data": []}
    elif edge in meta_objects:
        result = meta_objects[edge]
    else:
        raise AssertionError(f"Unexpected external Meta request: {method} {path}")
    return FacebookResponse(body=json.dumps(result), http_status=200, headers={})


FacebookAdsApi.call = meta_transport

from app.main import app
from app.core.rate_limit import limiter
from app.database import Base, engine, SessionLocal
from app.models import User
from app.core.security import get_password_hash

# This fixture is restricted above to localhost test databases.
limiter.enabled = False

Base.metadata.create_all(engine)
with SessionLocal() as db:
    user = db.query(User).filter_by(email=os.environ["TEST_EMAIL"]).first()
    if not user:
        db.add(
            User(
                email=os.environ["TEST_EMAIL"],
                name="test-feedback-user",
                hashed_password=get_password_hash(os.environ["TEST_PASSWORD"]),
                is_active=True,
                is_superuser=True,
            )
        )
        db.commit()


@app.get("/test-meta-calls")
def recorded_meta_calls():
    return meta_calls
