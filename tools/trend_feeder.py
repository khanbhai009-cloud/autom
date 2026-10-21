import asyncio
import logging
import os
import tempfile
import threading
import time

from sheets import trends as T
from tools import visions_ai as v

logger = logging.getLogger(__name__)

TREND_DAILY_LIMIT = 25
GAP_SECONDS = 30
TREND_DRIVE_PARENT_ID = os.environ.get("TREND_DRIVE_PARENT_ID", "")

# Alag processed folder chahiye to env me TREND_PROCESSED_FOLDER_ID set karo,
# warna regular Processed folder hi use hoga.
TREND_PROCESSED_FOLDER_ID = (
    os.environ.get("TREND_PROCESSED_FOLDER_ID", "") or v.DRIVE_PROCESSED_FOLDER_ID
)

_fail_count = {}
_run_lock = threading.Lock()


def create_trend_folder(name: str) -> str:
    """Create a trend folder using Service Account and grant writer (editor) access."""
    if not TREND_DRIVE_PARENT_ID:
        logger.warning("Trend folder not created — TREND_DRIVE_PARENT_ID is missing.")
        return ""

    if v.drive_service is None:
        logger.warning("Trend folder not created — Google Drive service account is unavailable.")
        return ""

    try:
        metadata = {
            "name": name.strip(),
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [TREND_DRIVE_PARENT_ID],
        }
        folder = v.drive_service.files().create(body=metadata, fields="id").execute()
        folder_id = folder["id"]

        permission_body = {
            "type": "user",
            "role": "writer",
            "emailAddress": "finisher@root-monolith-474003-b5.iam.gserviceaccount.com",
        }
        v.drive_service.permissions().create(
            fileId=folder_id,
            body=permission_body,
            fields="id",
        ).execute()

        return folder_id

    except Exception:
        logger.exception("Trend folder creation or permission assignment failed for %s.", name)
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
                
                # --- NEW PAUSE & RETRY LOGIC FOR API RATE LIMITS (503) ---
                dna = None
                for attempt in range(3):  # 3 baar try karega
                    try:
                        dna = v.analyze_image(temp_path)
                        break  # Agar success ho gaya toh loop tod do
                    except Exception as e:
                        if attempt < 2:  # Agar pehla ya doosra attempt fail hua
                            logger.warning("[trend:%s] API busy. 40 seconds ka pause le raha hai... (Attempt %d/3)", trend["trend"], attempt + 1)
                            time.sleep(40)  # 40 seconds ka timer/pause
                        else:
                            raise e  # 3 baar fail hone par main exception block me bhej do
                # ---------------------------------------------------------

                T.append_trend_prompt(trend["trend"], dna, file["id"])
                logger.info("[trend:%s] Prompt saved for %s", trend["trend"], file["name"])
                processed += 1

                # Prompt save ho chuka hai, isliye move ka error alag handle hota hai
                try:
                    v.move_file_in_drive(file["id"], folder_id, TREND_PROCESSED_FOLDER_ID)
                    logger.info("[trend:%s] Moved %s to processed", trend["trend"], file["name"])
                except Exception:
                    logger.exception(
                        "[trend:%s] Move to processed failed for %s.",
                        trend["trend"],
                        file.get("name", file["id"]),
                    )

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


def run_trend_feeder_safe() -> int:
    """Loop aur manual run dono yahi use karein, taaki ek saath do run na chalein."""
    if not _run_lock.acquire(blocking=False):
        logger.info("Trend feeder already running — skipping this trigger.")
        return 0
    try:
        return run_trend_feeder()
    finally:
        _run_lock.release()


def is_feeder_running() -> bool:
    return _run_lock.locked()


def start_manual_run() -> bool:
    """Dashboard 'Run now' button yahi call karega. False = already running."""
    if _run_lock.locked():
        return False
    logger.info("Trend feeder manual run requested.")
    threading.Thread(target=run_trend_feeder_safe, daemon=True).start()
    return True


async def trend_feeder_loop():
    """Run as a background asyncio task alongside the existing Vision Feeder."""
    await asyncio.sleep(60)
    while True:
        try:
            processed = await asyncio.to_thread(run_trend_feeder_safe)
        except Exception:
            logger.exception("Trend feeder loop failed.")
            processed = 0
        await asyncio.sleep(
            3600 if processed == -3 else (300 if processed <= 0 else 30)
        )
