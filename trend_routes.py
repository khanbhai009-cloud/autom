"""FastAPI endpoints for managing trends and uploading images to Drive."""
import asyncio
import os
from pathlib import Path
from typing import List

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from sheets import trends as T
from tools.trend_feeder import TREND_DAILY_LIMIT, create_trend_folder

router = APIRouter(prefix="/trends", tags=["trends"])
REGULAR = "Regular"
MAX_IMAGE_BYTES = 15 * 1024 * 1024
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif", ".bmp", ".tif", ".tiff"
}


class AddTrend(BaseModel):
    trend: str = Field(min_length=1, max_length=80)
    keywords: str = Field(default="", max_length=1000)


class Toggle(BaseModel):
    trend: str = Field(min_length=1, max_length=80)
    active: bool


class Keywords(BaseModel):
    trend: str = Field(min_length=1, max_length=80)
    keywords: str = Field(max_length=1000)


@router.get("")
async def list_trends():
    try:
        trends, counts = await asyncio.gather(
            asyncio.to_thread(T.get_trends),
            asyncio.to_thread(T.count_prompts_by_trend),
        )
    except Exception as error:
        raise HTTPException(503, "Trend settings are unavailable. Check Google Sheets configuration.") from error
    return [{**trend, "prompts": counts.get(trend["trend"], 0)} for trend in trends]


@router.get("/feeder-status")
async def feeder_status():
    try:
        processed_today = await asyncio.to_thread(T.count_added_today)
    except Exception as error:
        raise HTTPException(503, "Trend feeder status is unavailable. Check Google Sheets configuration.") from error
    return {"processed_today": processed_today, "daily_limit": TREND_DAILY_LIMIT}


@router.post("/add")
async def add_trend(body: AddTrend):
    name = body.trend.strip()
    if not name or name.casefold() == REGULAR.casefold():
        raise HTTPException(400, "Enter a trend name other than Regular.")

    try:
        existing = await asyncio.to_thread(T.get_trends)
        if any(trend["trend"].casefold() == name.casefold() for trend in existing):
            raise HTTPException(409, "That trend already exists.")
        folder_id = await asyncio.to_thread(create_trend_folder, name)
        created = await asyncio.to_thread(
            T.add_trend, name, body.keywords, folder_id
        )
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(503, "Could not save the trend. Check Google Sheets and Drive configuration.") from error

    if not created:
        raise HTTPException(409, "That trend already exists.")
    response = {"ok": True, "trend": name, "drive_folder_id": folder_id}
    if not folder_id:
        response["warning"] = (
            "Trend saved, but its Drive folder was not created. Set "
            "TREND_DRIVE_PARENT_ID and grant the service account access."
        )
    return response


@router.post("/toggle")
async def toggle(body: Toggle):
    try:
        updated = await asyncio.to_thread(
            T.set_trend_active, body.trend.strip(), body.active
        )
    except Exception as error:
        raise HTTPException(503, "Could not update the trend. Check Google Sheets configuration.") from error
    if not updated:
        raise HTTPException(404, "Trend not found.")
    return {"ok": True, "trend": body.trend, "active": body.active}


@router.post("/keywords")
async def update_keywords(body: Keywords):
    try:
        updated = await asyncio.to_thread(
            T.set_trend_keywords, body.trend.strip(), body.keywords
        )
    except Exception as error:
        raise HTTPException(503, "Could not update keywords. Check Google Sheets configuration.") from error
    if not updated:
        raise HTTPException(404, "Trend not found.")
    return {"ok": True}


@router.post("/upload")
async def upload(target: str = Form(...), files: List[UploadFile] = File(...)):
    """Queue images in Drive using Service Account."""
    if not files:
        raise HTTPException(400, "Choose at least one image.")

    normalized_target = target.strip()
    
    # Regular ke liye tumhari di hui Drive folder ID direct set ki gayi hai
    if normalized_target.casefold() == REGULAR.casefold():
        folder_id = "1pazvTr_I75pqCGZW-OEwr0Bs2q_8tFnu"
        normalized_target = REGULAR
    else:
        try:
            trends = await asyncio.to_thread(T.get_trends)
        except Exception as error:
            raise HTTPException(503, "Trend settings are unavailable.") from error
        
        # Trend wali id automatically sheets (T.get_trends) se aayegi
        selected = next(
            (
                trend for trend in trends
                if trend["trend"].casefold() == normalized_target.casefold()
            ),
            None,
        )
        if not selected or not selected["drive_folder_id"]:
            raise HTTPException(404, "Trend or its Drive folder was not found.")
        folder_id = selected["drive_folder_id"]
        normalized_target = selected["trend"]

    # Naya Service Account upload logic
    from tools import visions_ai as v
    import io
    from googleapiclient.http import MediaIoBaseUpload

    if v.drive_service is None:
        raise HTTPException(503, "Drive service account is unavailable.")

    saved = 0
    failed = []
    
    for upload_file in files:
        filename = Path(upload_file.filename or "image.jpg").name
        extension = Path(filename).suffix.casefold()
        content_type = (upload_file.content_type or "").casefold()
        
        if not content_type.startswith("image/") and extension not in IMAGE_EXTENSIONS:
            failed.append({"file": filename, "error": "Only image files are accepted."})
            continue

        try:
            data = await upload_file.read(MAX_IMAGE_BYTES + 1)
            if len(data) > MAX_IMAGE_BYTES:
                failed.append(
                    {"file": filename, "error": "Image exceeds the 15 MB upload limit."}
                )
                continue
                
            # Background thread me Google Drive API (Service Account) se upload execute karna
            def do_upload():
                metadata = {"name": filename, "parents": [folder_id]}
                media = MediaIoBaseUpload(io.BytesIO(data), mimetype=content_type or "image/jpeg", resumable=True)
                v.drive_service.files().create(body=metadata, media_body=media, fields="id").execute()

            await asyncio.to_thread(do_upload)
            saved += 1
            
        except Exception as error:
            failed.append(
                {"file": filename, "error": str(error).strip()[:200] or "Upload failed."}
            )
        finally:
            await upload_file.close()

    if not saved:
        message = "; ".join(item["error"] for item in failed[:3])
        raise HTTPException(502, f"No images were uploaded. {message}")
        
    return {
        "ok": True,
        "target": normalized_target,
        "queued": saved,
        "failed": failed,
    }
