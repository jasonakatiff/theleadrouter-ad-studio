from app.telemetry.runtime import capture_exception
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status, Header, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from typing import Dict, Any, Optional
from app.services.facebook_service import FacebookService, FacebookConnectionError, resolve_facebook_service
from app.models import FacebookAd, FacebookAdSet, FacebookCampaign, MetaAdsConnection, User
from app.delivery.schemas import AdRequest
from app.delivery.docs import DeliveryJobResult, LegacyDeliveryConflict
from app.database import get_db
from app.core.deps import get_current_active_user, require_permission
from app.core.config import settings
from app.core.oauth_state import create_oauth_state, verify_oauth_state, set_oauth_state_cookie, get_oauth_state_cookie_name
from app.core.token_encryption import encrypt_token
from app.services.meta_ads_oauth import build_oauth_url, exchange_code, list_ad_accounts, MetaOAuthError
from sqlalchemy.orm import Session
from app.services.campaign_validation import money_to_minor
from app.services.meta_connection import resolve_meta_connection, personal_meta_connection

from app.delivery.budget import RequestDeferred, now_utc

router = APIRouter()

def raise_if_budget_busy(error):
    if isinstance(error, RequestDeferred):
        if error.may_have_written:
            raise HTTPException(status_code=409, detail={"error": {
                "code": "WRITE_NEEDS_RECONCILIATION",
                "message": "Facebook preparation stopped after a write; verify its result before retrying",
                "details": None,
            }})
        raise HTTPException(
            status_code=429,
            detail={"error": {
                "code": "REQUEST_BUDGET_BUSY", "message": str(error), "details": None,
            }},
            headers={"Retry-After": str(max(1, int((error.until - now_utc()).total_seconds()) + 1))},
        )


PROVIDER = "meta-ads"
from app.api.v1.campaign_settings import router as settings_router
router.include_router(settings_router)


class SelectMetaAccountRequest(BaseModel):
    ad_account_id: str


def _require_meta_configured():
    if not settings.facebook_ads_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Meta Ads OAuth is not configured on this server.",
        )


def get_facebook_service(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    try:
        return resolve_facebook_service(db, current_user.id)
    except FacebookConnectionError as exc:
        capture_exception(exc, "facebook.get_facebook_service")
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except Exception as exc:
        capture_exception(exc, "facebook.get_facebook_service")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/oauth/start")
async def start_meta_oauth(
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_active_user),
):
    _require_meta_configured()
    state_token = create_oauth_state(current_user.id, PROVIDER)
    set_oauth_state_cookie(response, state_token, secure=request.url.scheme == "https")
    return {"oauth_url": build_oauth_url(settings.FACEBOOK_APP_ID, settings.FACEBOOK_OAUTH_REDIRECT_URI, state_token)}


