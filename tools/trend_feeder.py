"""Background Vision processing for images stored in trend-specific Drive folders."""
import asyncio
import logging
import os
import tempfile
import time

from sheets import trends as T
from tools import visions_ai as v

logger = logging.getLogger(__name__)

TREND_DAILY_LIMIT = 25
GAP_SECONDS = 30
TREND_DRIVE_PARENT_ID = os.environ.get("TREND_DRIVE_PARENT_ID", "")
_fail_count = {}


def create_trend_folder(name: str) -> str:
    """Create a trend folder under the configured parent; return empty on failure."""
    if not TREND_DRIVE_PARENT_ID:
        logger.warning("Trend folder not created — TREND_DRIVE_PARENT_ID is missing.")
        return ""

    try:
        from tools import drive_upload

        if drive_upload.is_configured():
            try:
                return drive_upload.create_folder(name, TREND_DRIVE_PARENT_ID)
            except Exception as error:
                logger.warning(
                    "OAuth folder creation failed; trying service account: %s",
                    error,
                )
    except Exception as error:
        logger.warning("Drive OAuth folder creation unavailable: %s", error)

    if v.drive_service is None:
        logger.warning("Trend folder not created — Google Drive service account is unavailable.")
        return ""
    try:
        metadata = {
            "name": name.strip(),
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [TREND_DRIVE_PARENT_ID],
        }
        return (
            v.drive_service.files()
            .create(body=metadata, fields="id")
            .execute()["id"]
        )
    except Exception:
        logger.exception("Trend folder creation failed for %s.", name)
        return ""


def run_trend_feeder() -> int:
    """Process a single batch; return count, or -3 when the daily cap is reached."""
    if v.is_stop_requested() or v.drive_service is None:
        return 0

    done_today = T.count_added_today()
    if done_today >= TREND_DAILY_LIMIT:
        logger.info("Trend feeder daily limit reached (%s).", TREND_DAILY_LIMIT)
        return -3

    trends = sorted(T.get_trends(), key=lambda trend: not trend["active"])
    processed = 0

    for trend in trends:
        folder_id = trend["drive_folder_id"]
        if not folder_id:
            continue
        try:
            result = (
                v.drive_service.files()
                .list(
                    q=f"'{folder_id}' in parents and trashed=false",
                    pageSize=1000,
                    orderBy="createdTime",
                    fields="files(id, name, mimeType)",
                )
                .execute()
            )
        except Exception:
            logger.exception("[trend:%s] Drive list failed.", trend["trend"])
            continue

        seen = T.processed_sources(trend["trend"])
        new_files = [
            file
            for file in result.get("files", [])
            if v._is_image(file)
            and file["id"] not in seen
            and _fail_count.get(file["id"], 0) < 2
        ]

        for file in new_files:
            if v.is_stop_requested() or done_today + processed >= TREND_DAILY_LIMIT:
                return processed

            extension = os.path.splitext(file.get("name", ""))[1][:12]
            temp_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    prefix=f"trend_{file['id']}_",
                    suffix=extension,
                    delete=False,
                ) as temp_file:
                    temp_path = temp_file.name

                logger.info("[trend:%s] Analyzing %s", trend["trend"], file["name"])
                v.download_from_drive(file["id"], temp_path)
                dna = v.analyze_image(temp_path)
                T.append_trend_prompt(trend["trend"], dna, file["id"])
                processed += 1
                time.sleep(GAP_SECONDS)
            except Exception:
                _fail_count[file["id"]] = _fail_count.get(file["id"], 0) + 1
                logger.exception(
                    "[trend:%s] Vision processing failed for %s.",
                    trend["trend"],
                    file.get("name", file["id"]),
                )
                time.sleep(60)
            finally:
                if temp_path and os.path.exists(temp_path):
                    os.remove(temp_path)

    return processed


async def trend_feeder_loop():
    """Run as a background asyncio task alongside the existing Vision Feeder."""
    await asyncio.sleep(60)
    while True:
        try:
            processed = await asyncio.to_thread(run_trend_feeder)
        except Exception:
            logger.exception("Trend feeder loop failed.")
            processed = 0
        await asyncio.sleep(
            3600 if processed == -3 else (300 if processed <= 0 else 30)
        )
