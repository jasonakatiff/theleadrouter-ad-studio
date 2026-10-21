from app.telemetry.runtime import capture_exception
from pathlib import Path
from typing import Literal, Optional
from uuid import uuid4
import shutil

from fastapi import APIRouter, Depends, File, UploadFile, Query
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.core.deps import get_current_active_user, require_permission
from app.database import get_db
from app.models import GeneratedAd, User
from app.delivery.api import DeliveryRoute, page, problem
from app.creatives.analysis import analyze_media, failure_message, MODEL
from app.creatives.models import CreativeAsset, CreativeEvent
from app.creatives.schemas import CreativeMetadata, MetadataEdit
from app.creatives.service import event, lock_asset, register_generated, now

router = APIRouter(route_class=DeliveryRoute)
MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".avi": "video/x-msvideo",
}


def store_upload(file):
    from app.api.v1.uploads import get_s3_client, UPLOAD_DIR
    from app.core.config import settings

    extension = Path(file.filename or "").suffix.lower()
    mime = MEDIA_TYPES.get(extension)
    if not mime or file.content_type != mime:
        problem(
            422,
            "MEDIA_TYPE",
            "Select a supported image or video with a matching file type",
        )
    maximum = (500 if mime.startswith("video/") else 10) * 1024 * 1024
    file.file.seek(0, 2)
    size = file.file.tell()
    file.file.seek(0)
    if not size or size > maximum:
        problem(422, "MEDIA_SIZE", "Images must be 1 byte–10 MB; videos 1 byte–500 MB")
    signature = file.file.read(16)
    file.file.seek(0)
    valid = {
        ".png": signature.startswith(b"\x89PNG\r\n\x1a\n"),
        ".jpg": signature.startswith(b"\xff\xd8\xff"),
        ".jpeg": signature.startswith(b"\xff\xd8\xff"),
        ".gif": signature.startswith((b"GIF87a", b"GIF89a")),
        ".webp": signature[:4] == b"RIFF" and signature[8:12] == b"WEBP",
        ".mp4": signature[4:8] == b"ftyp",
        ".mov": signature[4:8] in {b"ftyp", b"moov", b"mdat", b"wide"},
        ".webm": signature.startswith(b"\x1aE\xdf\xa3"),
        ".avi": signature[:4] == b"RIFF" and signature[8:12] == b"AVI ",
    }[extension]
    if not valid:
        problem(422, "MEDIA_CONTENT", "File contents do not match the selected format")
    storage = get_s3_client()
    if not storage:
        filename = "creative-" + str(uuid4()) + extension
        destination = UPLOAD_DIR / filename
        try:
            with destination.open("xb") as target:
                shutil.copyfileobj(file.file, target, length=1024 * 1024)
        except OSError:
            destination.unlink(missing_ok=True)
            problem(502, "UPLOAD_FAILED", "Creative storage upload failed; retry the upload")
        return settings.PUBLIC_API_URL.rstrip("/") + "/uploads/" + filename
    if not settings.R2_PUBLIC_URL.startswith("https://"):
        problem(
            503,
            "STORAGE_REQUIRED",
            "Creative uploads require Cloudflare R2 shared storage",
        )
    key = "creatives/" + str(uuid4()) + extension
    try:
        storage.upload_fileobj(
            file.file, settings.R2_BUCKET_NAME, key, ExtraArgs={"ContentType": mime}
        )
    except Exception:
        problem(
            502, "UPLOAD_FAILED", "Creative storage upload failed; retry the upload"
        )
    return settings.R2_PUBLIC_URL.rstrip("/") + "/" + key


def asset_json(db, asset, user):
    creator = db.get(User, asset.created_by_id) if asset.created_by_id else None
    return {
        "id": asset.id,
        "name": asset.name,
        "source_type": asset.source_type,
        "generated_ad_id": asset.generated_ad_id,
        "media_url": asset.media_url,
        "media_type": asset.media_type,
        "thumbnail_url": asset.thumbnail_url,
        "created_by_id": asset.created_by_id,
        "created_by_name": creator.name if creator else None,
        "registered_by_id": asset.registered_by_id,
        "created_at": asset.created_at,
        "metadata": asset.metadata_values,
        "metadata_revision": asset.metadata_revision,
        "generation_context": asset.generation_context,
        "analysis_status": asset.analysis_status,
        "analysis_error": asset.analysis_error,
        "analysis_model": asset.analysis_model,
        "analyzed_at": asset.analyzed_at,
        "analyzed_by_id": asset.analyzed_by_id,
        "can_edit": user.has_role("admin") or asset.created_by_id == user.id,
        "archived_at": asset.archived_at,
    }


def get_asset(db, identity, user, edit=False):
    lock_asset(db, identity)
    asset = db.get(CreativeAsset, identity, populate_existing=True)
    if not asset or asset.archived_at:
        problem(404, "NOT_FOUND", "Creative not found")
    if edit and not (user.has_role("admin") or asset.created_by_id == user.id):
        problem(
            403,
            "CREATIVE_OWNER_REQUIRED",
            "Only the creator or an admin can edit this creative",
        )
    return asset