@router.get("/oauth/callback")
async def meta_oauth_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
    db: Session = Depends(get_db),
):
    if error:
        raise HTTPException(status_code=400, detail=error_description or f"Meta OAuth error: {error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing Meta OAuth authorization code or state")
    state_cookie = request.cookies.get(get_oauth_state_cookie_name())
    if not state_cookie:
        raise HTTPException(status_code=400, detail="Missing OAuth state cookie")
    if state_cookie != state:
        raise HTTPException(status_code=400, detail="OAuth state mismatch between cookie and callback parameter")
    try:
        user_id = verify_oauth_state(state, PROVIDER)
        token_data = await exchange_code(
            code,
            settings.FACEBOOK_APP_ID,
            settings.FACEBOOK_APP_SECRET,
            settings.FACEBOOK_OAUTH_REDIRECT_URI,
        )
        accounts = await list_ad_accounts(token_data["access_token"])
    except (ValueError, MetaOAuthError) as exc:
        capture_exception(exc, "facebook.meta_oauth_callback")
        raise HTTPException(status_code=400, detail=str(exc))
    if not accounts:
        raise HTTPException(status_code=400, detail="No Meta ad accounts are available for this login.")

    encrypted_token = encrypt_token(token_data["access_token"])
    expires_in = token_data.get("expires_in")
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in) if isinstance(expires_in, (int, float)) else None
    select_required = len(accounts) > 1
    db.query(MetaAdsConnection).filter(MetaAdsConnection.user_id == user_id).update(
        {MetaAdsConnection.is_active: False}, synchronize_session="fetch"
    )
    for account in accounts:
        account_id = str(account.get("id") or "")
        if not account_id:
            continue
        connection = db.query(MetaAdsConnection).filter(
            MetaAdsConnection.user_id == user_id,
            MetaAdsConnection.ad_account_id == account_id,
        ).first()
        if not connection:
            connection = MetaAdsConnection(user_id=user_id, ad_account_id=account_id, encrypted_access_token=encrypted_token)
            db.add(connection)
        connection.account_name = account.get("name")
        connection.encrypted_access_token = encrypted_token
        connection.access_token_expires_at = expires_at
        connection.is_active = not select_required
    db.commit()

    redirect = RedirectResponse(
        url=f"{settings.FRONTEND_URL.rstrip('/')}/facebook-campaigns?{'select=1' if select_required else 'connected=1'}"
    )
    return redirect


@router.get("/connection")
def meta_connection_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    public, _token = resolve_meta_connection(db, current_user.id)
    return {**public, "oauth_available": settings.facebook_ads_enabled}


