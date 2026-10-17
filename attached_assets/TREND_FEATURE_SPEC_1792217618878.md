# Pinteresto — TREND FEATURE: Complete Implementation Spec (for AI coding agent)

> Is file ko poora padho, phir repo me saare changes implement karo.
> Repo: Pinteresto v3 (FastAPI + LangGraph + Google Sheets + Drive + Gemini Vision). Deploy: Render.

---

## 0. GOLDEN RULES (sabse pehle ye padho)

1. **REGULAR FLOW KO BILKUL CHHEDNA NAHI HAI.** Jab koi trend ON nahi hai, system pehle jaisa hi chalega.
2. **`tools/visions_ai.py` EDIT NAHI karna.** Sirf import karke reuse karna hai (`drive_service`, `analyze_image`, `download_from_drive`, `_is_image`, `is_stop_requested`).
3. **`Prompts_Master` sheet ko touch nahi karna.** Trend ke prompts alag sheet `Trend_Prompts` me jayenge.
4. **Trend path me koi bhi error aaye → silently regular flow pe fallback** (log warning, pipeline mat roko).
5. Existing files me sirf **3 chhote edits** (Section 4). Baaki sab naye files.
6. Trend ka ON/OFF **sirf manual toggle** hai. Koi start_date / end_date logic NAHI.
7. Hashtags/title/description ke keywords **user dega** (Trend_Settings me). Vision AI trend rows ke liye keywords/tags nahi nikalega — sirf image se prompt (style_key, label, description, t2i_base, niche_affinity).

---

## 1. FEATURE KA OVERVIEW

User mobile se dashboard (web app) me:
- **Trend add** karta hai (naam + keywords). Drive me us naam ka folder ban jata hai.
- **Trend ON/OFF toggle** karta hai.
- **Image upload** karta hai: dropdown me "Regular" ya koi trend chunta hai.
  - Regular → purane Drive Input folder me jata hai (purana Vision Feeder khud process karega).
  - Trend → us trend ke Drive folder me jata hai. **Upload ke time koi Vision call nahi.**
- Background me ek alag **Trend Feeder** loop dheere-dheere trend folders ki images ko Gemini Vision se process karke `Trend_Prompts` sheet me prompts add karta hai.
- Posting time pe CMO check karta hai: koi trend ON hai? → trend ke prompts + trend keywords use karta hai. Sab OFF → purana regular flow.

### Flow diagram
```
UPLOAD (turant, fast)
  App: trend/Regular chuno + images
    ├─ Regular → Drive Input folder (existing)  → existing Vision Feeder
    └─ Trend   → Drive/<trend> folder → "N queued" response

BACKGROUND (dheere dheere)
  trend_feeder_loop (naya, alag asyncio task)
    → har trend folder (ON wale pehle) ki nayi images (Drive file id se track)
    → analyze_image() (Gemini Vision, existing function)
    → Trend_Prompts sheet me row (trend = Halloween)
    → daily limit 25, har image ke beech 30s gap

POSTING (scheduler, har 60-72 min)
  node_cmo_mastermind → _call_cmo_for_account(account_key, ...)
    → [NEW HOOK] call_trend_cmo()
        ├─ koi trend ON + uske prompts ready → trend strategy return (regular skip)
        └─ warna None → REGULAR FLOW (unchanged)
```

---

## 2. GOOGLE SHEETS (2 nayi tabs, auto-create)

### `Trend_Settings`
| trend | active | drive_folder_id | keywords |
|---|---|---|---|
| Halloween | TRUE/FALSE | <drive folder id> | halloween decor, spooky season, pumpkin aesthetic |

- `active`: string `TRUE`/`FALSE` (gspread kabhi bool deta hai — dono handle karo).
- `keywords`: comma separated. User Pinterest analytics se dekhkar dashboard se edit karega.

### `Trend_Prompts`
`trend | style_key | account | label | description | t2i_base | niche_affinity | source_file | added_on`
- `source_file` = **Drive file id** (duplicate filenames se bachne ke liye) — isi se "already processed" track hota hai.
- `added_on` = `YYYY-MM-DD` — daily limit count ke liye.
- `account` = Vision ne jo diya (account_1 / account_2). CMO pehle same-account rows use karta hai, na mile to saari rows.

