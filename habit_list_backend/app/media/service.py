"""Safe local media storage with database ownership and soft deletion."""
from __future__ import annotations

import base64
import hashlib
import io
import wave
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import Settings, get_settings
from ..db.database import get_db
from ..db.models import MediaAsset, uuid7
from ..providers import dashscope

MediaKind = Literal["audio", "image", "video"]
ALLOWED_AUDIO_MIME = {
    "audio/aac",
    "audio/flac",
    "audio/m4a",
    "audio/mp4",
    "audio/mpeg",
    "audio/ogg",
    "audio/opus",
    "audio/wav",
    "audio/webm",
    "video/webm",  # MediaRecorder commonly reports webm as video/webm.
}
ALLOWED_IMAGE_MIME = {
    "image/gif",
    "image/heic",
    "image/heif",
    "image/jpeg",
    "image/png",
    "image/webp",
}
ALLOWED_VIDEO_MIME = {
    "video/mp4",
    "video/quicktime",
    "video/webm",
    "video/x-m4v",
}
_MIME_SUFFIX = {
    "audio/aac": ".aac",
    "audio/flac": ".flac",
    "audio/m4a": ".m4a",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/wav": ".wav",
    "audio/webm": ".webm",
    "video/webm": ".webm",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/x-m4v": ".m4v",
    "image/gif": ".gif",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


class MediaValidationError(ValueError):
    """A client-correctable media upload error."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalise_mime(value: str | None, filename: str) -> str:
    mime = (value or "").split(";", 1)[0].strip().lower()
    if mime:
        return mime
    suffix = Path(filename).suffix.lower()
    return {
        ".aac": "audio/aac",
        ".flac": "audio/flac",
        ".m4a": "audio/m4a",
        ".mp3": "audio/mpeg",
        ".ogg": "audio/ogg",
        ".opus": "audio/opus",
        ".wav": "audio/wav",
        ".webm": "audio/webm",
        ".heic": "image/heic",
        ".heif": "image/heif",
        ".gif": "image/gif",
        ".jpeg": "image/jpeg",
        ".jpg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".mp4": "video/mp4",
        ".m4v": "video/x-m4v",
        ".mov": "video/quicktime",
    }.get(suffix, "application/octet-stream")


def _validate_container_header(data: bytes, mime_type: str) -> None:
    """Reject mislabeled Live Photo containers before writing to disk.

    Older preview tests and browser image uploads intentionally accept opaque
    image bytes, but HEIC/HEIF and MOV/MP4 are container formats where trusting
    a user supplied MIME would make playback and downstream processing
    unpredictable.  These formats all carry an ISO-BMFF ``ftyp`` box.
    """

    if mime_type not in {
        "image/heic",
        "image/heif",
        "video/mp4",
        "video/quicktime",
        "video/x-m4v",
    }:
        return
    if len(data) < 12 or data[4:8] != b"ftyp":
        raise MediaValidationError("Live Photo 文件头无效，未识别为 HEIC/HEIF 或 MOV/MP4")
    brand = data[8:12]
    if mime_type.startswith("image/"):
        if brand not in {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}:
            raise MediaValidationError("HEIC/HEIF 文件头无效")
    elif brand not in {b"qt  ", b"isom", b"iso2", b"mp41", b"mp42", b"M4V ", b"avc1"}:
        raise MediaValidationError("动态图片文件头无效")


def _audio_duration_ms(data: bytes, mime_type: str) -> int | None:
    """Read duration for formats available in the Python standard library.

    Browser recordings are often WebM and intentionally remain duration-less
    until a media probe is introduced. WAV is common in tests and desktop
    uploads, so parse it without shelling out to a system binary.
    """

    if mime_type != "audio/wav":
        return None
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            rate = int(wav.getframerate() or 0)
            frames = int(wav.getnframes() or 0)
        if rate <= 0 or frames < 0:
            return None
        return int(round(frames * 1000 / rate))
    except (EOFError, OSError, ValueError, wave.Error):
        return None


def _path_for(asset_id: str, kind: MediaKind, mime_type: str, settings: Settings) -> tuple[Path, str]:
    suffix = _MIME_SUFFIX.get(mime_type, ".bin")
    now = datetime.now(timezone.utc)
    relative = Path(kind) / f"{now.year:04d}" / f"{now.month:02d}" / f"{asset_id}{suffix}"
    root = settings.media_root_path
    path = (root / relative).resolve()
    if root not in path.parents:
        raise MediaValidationError("媒体存储路径无效")
    return path, relative.as_posix()


def asset_url(asset_id: str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    return f"{settings.api_prefix}/media/{asset_id}"


def transcript_is_terrain_trusted(
    assets: Iterable[MediaAsset], settings: Settings | None = None
) -> bool:
    """Whether machine transcripts alone may become terrain evidence.

    Baseline 8.3.  The formation layer shows the user inferences about
    themselves, so it must not build one out of a sentence the machine guessed
    and the user never saw.  A transcript qualifies only when the provider
    vouched for it above ``memory_v3_min_asr_confidence``; an unreported
    confidence is unverified, not "probably fine".

    This gate is not a dead end for voice.  The client shows the transcript for
    review, and a confirmed or edited transcript arrives as user-authored text
    instead — which is eligible, because by then the words are the user's.
    """

    settings = settings or get_settings()
    transcribed = [asset for asset in assets if (asset.transcript or "").strip()]
    if not transcribed:
        return False
    return all(
        asset.transcript_confidence is not None
        and asset.transcript_confidence >= settings.memory_v3_min_asr_confidence
        for asset in transcribed
    )


def asset_response(asset: MediaAsset, settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    return {
        "asset_id": asset.asset_id,
        "kind": asset.asset_kind,
        "mime_type": asset.mime_type,
        "original_name": asset.original_name,
        "byte_size": int(asset.byte_size or 0),
        "duration_ms": asset.duration_ms,
        "transcript": asset.transcript,
        "transcript_confidence": asset.transcript_confidence,
        "group_id": asset.media_group_id,
        "role": asset.media_role,
        "is_live_photo_part": bool(asset.media_group_id and asset.media_role in {"live_still", "live_motion"}),
        "url": asset_url(asset.asset_id, settings),
        "created_at": asset.created_at,
    }


async def get_asset_for_user(
    db: AsyncSession,
    *,
    user_id: str,
    asset_id: str,
    kind: MediaKind | None = None,
    active_only: bool = True,
) -> MediaAsset | None:
    clauses = [MediaAsset.asset_id == asset_id, MediaAsset.user_id == user_id]
    if kind:
        clauses.append(MediaAsset.asset_kind == kind)
    if active_only:
        clauses.append(MediaAsset.status == "active")
    return (await db.execute(select(MediaAsset).where(*clauses))).scalar_one_or_none()


async def store_asset(
    db: AsyncSession,
    *,
    user_id: str,
    kind: MediaKind,
    filename: str,
    mime_type: str | None,
    data: bytes,
    settings: Settings | None = None,
    transcribe: bool = False,
    media_group_id: str | None = None,
    media_role: str | None = None,
) -> MediaAsset:
    settings = settings or get_settings()
    filename = (filename or "upload.bin").replace("\\", "/").rsplit("/", 1)[-1][:255]
    mime = _normalise_mime(mime_type, filename)
    allowed = (
        ALLOWED_AUDIO_MIME
        if kind == "audio"
        else ALLOWED_IMAGE_MIME
        if kind == "image"
        else ALLOWED_VIDEO_MIME
    )
    if mime not in allowed:
        raise MediaValidationError(f"不支持的{kind}媒体类型: {mime}")
    limit = settings.media_max_image_bytes if kind == "image" else settings.media_max_bytes
    if not data:
        raise MediaValidationError("媒体文件为空")
    if len(data) > limit:
        raise MediaValidationError(f"媒体文件不能超过 {limit // (1024 * 1024)}MB")

    _validate_container_header(data, mime)
    group_id = (media_group_id or "").strip()[:64] or None
    role = (media_role or "").strip()[:24] or None
    if role not in {None, "live_still", "live_motion"}:
        raise MediaValidationError("不支持的媒体组角色")
    if role == "live_still" and kind != "image":
        raise MediaValidationError("Live Photo 静态部分必须是图片")
    if role == "live_motion" and kind != "video":
        raise MediaValidationError("Live Photo 动态部分必须是视频")
    if role and not group_id:
        raise MediaValidationError("Live Photo 部分缺少媒体组标识")

    duration_ms = _audio_duration_ms(data, mime) if kind == "audio" else None
    if duration_ms is not None and duration_ms > settings.media_max_audio_seconds * 1000:
        raise MediaValidationError(
            f"音频不能超过 {settings.media_max_audio_seconds // 60} 分钟"
        )

    asset_id = uuid7()
    path, relative = _path_for(asset_id, kind, mime, settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    transcript: str | None = None
    transcript_confidence: float | None = None
    metadata: dict = {"storage": "local", "media_policy_version": "media-v2"}
    if group_id:
        metadata["media_group_id"] = group_id
    if role:
        metadata["media_role"] = role
    if transcribe and kind == "audio":
        try:
            result = await dashscope.asr_transcribe(data, filename)
            transcript = result.text.strip() or None
            transcript_confidence = result.confidence if transcript else None
            metadata["transcribed"] = transcript is not None
        except Exception as exc:  # noqa: BLE001 - original audio remains usable if ASR fails
            metadata["transcription_error"] = type(exc).__name__

    asset = MediaAsset(
        asset_id=asset_id,
        user_id=user_id,
        asset_kind=kind,
        mime_type=mime,
        original_name=filename,
        storage_path=relative,
        byte_size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        duration_ms=duration_ms,
        transcript=transcript,
        transcript_confidence=transcript_confidence,
        media_group_id=group_id,
        media_role=role,
        metadata_json=metadata,
        created_at=_now_iso(),
    )
    db.add(asset)
    await db.flush()
    return asset


async def attach_assets(
    db: AsyncSession,
    *,
    user_id: str,
    asset_ids: list[str],
    owner_type: str,
    owner_id: str,
) -> list[MediaAsset]:
    unique_ids = list(dict.fromkeys(str(value) for value in asset_ids if value))
    if not unique_ids:
        return []
    assets = list(
        (
            await db.execute(
                select(MediaAsset).where(
                    MediaAsset.user_id == user_id,
                    MediaAsset.asset_id.in_(unique_ids),
                    MediaAsset.status == "active",
                )
            )
        ).scalars().all()
    )
    if len(assets) != len(unique_ids):
        raise MediaValidationError("存在不可用或不属于当前用户的媒体")
    order = {asset_id: index for index, asset_id in enumerate(unique_ids)}
    assets.sort(key=lambda asset: order.get(asset.asset_id, len(order)))
    for asset in assets:
        if asset.owner_type != "unattached" and not (
            asset.owner_type == owner_type and asset.owner_id == owner_id
        ):
            raise MediaValidationError("媒体已经绑定到另一条记录")
        asset.owner_type = owner_type
        asset.owner_id = owner_id
    return assets


async def delete_asset(
    db: AsyncSession,
    *,
    user_id: str,
    asset_id: str,
    settings: Settings | None = None,
) -> bool:
    settings = settings or get_settings()
    asset = await get_asset_for_user(db, user_id=user_id, asset_id=asset_id, active_only=False)
    if asset is None or asset.status == "deleted":
        return False
    assets = [asset]
    group_id = str(asset.media_group_id or "").strip()
    if group_id and asset.media_role in {"live_still", "live_motion"}:
        siblings = list(
            (
                await db.execute(
                    select(MediaAsset).where(
                        MediaAsset.user_id == user_id,
                        MediaAsset.media_group_id == group_id,
                        MediaAsset.status == "active",
                    )
                )
            ).scalars().all()
        )
        # Keep deletion atomic at the Live Photo boundary. A caller may send
        # either asset id; the other original must not survive by accident.
        assets = list({row.asset_id: row for row in [asset, *siblings]}.values())
    deleted_at = _now_iso()
    for row in assets:
        row.status = "deleted"
        row.deleted_at = deleted_at
        path = (settings.media_root_path / row.storage_path).resolve()
        if settings.media_root_path in path.parents and path.exists():
            path.unlink()
    return True


def asset_file_path(asset: MediaAsset, settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    root = settings.media_root_path
    path = (root / asset.storage_path).resolve()
    if root not in path.parents:
        raise MediaValidationError("媒体存储路径无效")
    return path


async def load_media_prompt_parts(
    *,
    user_id: str,
    asset_ids: list[str] | None,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    """Load owned media as provider prompt parts without exposing file paths.

    The returned data URLs exist only for the in-flight model request.  They
    are intentionally absent from database rows, outbox payloads and logs.
    """

    settings = settings or get_settings()
    ids = list(dict.fromkeys(str(value) for value in (asset_ids or []) if value))
    if not ids:
        return []
    async with get_db(read_only=True) as db:
        assets = list(
            (
                await db.execute(
                    select(MediaAsset).where(
                        MediaAsset.user_id == user_id,
                        MediaAsset.asset_id.in_(ids),
                        MediaAsset.asset_kind.in_(["audio", "image"]),
                        MediaAsset.status == "active",
                    )
                )
            ).scalars().all()
        )
    parts: list[dict[str, Any]] = []
    for asset in assets:
        path = asset_file_path(asset, settings)
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if not raw:
            continue
        if asset.asset_kind == "audio":
            suffix = Path(asset.original_name).suffix.lower().lstrip(".")
            audio_format = suffix or asset.mime_type.split("/", 1)[-1]
            parts.append(
                {
                    "type": "input_audio",
                    "input_audio": {
                        "data": base64.b64encode(raw).decode("ascii"),
                        "format": audio_format,
                    },
                }
            )
        else:
            data_url = f"data:{asset.mime_type};base64,{base64.b64encode(raw).decode('ascii')}"
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": data_url},
                }
            )
    return parts


async def cleanup_unattached_assets(
    db: AsyncSession,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> int:
    """Delete abandoned uploads after the configured retention window.

    The database row is soft-deleted before the file is removed. This keeps
    ownership and deletion state auditable while ensuring a cancelled composer
    cannot leak files on the local disk indefinitely.
    """

    settings = settings or get_settings()
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    cutoff = current - timedelta(minutes=settings.media_unattached_retention_minutes)
    rows = list(
        (
            await db.execute(
                select(MediaAsset).where(
                    MediaAsset.status == "active",
                    MediaAsset.owner_type == "unattached",
                )
            )
        )
        .scalars()
        .all()
    )
    removed = 0
    for asset in rows:
        if asset.status != "active":
            continue
        try:
            created = datetime.fromisoformat(str(asset.created_at).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created >= cutoff:
            continue
        asset.status = "deleted"
        asset.deleted_at = current.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        path = asset_file_path(asset, settings)
        if path.is_file():
            path.unlink()
        removed += 1
    return removed