@router.get("/connections")
def meta_connections(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    connections = db.query(MetaAdsConnection).filter(
        MetaAdsConnection.user_id == current_user.id,
    ).order_by(MetaAdsConnection.ad_account_id).all()
    return {"connections": [
        {
            "ad_account_id": connection.ad_account_id,
            "account_name": connection.account_name,
            "selected": connection.is_active,
        }
        for connection in connections
    ]}


@router.post("/connection/select")
def select_meta_connection(
    body: SelectMetaAccountRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    account_id = body.ad_account_id if body.ad_account_id.startswith("act_") else f"act_{body.ad_account_id}"
    connection = db.query(MetaAdsConnection).filter(
        MetaAdsConnection.user_id == current_user.id,
        MetaAdsConnection.ad_account_id == account_id,
    ).first()
    if not connection:
        raise HTTPException(status_code=404, detail="Meta ad account is not available for this user.")
    public, _token = personal_meta_connection(connection)
    if not public["connected"]:
        raise HTTPException(
            status_code=409 if public["state"] == "expired" else 503,
            detail=public["error"]["message"],
        )
    db.query(MetaAdsConnection).filter(MetaAdsConnection.user_id == current_user.id).update(
        {MetaAdsConnection.is_active: False}, synchronize_session="fetch"
    )
    connection.is_active = True
    db.commit()
    return meta_connection_status(db, current_user)


@router.delete("/connection")
def disconnect_meta_connection(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    db.query(MetaAdsConnection).filter(MetaAdsConnection.user_id == current_user.id).update(
        {MetaAdsConnection.is_active: False}, synchronize_session="fetch"
    )
    db.commit()
    return {"message": "Disconnected"}

@router.get("/accounts")
def get_ad_accounts(
    refresh: bool = False,
    service: FacebookService = Depends(get_facebook_service),
    current_user: User = Depends(require_permission("campaigns:read"))
):
    try:
        return service.get_ad_accounts(force_refresh=refresh)
    except Exception as e:
        raise_if_budget_busy(e)
        capture_exception(e, "facebook.get_ad_accounts")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/campaigns")
def read_campaigns(
    ad_account_id: Optional[str] = None,
    service: FacebookService = Depends(get_facebook_service),
    current_user: User = Depends(require_permission("campaigns:read"))
):
    try:
        campaigns = service.get_campaigns(ad_account_id)
        # Convert FB objects to dicts
        return [dict(c) for c in campaigns]
    except Exception as e:
        raise_if_budget_busy(e)
        capture_exception(e, "facebook.read_campaigns")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/campaigns")
def create_campaign(
    campaign: Dict[str, Any],
    ad_account_id: Optional[str] = None,
    service: FacebookService = Depends(get_facebook_service),
    current_user: User = Depends(require_permission("campaigns:write"))
):
    try:
        # Check if ad_account_id is in query or body (body takes precedence if we structured it that way, but here we use query or separate param)
        # For POST, usually better to have it in the body or query. Let's support query for consistency with GET
        result = service.create_campaign(campaign, ad_account_id)
        return dict(result)
    except ValueError as e:
        capture_exception(e, "facebook.create_campaign")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise_if_budget_busy(e)
        capture_exception(e, "facebook.create_campaign")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/pixels")
def read_pixels(
    ad_account_id: Optional[str] = None,
    service: FacebookService = Depends(get_facebook_service),
    current_user: User = Depends(require_permission("campaigns:read"))
):
    try:
        pixels = service.get_pixels(ad_account_id)
        # Convert FB objects to dicts
        return [dict(p) for p in pixels]
    except Exception as e:
        raise_if_budget_busy(e)
        capture_exception(e, "facebook.read_pixels")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/pages")
def read_pages(
    ad_account_id: Optional[str] = None,
    service: FacebookService = Depends(get_facebook_service),
    current_user: User = Depends(require_permission("campaigns:read"))
):
    try:
        pages = service.get_pages(ad_account_id)
        return pages
    except Exception as e:
        raise_if_budget_busy(e)
        capture_exception(e, "facebook.read_pages")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/adsets")
def read_adsets(
    ad_account_id: Optional[str] = None,
    campaign_id: Optional[str] = None,
    service: FacebookService = Depends(get_facebook_service),
    current_user: User = Depends(get_current_active_user)
):
    try:
        adsets = service.get_adsets(ad_account_id, campaign_id)
        return [dict(a) for a in adsets]
    except Exception as e:
        raise_if_budget_busy(e)
        capture_exception(e, "facebook.read_adsets")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/adsets")
def create_adset(
    adset: Dict[str, Any],
    ad_account_id: Optional[str] = None,
    service: FacebookService = Depends(get_facebook_service),
    current_user: User = Depends(require_permission("campaigns:write"))
):
    try:
        result = service.create_adset(adset, ad_account_id)
        return dict(result)
    except ValueError as e:
        capture_exception(e, "facebook.create_adset")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise_if_budget_busy(e)
        capture_exception(e, "facebook.create_adset")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/creatives")
def create_creative(
    creative: Dict[str, Any],
    ad_account_id: Optional[str] = None,
    service: FacebookService = Depends(get_facebook_service),
    current_user: User = Depends(require_permission("campaigns:write"))
):
    try:
        result = service.create_creative(creative, ad_account_id)
        return dict(result)
    except ValueError as e:
        capture_exception(e, "facebook.create_creative")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise_if_budget_busy(e)
        capture_exception(e, "facebook.create_creative")
        raise HTTPException(status_code=500, detail=str(e))

@router.post(
    "/ads", status_code=202,
    responses={202: {"model": DeliveryJobResult}, 409: {"model": LegacyDeliveryConflict}},
    description="Requires campaigns:write. Queue final ad creation using an existing Meta "
    "ad set and creative. Requires ad_account_id and an Idempotency-Key unique per buyer "
    "across delivery submissions. Reuse the same key/payload after a lost response. "
    "Returns a job, not a Meta ad ID; poll GET /api/v1/delivery/jobs/{id}. "
    "PAUSED is the default; ACTIVE is allowed. Legacy errors retain their detail wrapper.",
)
def create_ad(
    ad: "AdRequest",
    ad_account_id: str = Query(..., pattern=r"^(act_)?[0-9]+$"),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=160),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("campaigns:write"))
):
    from app.delivery.queue import enqueue
    from app.delivery.api import job_json, problem
    account_id = ad_account_id if ad_account_id.startswith('act_') else 'act_' + ad_account_id
    try:
        return job_json(enqueue(db, current_user.id, idempotency_key, account_id, 'ad', ad.model_dump()))
    except ValueError as error:
        db.rollback()
        problem(409, 'SUBMISSION_CONFLICT', str(error))


@router.get("/ads")
def read_ads(
    adset_id: str,
    service: FacebookService = Depends(get_facebook_service),
    current_user: User = Depends(get_current_active_user)
):
    try:
        ads = service.get_ads(adset_id)
        return [dict(a) for a in ads]
    except Exception as e:
        raise_if_budget_busy(e)
        capture_exception(e, "facebook.read_ads")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/insights")
def read_insights(
    ad_account_id: Optional[str] = None,
    level: str = "campaign",
    date_preset: str = "last_30d",
    since: Optional[str] = None,
    until: Optional[str] = None,
    breakdown: Optional[str] = None,
    service: FacebookService = Depends(get_facebook_service),
    current_user: User = Depends(get_current_active_user),
):
    """Return Facebook Marketing API insights (spend, impressions, CTR, ROAS, etc.).

    Pass either date_preset (today/yesterday/last_7d/last_30d/last_90d/maximum)
    or since+until (YYYY-MM-DD) for a custom range. Level is account/campaign/adset/ad.
    """
    try:
        time_range = None
        if since and until:
            time_range = {"since": since, "until": until}
        rows = service.get_insights(
            ad_account_id=ad_account_id,
            level=level,
            date_preset=date_preset,
            time_range=time_range,
            breakdown=breakdown,
        )
        return rows
    except Exception as e:
        raise_if_budget_busy(e)
        capture_exception(e, "facebook.read_insights")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/campaigns/save")
def save_campaign_locally(
    campaign_data: Dict[str, Any],
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("campaigns:write"))
):
    try:
        # Check if exists
        existing = db.query(FacebookCampaign).filter(FacebookCampaign.id == campaign_data.get('id')).first()
        if existing:
            return {"message": "Campaign already exists", "id": existing.id}

        # Handle daily_budget casting
        daily_budget = campaign_data.get('dailyBudget')
        daily_budget_minor = money_to_minor(daily_budget) if daily_budget not in (None, '') else None
        daily_budget = daily_budget_minor // 100 if daily_budget_minor is not None else None

        new_campaign = FacebookCampaign(
            created_by_id=current_user.id,
            id=campaign_data.get('id'),
            name=campaign_data.get('name'),
            objective=campaign_data.get('objective'),
            budget_type=campaign_data.get('budgetType', 'ABO'),
            daily_budget=daily_budget,
            daily_budget_minor=daily_budget_minor,
            bid_strategy=campaign_data.get('bidStrategy'),
            status=campaign_data.get('status'),
            fb_campaign_id=campaign_data.get('fbCampaignId')
        )
        db.add(new_campaign)
        db.commit()
        db.refresh(new_campaign)
        return {"message": "Campaign saved locally", "id": new_campaign.id}
    except Exception as e:
        raise_if_budget_busy(e)
        capture_exception(e, "facebook.save_campaign_locally")
        db.rollback()
        print(f"Error saving campaign locally: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/adsets/save")