---

## 3. NAYE FILES (exact code — as-is banao)

> Agar repo ka structure thoda alag ho to imports adapt karo, logic same rakho.
> `sheets/trends.py` apna gspread client khud banata hai (existing `sheets/base.py` pe dependency nahi) — config se `GOOGLE_CREDS_JSON`, `SPREADSHEET_ID` leta hai.

### 3.1 `sheets/trends.py`
```python
"""
sheets/trends.py — Trend system data layer (NEW, self-contained).

Tabs (auto-created):
  Trend_Settings : trend | active | drive_folder_id | keywords
  Trend_Prompts  : trend | style_key | account | label | description | t2i_base |
                   niche_affinity | source_file | added_on

Does NOT touch Prompts_Master or any regular-flow tab.
"""
import json
import logging
import threading
import time
from datetime import date

import gspread
from google.oauth2.service_account import Credentials

from config import GOOGLE_CREDS_JSON, SPREADSHEET_ID

logger = logging.getLogger(__name__)

SETTINGS_TAB = "Trend_Settings"
PROMPTS_TAB = "Trend_Prompts"
SETTINGS_HEADERS = ["trend", "active", "drive_folder_id", "keywords"]
PROMPTS_HEADERS = ["trend", "style_key", "account", "label", "description",
                   "t2i_base", "niche_affinity", "source_file", "added_on"]

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets",
           "https://www.googleapis.com/auth/drive"]
_lock = threading.Lock()
_client = None
_cache = {"settings": (0.0, []), "prompts": (0.0, [])}
_TTL = 60  # seconds — toggle/edit also clears cache immediately


def _gc():
    global _client
    if _client is None:
        info = json.loads(GOOGLE_CREDS_JSON) if isinstance(GOOGLE_CREDS_JSON, str) else GOOGLE_CREDS_JSON
        _client = gspread.authorize(Credentials.from_service_account_info(info, scopes=_SCOPES))
    return _client


def _ws(tab: str, headers: list):
    sh = _gc().open_by_key(SPREADSHEET_ID)
    try:
        return sh.worksheet(tab)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab, rows=500, cols=len(headers))
        ws.append_row(headers)
        return ws


def init_trend_tabs():
    """Call once at startup (safe to call repeatedly)."""
    _ws(SETTINGS_TAB, SETTINGS_HEADERS)
    _ws(PROMPTS_TAB, PROMPTS_HEADERS)


def _clear():
    _cache["settings"] = (0.0, [])
    _cache["prompts"] = (0.0, [])


def _is_true(v) -> bool:
    return str(v).strip().upper() == "TRUE"


# ── Settings ────────────────────────────────────────────────────────────────
def get_trends() -> list:
    ts, data = _cache["settings"]
    if data and time.time() - ts < _TTL:
        return data
    rows = _ws(SETTINGS_TAB, SETTINGS_HEADERS).get_all_records()
    out = []
    for r in rows:
        name = str(r.get("trend", "")).strip()
        if not name:
            continue
        out.append({
            "trend": name,
            "active": _is_true(r.get("active")),
            "drive_folder_id": str(r.get("drive_folder_id", "")).strip(),
            "keywords": [k.strip() for k in str(r.get("keywords", "")).split(",") if k.strip()],
        })
    _cache["settings"] = (time.time(), out)
    return out


def get_active_trends() -> list:
    return [t for t in get_trends() if t["active"]]


def _find_row(ws, trend: str) -> int:
    for i, v in enumerate(ws.col_values(1), start=1):
        if i > 1 and v.strip().lower() == trend.strip().lower():
            return i
    return 0


def add_trend(trend: str, keywords: str = "", drive_folder_id: str = "") -> bool:
    with _lock:
        ws = _ws(SETTINGS_TAB, SETTINGS_HEADERS)
        if _find_row(ws, trend):
            return False
        ws.append_row([trend.strip(), "FALSE", drive_folder_id, keywords.strip()])
        _clear()
        return True


def set_trend_active(trend: str, active: bool) -> bool:
    with _lock:
        ws = _ws(SETTINGS_TAB, SETTINGS_HEADERS)
        row = _find_row(ws, trend)
        if not row:
            return False
        ws.update_cell(row, 2, "TRUE" if active else "FALSE")
        _clear()
        return True


def set_trend_keywords(trend: str, keywords: str) -> bool:
    with _lock:
        ws = _ws(SETTINGS_TAB, SETTINGS_HEADERS)
        row = _find_row(ws, trend)
        if not row:
            return False
        ws.update_cell(row, 4, keywords.strip())
        _clear()
        return True


# ── Prompts ─────────────────────────────────────────────────────────────────
def _all_prompts() -> list:
    ts, data = _cache["prompts"]
    if data and time.time() - ts < _TTL:
        return data
    rows = _ws(PROMPTS_TAB, PROMPTS_HEADERS).get_all_records()
    _cache["prompts"] = (time.time(), rows)
    return rows


def get_trend_prompts(trend: str) -> list:
    return [r for r in _all_prompts()
            if str(r.get("trend", "")).strip().lower() == trend.strip().lower()
            and str(r.get("t2i_base", "")).strip()]


def processed_sources(trend: str) -> set:
    return {str(r.get("source_file", "")).strip() for r in _all_prompts()
            if str(r.get("trend", "")).strip().lower() == trend.strip().lower()}


def count_added_today() -> int:
    today = str(date.today())
    return sum(1 for r in _all_prompts() if str(r.get("added_on", "")).strip() == today)


def count_prompts_by_trend() -> dict:
    out = {}
    for r in _all_prompts():
        k = str(r.get("trend", "")).strip()
        if k:
            out[k] = out.get(k, 0) + 1
    return out


def append_trend_prompt(trend: str, dna: dict, source_file: str):
    with _lock:
        ws = _ws(PROMPTS_TAB, PROMPTS_HEADERS)
        ws.append_row([
            trend,
            dna.get("style_key", ""),
            dna.get("account", ""),
            dna.get("label", ""),
            dna.get("description", ""),
            dna.get("t2i_base", ""),
            dna.get("niche_affinity", ""),
            source_file,
            str(date.today()),
        ])
        _cache["prompts"] = (0.0, [])
```