@router.get("")
def list_assets(
    search: str = Query("", max_length=200),
    source_type: Optional[Literal["system_generated", "external_upload"]] = None,
    created_by_id: Optional[str] = None,
    limit: int = Query(24, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    statement = select(CreativeAsset).where(CreativeAsset.archived_at.is_(None))
    if search:
        statement = statement.where(CreativeAsset.name.ilike("%" + search + "%"))
    if source_type:
        statement = statement.where(CreativeAsset.source_type == source_type)
    if created_by_id:
        statement = statement.where(CreativeAsset.created_by_id == created_by_id)
    total = db.scalar(select(func.count()).select_from(statement.subquery()))
    assets = db.scalars(
        statement.order_by(CreativeAsset.created_at.desc(), CreativeAsset.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return page([asset_json(db, asset, user) for asset in assets], total, limit, offset)


@router.post("/uploads", status_code=201)
def upload_asset(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("campaigns:write")),
):
    url = store_upload(file)
    asset = CreativeAsset(
        name=Path(file.filename or "Uploaded creative").name[:255],
        source_type="external_upload",
        media_url=url,
        media_type="video" if file.content_type.startswith("video/") else "image",
        created_by_id=user.id,
        registered_by_id=user.id,
        metadata_values=CreativeMetadata().model_dump(),
    )
    db.add(asset)
    db.flush()
    event(db, asset, user.id, "uploaded")
    db.commit()
    return asset_json(db, asset, user)


@router.post("/generated/{generated_id}")
def register(
    generated_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("campaigns:write")),
):
    ad = db.get(GeneratedAd, generated_id)
    if not ad:
        problem(404, "NOT_FOUND", "Generated creative not found")
    try:
        asset = register_generated(db, ad, user.id)
    except ValueError as error:
        problem(422, "MEDIA_REQUIRED", str(error))
    if asset.archived_at:
        problem(409, "ARCHIVED", "This creative was archived")
    db.commit()
    return asset_json(db, asset, user)


@router.get("/{asset_id}")
def detail(
    asset_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    return asset_json(db, get_asset(db, asset_id, user), user)


@router.post("/{asset_id}/analyze")
def analyze(
    asset_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("campaigns:write")),
):
    asset = get_asset(db, asset_id, user)
    if asset.analysis_status == "ready":
        return asset_json(db, asset, user)
    try:
        values = analyze_media(asset.media_url, asset.media_type)
        values["talent_gender"] = None
        asset.metadata_values = CreativeMetadata.model_validate(values).model_dump()
    except Exception as error:
        capture_exception(error, "creatives.analyze")
        asset.analysis_status = "failed"
        asset.analysis_error = failure_message(error)
        event(db, asset, user.id, "analysis_failed")
        db.commit()
        problem(502, "ANALYSIS_FAILED", asset.analysis_error)
    asset.metadata_revision += 1
    asset.analysis_status, asset.analysis_error = "ready", None
    asset.analyzed_by_id, asset.analyzed_at, asset.analysis_model = (
        user.id,
        now(),
        MODEL,
    )
    event(
        db,
        asset,
        user.id,
        "analyzed",
        {
            "revision": asset.metadata_revision,
            "metadata": asset.metadata_values,
            "model": MODEL,
        },
    )
    db.commit()
    return asset_json(db, asset, user)


@router.get("/{asset_id}/events")
def history(
    asset_id: str,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    if not db.get(CreativeAsset, asset_id):
        problem(404, "NOT_FOUND", "Creative not found")
    statement = select(CreativeEvent).where(CreativeEvent.asset_id == asset_id)
    total = db.scalar(select(func.count()).select_from(statement.subquery()))
    rows = db.scalars(
        statement.order_by(CreativeEvent.created_at.desc(), CreativeEvent.id)
        .offset(offset)
        .limit(limit)
    ).all()
    users = {
        member.id: member.name
        for member in db.scalars(
            select(User).where(
                User.id.in_([row.actor_id for row in rows if row.actor_id])
            )
        )
    }
    return page(
        [
            {
                "id": row.id,
                "actor_id": row.actor_id,
                "actor_name": users.get(row.actor_id),
                "action": row.action,
                "created_at": row.created_at,
                "details": row.details,
            }
            for row in rows
        ],
        total,
        limit,
        offset,
    )


@router.patch("/{asset_id}")
def edit(
    asset_id: str,
    data: MetadataEdit,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("campaigns:write")),
):
    asset = get_asset(db, asset_id, user, edit=True)
    if (
        asset.analysis_status != "ready"
        or asset.metadata_revision != data.expected_revision
    ):
        problem(
            409,
            "REVISION_CONFLICT",
            "Creative changed or analysis is incomplete; reload before editing",
        )
    previous = asset.metadata_values
    asset.metadata_values = data.metadata.model_dump()
    asset.metadata_revision += 1
    event(
        db,
        asset,
        user.id,
        "metadata_edited",
        {
            "revision": asset.metadata_revision,
            "before": previous,
            "metadata": asset.metadata_values,
        },
    )
    db.commit()
    return asset_json(db, asset, user)


@router.delete("/{asset_id}")
def archive(
    asset_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("campaigns:write")),
):
    asset = get_asset(db, asset_id, user, edit=True)
    asset.archived_at = now()
    event(db, asset, user.id, "archived")
    db.commit()
    return {"id": asset.id, "archived_at": asset.archived_at}