def save_adset_locally(
    adset_data: Dict[str, Any],
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("campaigns:write"))
):
    try:
        # Check if exists
        existing = db.query(FacebookAdSet).filter(FacebookAdSet.id == adset_data.get('id')).first()
        if existing:
            return {"message": "AdSet already exists", "id": existing.id}

        # Ensure campaign exists (FK check)
        campaign_id = adset_data.get('campaignId')
        if not campaign_id:
             raise HTTPException(status_code=400, detail="campaignId is required")

        # We assume campaign is already saved by the frontend calling /campaigns/save first

        # Handle numeric fields casting
        daily_budget = adset_data.get('dailyBudget')
        daily_budget_minor = money_to_minor(daily_budget) if daily_budget not in (None, '') else None
        daily_budget = daily_budget_minor // 100 if daily_budget_minor is not None else None

        bid_amount = adset_data.get('bidAmount')
        bid_amount_minor = money_to_minor(bid_amount) if bid_amount not in (None, '') else None
        bid_amount = bid_amount_minor // 100 if bid_amount_minor is not None else None

        new_adset = FacebookAdSet(
            created_by_id=current_user.id,
            id=adset_data.get('id'),
            campaign_id=campaign_id,
            name=adset_data.get('name'),
            optimization_goal=adset_data.get('optimizationGoal'),
            daily_budget=daily_budget,
            daily_budget_minor=daily_budget_minor,
            bid_strategy=adset_data.get('bidStrategy'),
            bid_amount=bid_amount,
            bid_amount_minor=bid_amount_minor,
            targeting=adset_data.get('targeting'),
            pixel_id=adset_data.get('pixelId'),
            conversion_event=adset_data.get('conversionEvent'),
            status=adset_data.get('status'),
            fb_adset_id=adset_data.get('fbAdsetId')
        )
        db.add(new_adset)
        db.commit()
        db.refresh(new_adset)
        return {"message": "AdSet saved locally", "id": new_adset.id}
    except Exception as e:
        raise_if_budget_busy(e)
        capture_exception(e, "facebook.save_adset_locally")
        db.rollback()
        print(f"Error saving adset locally: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/ads/save")