### 3.2 `tools/drive_upload.py` (OAuth — app se Drive upload ke liye)
**Kyun:** Service account personal Gmail Drive me file upload kare to `storageQuotaExceeded` aata hai. Isliye upload/folder-create **user ke OAuth refresh token** se hoga. Service account sirf read karega (feeder). Folder user ka hoga aur parent folder service account ko Editor share hoga (access inherit hota hai).

Env vars: `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REFRESH_TOKEN` (scope: `https://www.googleapis.com/auth/drive`).

```python
"""tools/drive_upload.py — Drive upload/folder-create using USER OAuth (not service account)."""
import io
import logging
import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

logger = logging.getLogger(__name__)

_service = None


def is_configured() -> bool:
    return all(os.environ.get(k) for k in (
        "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_REFRESH_TOKEN"))


def _svc():
    global _service
    if _service is None:
        creds = Credentials(
            token=None,
            refresh_token=os.environ["GOOGLE_OAUTH_REFRESH_TOKEN"],
            client_id=os.environ["GOOGLE_OAUTH_CLIENT_ID"],
            client_secret=os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
            token_uri="https://oauth2.googleapis.com/token",
            scopes=["https://www.googleapis.com/auth/drive"],
        )
        creds.refresh(Request())
        _service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _service


def create_folder(name: str, parent_id: str) -> str:
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
    return _svc().files().create(body=meta, fields="id").execute()["id"]


def upload_image(folder_id: str, filename: str, data: bytes, mimetype: str) -> str:
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mimetype or "image/jpeg", resumable=False)
    meta = {"name": filename, "parents": [folder_id]}
    return _svc().files().create(body=meta, media_body=media, fields="id").execute()["id"]
```

### 3.3 `tools/trend_feeder.py`
Background Vision processing sirf trend folders ke liye.
`create_trend_folder` pehle OAuth se try karta hai (agar configured), warna service account se.

