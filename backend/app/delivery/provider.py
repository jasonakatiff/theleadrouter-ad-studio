import json
import os
import re
from contextlib import ExitStack, contextmanager
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import requests

from app.delivery import config
from app.delivery.errors import PostingError
from app.services.facebook_service import FacebookService
from app.delivery.budget import RequestBudget, create_api, request_cost


class ProviderError(Exception):
    def __init__(
        self, message="Facebook request failed", retryable=False, retry_after=0
    ):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


def decimal_value(value, scale=6):
    try:
        result = Decimal(str(value))
        if (
            not result.is_finite()
            or result < 0
            or result >= Decimal("1e18")
            or result != result.quantize(Decimal(10) ** -scale)
        ):
            raise ValueError("Invalid reporting number")
        return result
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Invalid reporting number") from None


class DeliveryProvider(FacebookService):
    @classmethod
    def for_user(cls, db, owner_id):
        from app.models import User
        from app.services.meta_connection import resolve_meta_connection

        owner = db.get(User, owner_id) if owner_id else None
        if (
            not owner
            or not owner.is_active
            or not owner.has_permission("campaigns:write")
        ):
            raise ProviderError("Buyer access is no longer available")
        connection, token = resolve_meta_connection(db, owner_id)
        if not token or not connection["connected"]:
            raise ProviderError("Reconnect the buyer’s Meta account before retrying")
        return cls(token, connection.get("ad_account_id"))

    def __init__(self, access_token=None, ad_account_id=None):
        self.access_token = access_token
        self.ad_account_id = ad_account_id
        self.account = None
        self.budget = RequestBudget()
        self.lane = "interactive"
        self.account_scope = None
        self.api = create_api(
            access_token=self.access_token,
            app_id=os.getenv("FACEBOOK_APP_ID") or os.getenv("VITE_FACEBOOK_APP_ID"),
            app_secret=os.getenv("FACEBOOK_APP_SECRET")
            or os.getenv("VITE_FACEBOOK_APP_SECRET"),
            budget=self.budget,
        )

    def configure_budget(self, engine, account_id, lane):
        self.budget = RequestBudget(engine)
        self.account_scope, self.lane = account_id, lane
        self.api._request_budget = self.budget
        self.api._budget_account_id, self.api._budget_lane = account_id, lane

    @contextmanager
    def posting_media(self, url, video=False):
        from app.delivery.media import download_media

        with ExitStack() as stack:
            try:
                path = stack.enter_context(download_media(url, video=video))
            except ValueError:
                raise PostingError(
                    "MEDIA_INVALID",
                    "Media is inaccessible or invalid. Check the public URL, file type and size, then retry.",
                    safe_to_retry=True,
                    retry_allowed=True,
                ) from None
            except Exception:
                raise PostingError(
                    "MEDIA_DOWNLOAD",
                    "Media could not be downloaded before contacting Meta.",
                    safe_to_retry=True,
                    automatic=True,
                    retry_allowed=True,
                ) from None
            yield path

    def upload_image(self, url, ad_account_id=None):
        from facebook_business.adobjects.adimage import AdImage

        with self.posting_media(url) as path:
            image = AdImage(parent_id=ad_account_id, api=self.api)
            image[AdImage.Field.filename] = path
            image.remote_create()
            return image[AdImage.Field.hash]

    def upload_video(self, url, ad_account_id=None, wait_for_ready=False):
        from facebook_business.adobjects.advideo import AdVideo

        with self.posting_media(url, video=True) as path:
            video = AdVideo(parent_id=ad_account_id, api=self.api)
            video[AdVideo.Field.filepath] = path
            video.remote_create()
            return {"video_id": str(video["id"]), "status": "processing"}

    def video_thumbnails(self, video_id):
        data = self.read(video_id + "/thumbnails", {"fields": "uri", "limit": 1})
        return [item["uri"] for item in data.get("data", []) if item.get("uri")]

    def reconciliation_candidates(self, job):
        import time

        deadline = time.monotonic() + 120
        params = {"fields": "id,name,account_id,adset_id,creative{id,name}", "limit": 100}
        seen, matches = set(), []
        for _ in range(20):
            if time.monotonic() >= deadline:
                raise ProviderError("Ad lookup exceeded its time limit")
            data = self.read(job.payload["adset_id"] + "/ads", params)
            for ad in data.get("data", []):
                if matches_job(job, ad):
                    matches.append({"id": str(ad["id"]), "name": ad["name"]})
            if not data.get("paging", {}).get("next"):
                return matches
            cursor = data.get("paging", {}).get("cursors", {}).get("after")
            if not cursor or cursor in seen:
                raise ProviderError("Ad lookup pagination did not advance")
            seen.add(cursor)
            params["after"] = cursor
        raise ProviderError("Ad lookup exceeded its page limit")

    def read(self, path, params=None):
        return self.request("GET", path, params)

    def request(self, method, path, params=None):
        if not isinstance(path, str):
            raise ProviderError("Invalid Facebook resource path")
        resource = ""
        if path:
            match = re.fullmatch(r"(act_)?([0-9]{1,32})(?:/(ads|insights|thumbnails))?", path)
            if match is None:
                raise ProviderError("Invalid Facebook resource path")
            prefix = "act_" if match.group(1) else ""
            edge = {None: "", "ads": "/ads", "insights": "/insights", "thumbnails": "/thumbnails"}[match.group(3)]
            resource = prefix + str(int(match.group(2))) + edge
        if not self.access_token:
            raise ProviderError(
                "Facebook access token is missing; an administrator must configure it"
            )
        try:

            def send():
                return (requests.get if method == "GET" else requests.post)(
                    f"https://graph.facebook.com/{config.GRAPH_VERSION}/{resource}",
                    headers={"Authorization": "Bearer " + self.access_token},
                    **({"params": params} if method == "GET" else {"data": params}),
                    timeout=(5, 30),
                    allow_redirects=False,
                )

            budget = getattr(self, "budget", None)
            response = (
                budget.call(
                    send,
                    account_id=(
                        resource.split("/")[0]
                        if resource.startswith("act_")
                        else self.account_scope
                    ),
                    lane=self.lane,
                    cost=request_cost(params),
                )
                if budget
                else send()
            )
        except (requests.Timeout, requests.ConnectionError):
            raise ProviderError("Facebook connection failed", retryable=True) from None
        if 300 <= response.status_code < 400:
            raise ProviderError("Facebook returned an unexpected redirect")
        try:
            data = response.json()
        except ValueError:
            raise ProviderError(
                "Facebook returned an invalid response",
                retryable=response.status_code >= 500,
            ) from None
        if not response.ok or "error" in data:
            error = data.get("error", {})
            code = error.get("code")
            permanent = code in {10, 100, 190, 200, 294} or response.status_code in {
                401,
                403,
            }
            retryable = not permanent and (
                response.status_code == 429
                or response.status_code >= 500
                or error.get("is_transient") is True
                or code in {1, 2, 4, 17, 32, 613, 80004}
            )
            try:
                retry_after = max(
                    0, min(int(response.headers.get("Retry-After", "0")), 86400)
                )
            except ValueError:
                retry_after = 0
            raise ProviderError(
                (
                    "Facebook denied the request"
                    if permanent
                    else "Facebook request failed"
                ),
                retryable,
                retry_after,
            )
        return data

    def account_info(self, account_id):
        from app.delivery.cache import cached_metadata

        def load():
            data = self.read(account_id, {"fields": "id,currency,timezone_name"})
            if (
                data.get("id") != account_id
                or not isinstance(data.get("currency"), str)
                or len(data["currency"]) != 3
            ):
                raise ProviderError("Facebook returned invalid account metadata")
            ZoneInfo(data["timezone_name"])
            return data

        return cached_metadata(
            "metadata:" + account_id + ":" + config.GRAPH_VERSION,
            self.access_token,
            self.budget.engine,
            load,
        )

    def ad_status(self, ad_id):
        return self.read(
            ad_id,
            {
                "fields": "id,name,account_id,adset_id,campaign_id,effective_status,creative{id,name}"
            },
        )

    def ad_statuses(self, ad_ids):
        return self.read(
            "", {"ids": ",".join(ad_ids), "fields": "id,account_id,effective_status"}
        )

    def video_status(self, video_id):
        return self.read(video_id, {"fields": "id,status"})

    def start_report(self, account_id, since, until):
        data = self.request(
            "POST",
            account_id + "/insights",
            {
                "fields": "account_id,ad_id,date_start,date_stop,impressions,clicks,spend,actions,action_values",
                "level": "ad",
                "time_increment": 1,
                "time_range": json.dumps(
                    {"since": since.isoformat(), "until": until.isoformat()}
                ),
                "action_attribution_windows": json.dumps(["7d_click", "1d_view"]),
                "action_report_time": "conversion",
            },
        )
        report_id = str(data.get("report_run_id", ""))
        if not report_id.isdigit():
            raise ProviderError("Facebook did not return a report identity")
        return report_id

    def report_status(self, report_id):
        return self.read(
            report_id, {"fields": "id,async_status,async_percent_completion"}
        )

    def report_page(self, report_id, cursor=None):
        params = {"limit": config.REPORT_PAGE_SIZE}
        if cursor:
            params["after"] = cursor
        data = self.read(report_id + "/insights", params)
        if not isinstance(data.get("data"), list):
            raise ProviderError("Facebook report page is malformed")
        after = data.get("paging", {}).get("cursors", {}).get("after")
        more = bool(data.get("paging", {}).get("next"))
        if more and (not isinstance(after, str) or not after or after == cursor):
            raise ProviderError("Facebook report pagination did not advance")
        return data["data"], after if more else None


def job_marker(job):
    """The launch marker used to recognise a job's own objects on Meta.

    It lives on the creative name, never the ad name: ad names are reported as
    ad_name and feed buyers' tracking, so they must stay exactly what was typed.
    """
    return job.name + " [bw:" + job.id + "]"


def matches_job(job, remote):
    creative = job.results.get("creative_id") or job.payload.get("creative_id")
    return (
        str(remote.get("account_id")).removeprefix("act_")
        == job.account_id.removeprefix("act_")
        and str(remote.get("adset_id")) == job.payload["adset_id"]
        and str(remote.get("creative", {}).get("id")) == creative
        and remote.get("creative", {}).get("name") == job_marker(job)
    )