def save_ad_locally(
    ad_data: Dict[str, Any],
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("campaigns:write"))
):
    try:
        from sqlalchemy import text
        from app.delivery.models import ManagedAd
        fb_ad_id = ad_data.get('fbAdId')
        identity = fb_ad_id or ad_data.get('id')
        if not identity:
            raise HTTPException(status_code=422, detail="An ad identity is required")
        db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:identity, 195558004))'), {'identity': identity})
        managed = db.query(ManagedAd).filter(ManagedAd.fb_ad_id == fb_ad_id).first() if fb_ad_id else None
        if managed and managed.owner_id != current_user.id and not current_user.has_role('admin'):
            raise HTTPException(status_code=403, detail="Ad belongs to another buyer")
        existing = db.query(FacebookAd).filter(FacebookAd.fb_ad_id == fb_ad_id).first() if fb_ad_id else db.get(FacebookAd, ad_data.get('id'))
        if existing:
            return {"message": "Ad already saved locally", "id": existing.id}

        new_ad = FacebookAd(
            created_by_id=current_user.id,
            id=ad_data.get('id'),
            adset_id=ad_data.get('adsetId'),
            name=ad_data.get('name'),
            creative_name=ad_data.get('creativeName'),
            image_url=ad_data.get('imageUrl'),
            # Video support fields
            media_type=ad_data.get('mediaType', 'image'),
            video_url=ad_data.get('videoUrl'),
            video_id=ad_data.get('videoId'),
            thumbnail_url=ad_data.get('thumbnailUrl'),
            bodies=ad_data.get('bodies'),
            headlines=ad_data.get('headlines'),
            description=ad_data.get('description'),
            cta=ad_data.get('cta'),
            website_url=ad_data.get('websiteUrl'),
            status=ad_data.get('status'),
            fb_ad_id=ad_data.get('fbAdId'),
            fb_creative_id=ad_data.get('fbCreativeId')
        )
        db.add(new_ad)
        db.flush()
        if managed:
            managed.local_ad_id = new_ad.id
        db.commit()
        db.refresh(new_ad)
        return {"message": "Ad saved locally", "id": new_ad.id}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        raise_if_budget_busy(e)
        capture_exception(e, "facebook.save_ad_locally")
        db.rollback()
        print(f"Error saving ad locally: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/upload-image")