```python
"""
tools/trend_feeder.py — Background Vision processing for TREND folders (NEW).

Reuses analyze_image / drive_service from tools/visions_ai.py (that file is NOT edited).
Images stay in their Drive trend folder; processed ones are tracked by Drive file id
in the Trend_Prompts sheet (source_file column), so nothing is moved or re-processed.
"""
import asyncio
import logging
import os
import time

from sheets import trends as T
from tools import visions_ai as v

logger = logging.getLogger(__name__)

TREND_DAILY_LIMIT = 25
GAP_SECONDS = 30
TREND_DRIVE_PARENT_ID = os.environ.get("TREND_DRIVE_PARENT_ID", "")

_fail_count: dict = {}


def create_trend_folder(name: str) -> str:
    """Create <name> folder inside the shared parent folder. Returns folder id ('' on failure)."""
    if not TREND_DRIVE_PARENT_ID:
        logger.warning("Trend folder not created — TREND_DRIVE_PARENT_ID missing")
        return ""
    # Prefer user OAuth (folder owned by user → uploads from app work). Fallback: service account.
    try:
        from tools import drive_upload
        if drive_upload.is_configured():
            return drive_upload.create_folder(name, TREND_DRIVE_PARENT_ID)
    except Exception as e:
        logger.warning(f"OAuth folder create failed, trying service account: {e}")
    if v.drive_service is None:
        return ""
    try:
        meta = {"name": name, "mimeType": "application/vnd.google-apps.folder",
                "parents": [TREND_DRIVE_PARENT_ID]}
        return v.drive_service.files().create(body=meta, fields="id").execute()["id"]
    except Exception as e:
        logger.error(f"Trend folder create failed: {e}")
        return ""


def run_trend_feeder() -> int:
    """One pass. Returns processed count, 0 if nothing, -3 if daily limit hit."""
    if v.is_stop_requested() or v.drive_service is None:
        return 0

    done_today = T.count_added_today()
    if done_today >= TREND_DAILY_LIMIT:
        logger.info(f"Trend feeder daily limit reached ({TREND_DAILY_LIMIT})")
        return -3

    trends = sorted(T.get_trends(), key=lambda t: not t["active"])  # ON trends first
    processed = 0

    for t in trends:
        folder_id = t["drive_folder_id"]
        if not folder_id:
            continue
        try:
            res = v.drive_service.files().list(
                q=f"'{folder_id}' in parents and trashed=false",
                pageSize=200, fields="files(id, name, mimeType)").execute()
        except Exception as e:
            logger.error(f"[trend:{t['trend']}] Drive list error: {e}")
            continue

        seen = T.processed_sources(t["trend"])
        new_files = [f for f in res.get("files", [])
                     if v._is_image(f) and f["id"] not in seen and _fail_count.get(f["id"], 0) < 2]

        for f in new_files:
            if v.is_stop_requested() or done_today + processed >= TREND_DAILY_LIMIT:
                return processed
            temp = f"/tmp/trend_{f['id']}_{f['name']}"
            try:
                logger.info(f"[trend:{t['trend']}] Analyzing {f['name']}")
                v.download_from_drive(f["id"], temp)
                dna = v.analyze_image(temp)
                T.append_trend_prompt(t["trend"], dna, f["id"])
                processed += 1
                time.sleep(GAP_SECONDS)
            except Exception as e:
                _fail_count[f["id"]] = _fail_count.get(f["id"], 0) + 1
                logger.error(f"[trend:{t['trend']}] {f['name']} failed: {e}")
                time.sleep(60)
            finally:
                if os.path.exists(temp):
                    os.remove(temp)
    return processed


async def trend_feeder_loop():
    """Start in lifespan: asyncio.create_task(trend_feeder_loop())"""
    await asyncio.sleep(60)
    while True:
        try:
            n = await asyncio.to_thread(run_trend_feeder)
        except Exception as e:
            logger.error(f"trend_feeder_loop error: {e}")
            n = 0
        await asyncio.sleep(3600 if n == -3 else (300 if n <= 0 else 30))
```

### 3.4 `mastermind/trend_cmo.py`
Trend path ka CMO. `None` return = regular flow chalega. Existing `node_cmo` ke helpers lazily import karta hai (circular import se bachne ke liye).

```python
"""
mastermind/trend_cmo.py — Trend path for the CMO (NEW).

call_trend_cmo() returns a full strategy dict when at least one trend is ON and has
processed prompts. It returns None otherwise, so node_cmo continues the REGULAR flow.
It never advances the regular Style_Tracker / Prompt rows of Prompts_Master.
"""
import logging
import random
import re

from sheets import load_prompt_tracker, save_prompt_tracker
from sheets.trends import get_active_trends, get_trend_prompts

logger = logging.getLogger(__name__)

_TAIL = ", 4K ultra HD, photorealistic, highly detailed, award-winning photography"


def _camel(kw: str) -> str:
    return "".join(w.capitalize() for w in re.sub(r"[^A-Za-z0-9 ]", " ", kw).split())


def _pick_trend_and_row(account_key: str):
    active = get_active_trends()
    if not active:
        return None, None
    tracker = load_prompt_tracker()

    pick_key = f"trend_pick__{account_key}"
    start = (tracker.get(pick_key, -1) + 1) % len(active)
    tracker[pick_key] = start

    for off in range(len(active)):
        trend = active[(start + off) % len(active)]
        rows = get_trend_prompts(trend["trend"])
        mine = [r for r in rows if str(r.get("account", "")).strip() == account_key] or rows
        if not mine:
            continue
        rkey = f"trend__{trend['trend']}__{account_key}"
        idx = (tracker.get(rkey, -1) + 1) % len(mine)
        tracker[rkey] = idx
        save_prompt_tracker(tracker)
        logger.info(f"   [{account_key}] TREND '{trend['trend']}' prompt [{idx+1}/{len(mine)}]")
        return trend, dict(mine[idx])
    return None, None


def _apply_trend_rules(result: dict, trend: dict, kws: list):
    """Code-level guarantee: keyword in title/desc + trend hashtags in tags."""
    text = f"{result.get('title','')} {result.get('description','')}".lower()
    if not any(k.lower() in text for k in kws):
        result["title"] = f"{kws[0].title()}: {result.get('title','')}"[:90]

    trend_tags = [_camel(k) for k in kws if _camel(k)][:3]
    existing = [t for t in result.get("tags", []) if t not in trend_tags]
    result["tags"] = (trend_tags + existing)[:5]
    result["board_keywords"] = list(dict.fromkeys(kws + result.get("board_keywords", [])))[:4]


def call_trend_cmo(account_key: str, boards: dict = None, trends: dict = None):
    from mastermind import node_cmo as n  # lazy import (avoids circular import)
    from tools.firebase_boards import format_boards_for_prompt, format_trends_for_prompt

    trend, row = _pick_trend_and_row(account_key)
    if not trend:
        return None

    kw_pool = trend["keywords"] or [trend["trend"]]
    kws = random.sample(kw_pool, min(3, len(kw_pool)))

    style_key = str(row.get("style_key", "")).strip() or f"{_camel(trend['trend'])}Style"
    t2i = str(row.get("t2i_base", "")).strip()
    ratio = n._pick_ratio()
    niche_list = [x.strip() for x in str(row.get("niche_affinity", "")).split(",") if x.strip()]
    niche = random.choice(niche_list) if niche_list else "home"

    row["tags"] = ",".join(_camel(k) for k in kws)

    prompt = n._build_forced_style_prompt(
        n._ACCOUNT_PROFILES[account_key], style_key, ratio, t2i,
        sheet_row=row,
        boards_formatted=format_boards_for_prompt(boards or {}),
        trends_formatted=format_trends_for_prompt(trends or {}),
    ) + f"""

TREND MODE (IMPORTANT — overrides the account's default mood)
Active trend: {trend['trend']}
Trend keywords for this pin: {', '.join(kws)}
- Follow the trend's mood and theme even if the account profile says bright/warm.
- Title and description MUST naturally use at least one of the trend keywords.
- Tags MUST include CamelCase versions of these keywords: {', '.join(_camel(k) for k in kws)}
- Keep the JSON format exactly as specified above."""

    result = None
    for name, fn in (("Gemini", n._call_gemini_sync), ("Groq", n._call_groq_sync)):
        try:
            result = n._extract_json(fn(prompt))
            n._validate(result, account_key)
            logger.info(f"   [{account_key}] Trend copy via {name}")
            break
        except Exception as e:
            logger.error(f"   [{account_key}] Trend {name} failed: {type(e).__name__}: {e}")
            result = None

    if result is None:  # zero-LLM build from the trend row
        label = str(row.get("label", "")).strip() or trend["trend"]
        desc = str(row.get("description", "")).strip() or f"{trend['trend']} aesthetic."
        result = {
            "pin_type": "VIRAL_PIN",
            "strategy": f"Trend Direct (no LLM) — {trend['trend']}",
            "vibe": desc[:80],
            "title": f"{kws[0].title()} Ideas You'll Want To Save"[:90],
            "description": desc[:380],
            "tags": [],
            "alt_text": f"{label} — {kws[0]} aesthetic photography"[:200],
            "visual_prompt": t2i.rstrip(", ") + (_TAIL if "4K ultra HD" not in t2i else ""),
        }

    result["pin_type"] = "VIRAL_PIN"
    result["visual_style"] = style_key
    result["ratio"] = result.get("ratio", ratio)
    result["niche"] = niche
    result["trend"] = trend["trend"]
    _apply_trend_rules(result, trend, kws)
    return result
```