def upload_image(
    data: Dict[str, str],
    ad_account_id: Optional[str] = None,
    service: FacebookService = Depends(get_facebook_service),
    current_user: User = Depends(require_permission("campaigns:write"))
):
    try:
        image_url = data.get("image_url")
        if not image_url:
            raise HTTPException(status_code=400, detail="image_url is required")
        image_hash = service.upload_image(image_url, ad_account_id)
        return {"image_hash": image_hash}
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    except Exception as e:
        raise_if_budget_busy(e)
        capture_exception(e, "facebook.upload_image")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/upload-video")
def upload_video(
    data: Dict[str, Any],
    ad_account_id: Optional[str] = None,
    service: FacebookService = Depends(get_facebook_service),
    current_user: User = Depends(require_permission("campaigns:write"))
):
    """Upload a video to Facebook Ad Library.

    Request body:
        video_url: URL of the video to upload
        wait_for_ready: Whether to wait for processing (default True)
        timeout: Max seconds to wait (default 600)

    Returns:
        video_id: Facebook video ID
        status: 'processing', 'ready', or 'error'
        thumbnails: List of auto-generated thumbnail URLs (if ready)
    """
    video_url = data.get("video_url")
    if not video_url:
        raise HTTPException(status_code=400, detail="video_url is required")

    try:
        wait_for_ready = data.get("wait_for_ready", True)
        timeout = data.get("timeout", 600)

        result = service.upload_video(
            video_url,
            ad_account_id,
            wait_for_ready=wait_for_ready,
            timeout=timeout
        )
        return result
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    except Exception as e:
        raise_if_budget_busy(e)
        capture_exception(e, "facebook.upload_video")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/video-status/{video_id}")
def get_video_status(
    video_id: str,
    service: FacebookService = Depends(get_facebook_service),
    current_user: User = Depends(get_current_active_user)
):
    """Check the processing status of a video.

    Returns:
        status: 'processing', 'ready', or 'error'
        video_id: The video ID
        length: Video duration in seconds (if ready)
    """
    try:
        return service.get_video_status(video_id)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    except Exception as e:
        raise_if_budget_busy(e)
        capture_exception(e, "facebook.get_video_status")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/video-thumbnails/{video_id}")
def get_video_thumbnails(
    video_id: str,
    service: FacebookService = Depends(get_facebook_service),
    current_user: User = Depends(get_current_active_user)
):
    """Get auto-generated thumbnails for a video.

    Returns:
        thumbnails: List of thumbnail URLs
    """
    try:
        thumbnails = service.get_video_thumbnails(video_id)
        return {"thumbnails": thumbnails}
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    except Exception as e:
        raise_if_budget_busy(e)
        capture_exception(e, "facebook.get_video_thumbnails")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/locations/search")
def search_locations(
    q: str,
    type: str = "city",
    limit: int = 10,
    ad_account_id: Optional[str] = None,
    service: FacebookService = Depends(get_facebook_service),
    current_user: User = Depends(get_current_active_user)
):
    try:
        locations = service.search_locations(q, type, limit, ad_account_id)
        return [dict(loc) for loc in locations]
    except Exception as e:
        raise_if_budget_busy(e)
        capture_exception(e, "facebook.search_locations")
        raise HTTPException(status_code=500, detail=str(e))


@router.get('/custom-audiences')
def read_custom_audiences(ad_account_id: str, service: FacebookService = Depends(get_facebook_service), current_user: User = Depends(get_current_active_user)):
    try:
        return service.get_custom_audiences(ad_account_id)
    except Exception as error:
        capture_exception(error, "facebook.read_custom_audiences")
        raise HTTPException(status_code=502, detail=str(error))


@router.get('/instagram-accounts')
def read_instagram_accounts(ad_account_id: str, service: FacebookService = Depends(get_facebook_service), current_user: User = Depends(get_current_active_user)):
    try:
        return service.get_instagram_accounts(ad_account_id)
    except Exception as error:
        capture_exception(error, "facebook.read_instagram_accounts")
        raise HTTPException(status_code=502, detail=str(error))