### 3.5 `trend_routes.py` (repo root, FastAPI router)
```python
"""trend_routes.py — API for the dashboard (NEW)."""
import asyncio
import os
from typing import List

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from sheets import trends as T
from tools.trend_feeder import TREND_DAILY_LIMIT, create_trend_folder

router = APIRouter(prefix="/trends", tags=["trends"])

REGULAR = "Regular"


class AddTrend(BaseModel):
    trend: str
    keywords: str = ""


class Toggle(BaseModel):
    trend: str
    active: bool


class Keywords(BaseModel):
    trend: str
    keywords: str


@router.get("")
async def list_trends():
    trends = await asyncio.to_thread(T.get_trends)
    counts = await asyncio.to_thread(T.count_prompts_by_trend)
    return [{**t, "prompts": counts.get(t["trend"], 0)} for t in trends]


@router.get("/feeder-status")
async def feeder_status():
    today = await asyncio.to_thread(T.count_added_today)
    return {"processed_today": today, "daily_limit": TREND_DAILY_LIMIT}


@router.post("/add")
async def add_trend(body: AddTrend):
    name = body.trend.strip()
    if not name or name.lower() == REGULAR.lower():
        raise HTTPException(400, "valid trend name required")
    folder_id = await asyncio.to_thread(create_trend_folder, name)
    ok = await asyncio.to_thread(T.add_trend, name, body.keywords, folder_id)
    if not ok:
        raise HTTPException(409, "trend already exists")
    return {"ok": True, "trend": name, "drive_folder_id": folder_id}


@router.post("/toggle")
async def toggle(body: Toggle):
    if not await asyncio.to_thread(T.set_trend_active, body.trend, body.active):
        raise HTTPException(404, "trend not found")
    return {"ok": True, "trend": body.trend, "active": body.active}


@router.post("/keywords")
async def keywords(body: Keywords):
    if not await asyncio.to_thread(T.set_trend_keywords, body.trend, body.keywords):
        raise HTTPException(404, "trend not found")
    return {"ok": True}


@router.post("/upload")
async def upload(target: str = Form(...), files: List[UploadFile] = File(...)):
    """
    target = "Regular"  -> existing Drive INPUT folder (purana Vision Feeder process karega)
    target = <trend>    -> us trend ka Drive folder (Trend Feeder background me process karega)
    NO Vision call here — sirf Drive me save + queued count.
    """
    from tools import drive_upload
    if not drive_upload.is_configured():
        raise HTTPException(500, "Drive OAuth not configured (GOOGLE_OAUTH_* env vars)")

    if target.strip().lower() == REGULAR.lower():
        from tools.visions_ai import DRIVE_INPUT_FOLDER_ID
        folder_id = DRIVE_INPUT_FOLDER_ID
    else:
        match = next((t for t in await asyncio.to_thread(T.get_trends)
                      if t["trend"].lower() == target.strip().lower()), None)
        if not match or not match["drive_folder_id"]:
            raise HTTPException(404, "trend or its Drive folder not found")
        folder_id = match["drive_folder_id"]

    saved, failed = 0, []
    for f in files:
        try:
            data = await f.read()
            await asyncio.to_thread(drive_upload.upload_image, folder_id,
                                    f.filename or "image.jpg", data, f.content_type or "image/jpeg")
            saved += 1
        except Exception as e:
            failed.append({"file": f.filename, "error": str(e)[:200]})
    if saved == 0:
        raise HTTPException(502, f"upload failed: {failed[:3]}")
    return {"ok": True, "target": target, "queued": saved, "failed": failed}
```
Dependency: `python-multipart` ko `requirements.txt` me add karo (file upload ke liye zaroori).

### 3.6 `static/trends.html` (mobile-first single page)
Spec (agent banaye, existing `static/index.html` ki styling/theme follow kare):
- Mobile-first, ek file, vanilla JS (`fetch` se upar ke endpoints call karo). Koi build step nahi.
- **3 tabs:**
  1. **Trends** — `GET /trends` se list. Har trend card: naam, prompts count, **ON/OFF toggle** (`POST /trends/toggle`), keywords dikhao + **"Edit keywords"** (`POST /trends/keywords`). Upar **"+ Add Trend"** button → form (trend naam + keywords textarea) → `POST /trends/add`. Success pe list refresh.
  2. **Upload** — dropdown: `Regular` + saare trends. Multiple image picker (`accept="image/*" multiple`). Upload `POST /trends/upload` (`multipart/form-data`: `target`, `files`). Progress/"N queued" message. Error saaf dikhao (silent fail nahi). Bahut saari images ho to 5-5 ke batch me bhejo (request size/timeouts ke liye).
  3. **Status** — `GET /trends` + `GET /trends/feeder-status`: kaunsa trend ON hai, har trend ke prompts, aaj kitni images process hui (X / limit).
- Toggle ke baad turant UI update; error pe wapas revert + message.
- Serve: `main.py` me route `GET /trends-ui` → `FileResponse("static/trends.html")` (NOTE: `/trends` already API prefix hai, isliye UI ka path `/trends-ui`). Chahe to `index.html` me iska link bhi daal do.

---

## 4. EXISTING FILES ME EDITS (sirf 3)

### 4.1 `mastermind/node_cmo.py`
`_call_cmo_for_account(...)` ke **bilkul shuru me**, `ratio = _pick_ratio()` se PEHLE:
```python
    # ── TREND PATH (new) — koi trend ON ho tabhi chalega, warna None → regular flow ──
    try:
        from mastermind.trend_cmo import call_trend_cmo
        _trend_result = call_trend_cmo(account_key, boards, trends)
        if _trend_result:
            return _trend_result
    except Exception as _te:
        logger.warning(f"[{account_key}] Trend path failed → regular flow: {type(_te).__name__}: {_te}")
```
Aur kuch nahi badalna (VISUAL_STYLES, HARDCODED_FALLBACK, rotation engine sab as-is). Trend mode me regular `Style_Tracker`/`Prompt_Tracker` rows advance nahi hote (sirf `trend__...` aur `trend_pick__...` keys use hoti hain, wo bhi existing `load_prompt_tracker/save_prompt_tracker` se).

### 4.2 `main.py`
- Imports: `from sheets.trends import init_trend_tabs`, `from tools.trend_feeder import trend_feeder_loop`, `from trend_routes import router as trend_router`.
- `app = FastAPI(...)` ke baad: `app.include_router(trend_router)`.
- `lifespan()` me `init_sheets()` ke baad:
```python
try:
    init_trend_tabs()
except Exception as e:
    logger.warning(f"Trend tabs init failed: {e}")
asyncio.create_task(trend_feeder_loop())
```
- Route add: `GET /trends-ui` → `static/trends.html`.

### 4.3 `requirements.txt`
`python-multipart` add karo. (`google-auth`, `google-api-python-client`, `gspread` already hain.)

---

## 5. ENV VARS (Render me add)
| Var | Kya |
|---|---|
| `TREND_DRIVE_PARENT_ID` | Drive ka parent folder (jaise "Pinteresto-Trends") ka ID. Service account email ko iska **Editor** access do. |
| `GOOGLE_OAUTH_CLIENT_ID` | OAuth client (Google Cloud Console → Credentials → OAuth client, Desktop/Web) |
| `GOOGLE_OAUTH_CLIENT_SECRET` | wahi |
| `GOOGLE_OAUTH_REFRESH_TOKEN` | Ek baar consent flow (scope `drive`) chala ke mila refresh token |

Agent ko ek chhota helper script `scripts/get_drive_refresh_token.py` bhi banana hai (google-auth-oauthlib `InstalledAppFlow`, `run_local_server` ya console flow) jo refresh token print kare, aur `docs` me 5-line setup guide likhna hai (mobile/Codespaces me bhi chal sake — Codespaces me port-forward ya manual code paste flow use karo).

> OAuth consent screen "Testing" mode me refresh token 7 din me expire hota hai → isko **"In production"** publish karna (ya user ko batana).

---

## 6. EDGE CASES / DHYAN DENE WALI BAATEIN
1. **Circular import:** `trend_cmo` ko `node_cmo` import sirf function ke andar karna hai (upar diya hai, waise hi rakho).
2. **Trend ON but prompts 0** (images abhi process nahi hui): `call_trend_cmo` `None` return karta hai → regular flow. Pin miss nahi hota.
3. **Do trend ON:** `trend_pick__{account}` tracker se alternate hote hain.
4. **Cache:** trend settings/prompts cache 60s; toggle/edit pe turant clear.
5. **Gemini quota:** trend feeder 25/day + 30s gap; regular feeder alag limit (10/day). Dono ek hi Gemini key share karte hain — `analyze_image` me fallback key already hai.
6. **Duplicate processing se bachav:** Drive file id `source_file` me store hoti hai; failed image 2 baar fail ho to us run me skip (`_fail_count`).
7. **Render free plan so jata hai** → scheduler/feeder ruk jayega. Always-on instance ya UptimeRobot ping chahiye. Local JSON files restart pe udti hain (tracker Sheets me hi rahe).
8. **Trend mode me hardcoded `VISUAL_STYLES` use nahi hote** (trend path alag hai), isliye Halloween ke beech boho/sage style nahi aayega.
9. `Vision` ka `analyze_image` prompt `tags` aur `account` deta hai — trend rows me `tags` ignore karo (keywords user ke hain).

---

## 7. TEST CHECKLIST (implementation ke baad)
1. App start → `Trend_Settings` aur `Trend_Prompts` tabs bane (headers ke saath).
2. `/trends-ui` → Add Trend ("Halloween", keywords) → sheet me row (`active=FALSE`) + Drive me folder bana.
3. Upload tab → Halloween chuno + 2-3 images → "N queued"; Drive folder me files dikhi; **koi Vision call nahi hui**.
4. ~1-2 min baad (feeder loop) `Trend_Prompts` me rows aayi (`trend=Halloween`, `source_file`=Drive id). Restart ke baad dobara process NA ho.
5. Toggle OFF rakhke `POST /trigger/account_1` → log me regular flow (Style Rotation…), trend log nahi.
6. Toggle ON → `/trigger/account_1` → log: `TREND 'Halloween' prompt [1/N]`; title/description me keyword; tags me CamelCase keywords.
7. Trend ON, prompts 0 wala naya trend → regular flow chala (fallback).
8. Trend path me jaanbujhkar error (galat sheet name) → warning log + regular flow, pin phir bhi post.
9. Upload target = Regular → file Drive Input folder me gayi; purana feeder uthaya.
10. Sab trends OFF → bilkul pehle jaisa behaviour.

---

## 8. FILE CHECKLIST (summary)
**Naye:** `sheets/trends.py`, `tools/drive_upload.py`, `tools/trend_feeder.py`, `mastermind/trend_cmo.py`, `trend_routes.py`, `static/trends.html`, `scripts/get_drive_refresh_token.py`
**Edit:** `mastermind/node_cmo.py` (1 hook), `main.py` (imports + router + lifespan + `/trends-ui`), `requirements.txt`
**Touch nahi:** `tools/visions_ai.py`, `sheets/prompts_master.py`, `Prompts_Master` sheet, regular scheduler/agent/blog/pinterest flow.
