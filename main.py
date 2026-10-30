import asyncio
import json
import logging
import os
import random
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pydantic import BaseModel

from agent import run_agent, fill_missing_niches
from mastermind.graph import run_mastermind
from sheets import get_all_products
from sheets.schedule_state import load_daily_plan, save_daily_plan
from tools.llm import chat
from config import GEMINI_API_KEY, GEMINI_CHAT_MODEL
from tools.visions_ai import (
    run_feeder_agent, get_vision_stats,
    request_stop as vf_request_stop,
    request_start as vf_request_start,
)
from tools import run_control
from sheets.trends import init_trend_tabs
from tools.trend_feeder import trend_feeder_loop
from trend_routes import router as trend_router
from product_routes import router as kits_router

# ── Display timezone ─────────────────────────────────────────────────────────
# The scheduler itself stays on America/New_York (pin slots target the US
# audience), but every time SHOWN to the owner (app/dashboard) is Indian
# time — he runs this from India. Convert properly via astimezone: the old
# "+5:30 on EST" hack was wrong by an hour during US daylight saving.
_IST = ZoneInfo("Asia/Kolkata")

def _now_ist_str() -> str:
    """Current time in India, owner-readable: '10:20 PM'."""
    return datetime.now(_IST).strftime("%I:%M %p")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

# ── In-memory Log Buffer
import socket
import dns.resolver

_dns_cache = {}
_original_getaddrinfo = socket.getaddrinfo

def _patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if host in _dns_cache:
        ip = _dns_cache[host]
    else:
        try:
            resolver = dns.resolver.Resolver()
            resolver.nameservers = ["1.1.1.1", "8.8.8.8"]  # Cloudflare + Google DNS
            answer = resolver.resolve(host, "A")
            ip = answer[0].to_text()
            _dns_cache[host] = ip
        except Exception:
            return _original_getaddrinfo(host, port, family, type, proto, flags)
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port))]

socket.getaddrinfo = _patched_getaddrinfo
import collections

class _MemHandler(logging.Handler):
    """Keeps last MAX_LINES log records in a deque — thread-safe reads."""
    MAX_LINES = 500

    def __init__(self):
        super().__init__()
        self._buf: collections.deque = collections.deque(maxlen=self.MAX_LINES)

    def emit(self, record: logging.LogRecord):
        try:
            line = self.format(record)
            self._buf.append(line)
        except Exception:
            pass

    def get_lines(self, n: int = 200) -> list:
        lines = list(self._buf)
        return lines[-n:] if n < len(lines) else lines

_mem_handler = _MemHandler()
_mem_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
logging.getLogger().addHandler(_mem_handler)

# ── Gemini Client (for CMO chat) ───────────────────────────────────────────────
try:
    from google import genai as _genai
    _gemini_client = _genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
except Exception:
    _gemini_client = None

# ── Global State ──────────────────────────────────────────────────────────────
state = {
    "running": False,
    "last_run": None,
    "posted_today": 0,
    "last_summary": "Not run yet",
    "mastermind_running": False,
    "mastermind_last_run": None,
    "mastermind_summary": "Awaiting first cycle...",
    "mastermind_a1_strategy": "—",
    "mastermind_a2_strategy": "—",
    "mastermind_a1_posted": False,
    "mastermind_a2_posted": False,
    "mastermind_fallback": False,
    "stop_requested": False,
    "vision_feeder_running": False,
    "vision_feeder_paused": False,
    # ── Blog Engine (V4) ───────────────────────────────────────
    "blog_running": False,
    "blog_last_url": "",
    "blog_last_run": None,
    "blog_last_account": "—",
}

scheduler = AsyncIOScheduler(timezone="America/New_York")

# ── Mastermind Job ─────────────────────────────────────────────────────────────
async def mastermind_scheduled_job(trigger: str):
    # ── GLOBAL STOP gate ─────────────────────────────────────────────────────
    # Scheduled pin slots: STOP laga ho toh poora cycle skip — koi Gemini
    # call nahi, koi generation nahi. Manual Run (app ka button) owner ka
    # resume signal hai, isliye woh stop saaf karke aage badhta hai.
    if trigger.startswith("manual"):
        if run_control.is_stopped():
            run_control.clear_stop()
            logger.info("▶️ [Scheduler] Manual run — STOP saaf, system resume.")
    elif run_control.is_stopped():
        logger.info(f"⏸️ [Scheduler] STOP active hai — {trigger} skip.")
        return
    if state["mastermind_running"]:
        logger.warning(f"⚠️ Mastermind already running. Skipping {trigger}")
        return
    state["mastermind_running"] = True
    state["stop_requested"] = False
    state["mastermind_last_run"] = _now_ist_str()
    try:
        logger.info(f"🧠 Mastermind Triggered: {trigger}")
        result = await run_mastermind(trigger=trigger)
        state["mastermind_summary"]    = result.get("summary", "Done")
        state["mastermind_a1_strategy"] = result.get("a1_strategy", "—")
        state["mastermind_a2_strategy"] = result.get("a2_strategy", "—")
        state["mastermind_a1_posted"]   = result.get("a1_posted", False)
        state["mastermind_a2_posted"]   = result.get("a2_posted", False)
        state["mastermind_fallback"]    = result.get("fallback_triggered", False)
        state["posted_today"] += (1 if result.get("a1_posted") else 0) + (1 if result.get("a2_posted") else 0)
    except Exception as e:
        logger.error(f"❌ Mastermind Error: {e}")
        state["mastermind_summary"] = f"Error: {e}"
    finally:
        state["mastermind_running"] = False

# ── Smart Daily Scheduler ──────────────────────────────────────────────────────
#
# Scheduler timezone: America/New_York (pins US audience ke liye hain).
# IST sirf DISPLAY ke liye hai (app/logs) — scheduling hamesha ek hi
# timezone par hoti hai, kabhi dono par nahi.
# Pins   : 20 total per day — 10 per account
# Gap    : random 60–72 min between consecutive pins (interleaved A1/A2)
# Layout : slots interleaved  →  A1, A2, A1, A2 ... (20 slots total)
# Split  : 100% VIRAL_PIN
# Plan   : Sheets (Schedule_State) me persist — restart par resume.
#
# Trigger string format: "scheduled-account1-VIRAL_PIN"
#   → Agent Executor posts ONLY the specified account
#
# ─────────────────────────────────────────────────────────────────────────────

def _random_daily_split() -> list:
    """100% VIRAL_PIN — all 10 slots are aesthetic AI-generated pins."""
    return ["VIRAL_PIN"] * 10


def schedule_daily_pins(force: bool = False):
    """
    Register today's 20 pins (10 per account, 60-72 min gaps, interleaved).

    Plan PERSISTENCE (30 Oct 2026): the day's slot plan is saved to Sheets
    (Schedule_State tab). A restart/deploy RESUMES the same plan — slots
    whose time has passed are skipped, the rest fire at their planned
    times. Before this, every restart threw the plan away and started a
    fresh train 5-15 min later, so deploy-heavy days posted in bursts.
    A brand-new plan is made only for a new day (midnight cron, force=True)
    or when no saved plan exists.
    """
    if force and run_control.is_stopped():
        # Naya din = naya mauka: kal ka STOP naye plan par carry nahi hoga.
        run_control.clear_stop()
        logger.info("▶️ [Scheduler] Naye din ka plan — kal ka STOP saaf kar diya.")
    tz  = scheduler.timezone
    now = datetime.now(tz=tz)

    TOTAL_PINS  = 20
    MIN_GAP     = 60   # minutes between consecutive pins (any account)
    MAX_GAP     = 72   # minutes

    today = now.strftime("%Y-%m-%d")
    plan  = None if force else load_daily_plan()
    if plan and plan.get("day") == today and len(plan.get("slots", [])) == TOTAL_PINS:
        slots   = [int(s) for s in plan["slots"]]
        resumed = True
    else:
        slots = []
        current = random.randint(5, 15)   # first pin: 5–15 min from plan time
        for _ in range(TOTAL_PINS):
            slots.append(current)
            current += random.randint(MIN_GAP, MAX_GAP)
        slots.sort()
        save_daily_plan({"day": today, "slots": slots,
                         "created": now.isoformat(timespec="seconds")})
        resumed = False

    # ── Random pin-type split for each account ─────────────────────────────────
    a1_plan = _random_daily_split()   # 10x VIRAL_PIN
    a2_plan = _random_daily_split()   # 10x VIRAL_PIN

    # Slots are offsets from the PLAN's creation moment — on a resume that
    # moment is in the past, so anchor to it (na ki restart ke 'now' par).
    base = now
    if resumed:
        try:
            base = datetime.fromisoformat(plan["created"])
            if base.tzinfo is None:
                base = base.replace(tzinfo=tz)
        except Exception:
            base = now

    # ── Remove old scheduled pin jobs ─────────────────────────────────────────
    for job in scheduler.get_jobs():
        if job.id.startswith("pin_"):
            scheduler.remove_job(job.id)

    # ── Register jobs — interleaved A1/A2 (beet gaye slots skip) ──────────────
    a1_idx = a2_idx = scheduled = skipped_past = 0

    for i, offset_min in enumerate(slots):
        run_time = base + timedelta(minutes=offset_min)
        if run_time <= now + timedelta(seconds=30):
            skipped_past += 1
            if i % 2 == 0:
                a1_idx += 1
            else:
                a2_idx += 1
            continue

        if i % 2 == 0 and a1_idx < 10:         # even slot → Account 1
            pin_type = a1_plan[a1_idx]
            scheduler.add_job(
                mastermind_scheduled_job, "date", run_date=run_time,
                id=f"pin_a1_{a1_idx + 1}",
                kwargs={"trigger": f"scheduled-account1-{pin_type}"},
            )
            logger.info(f"📌 [Acc1 #{a1_idx+1}] {run_time.strftime('%I:%M %p')} US (India: {run_time.astimezone(_IST).strftime('%I:%M %p')}) → {pin_type}")
            a1_idx   += 1
            scheduled += 1

        elif i % 2 == 1 and a2_idx < 10:       # odd slot  → Account 2
            pin_type = a2_plan[a2_idx]
            scheduler.add_job(
                mastermind_scheduled_job, "date", run_date=run_time,
                id=f"pin_a2_{a2_idx + 1}",
                kwargs={"trigger": f"scheduled-account2-{pin_type}"},
            )
            logger.info(f"📌 [Acc2 #{a2_idx+1}] {run_time.strftime('%I:%M %p')} US (India: {run_time.astimezone(_IST).strftime('%I:%M %p')}) → {pin_type}")
            a2_idx   += 1
            scheduled += 1

    logger.info(
        f"✅ Daily schedule ready — {scheduled}/20 pins registered"
        f"{' (RESUMED saved plan)' if resumed else ' (new plan)'}\n"
        f"   Beet gaye slots skip: {skipped_past} | Acc1 next idx: {a1_idx} | Acc2 next idx: {a2_idx}\n"
        f"   Gap between pins: {MIN_GAP}–{MAX_GAP} min random (interleaved A1/A2)"
    )
    # Reset daily counter
    state["posted_today"] = 0


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Auto-create all required Google Sheet tabs on startup ──────────────────
    try:
        from sheets import init_sheets
        init_sheets()
    except Exception as _e:
        logger.warning(f"⚠️ Sheet auto-init skipped — {_e}")

    try:
        init_trend_tabs()
    except Exception as _e:
        logger.warning(f"⚠️ Trend tabs init failed: {_e}")

    # Daily re-scheduler — fires at midnight (scheduler tz = America/New_York),
    # force=True → naye din ka NAYA plan banta hai
    scheduler.add_job(schedule_daily_pins, "cron", hour=0, minute=0,
                      id="daily_scheduler", kwargs={"force": True})

    # ── Watchdog: every 30 min check if no pin jobs are registered → re-schedule ──
    # This survives HuggingFace hibernation / missed cron wakeups
    async def pin_watchdog():
        while True:
            await asyncio.sleep(1800)  # check every 30 minutes
            try:
                pin_jobs = [j for j in scheduler.get_jobs() if j.id.startswith("pin_")]
                if not pin_jobs:
                    logger.warning("⚠️ Watchdog: No pin jobs found — re-scheduling now...")
                    schedule_daily_pins()
                else:
                    logger.info(f"✅ Watchdog: {len(pin_jobs)} pin job(s) still active.")
            except Exception as e:
                logger.error(f"❌ Watchdog error: {e}")

    schedule_daily_pins()
    scheduler.start()
    asyncio.create_task(pin_watchdog())
    logger.info("✅ Smart Scheduler Active — 20 pins/day (10 per account) | scheduler tz America/New_York (US audience) | Gap: 60–72 min between pins | Plan Sheets me saved — restart par resume hota hai")

    # ── Vision Feeder: Google Drive loop ─────────────────────────────────────
    async def vision_feeder_loop():
        logger.info("👁️ Vision Feeder Agent background task registered.")
        logger.info("👁️ Vision Feeder Agent started in background...")
        while True:
            try:
                result = await asyncio.to_thread(run_feeder_agent)
                if result == -3:
                    logger.info("👁️ Vision Feeder: daily limit reached — sleeping 24h.")
                    await asyncio.sleep(86400)
                elif result == -2:
                    await asyncio.sleep(60)
                elif result == 0:
                    logger.info("👁️ Vision Feeder: Drive empty — sleeping 5 minutes...")
                    await asyncio.sleep(300)
                else:
                    await asyncio.sleep(300)
            except Exception as _e:
                logger.error(f"❌ Vision Feeder loop error: {_e}")
                await asyncio.sleep(300)

    asyncio.create_task(vision_feeder_loop())
    trend_task = asyncio.create_task(trend_feeder_loop())

    yield
    trend_task.cancel()
    try:
        await trend_task
    except asyncio.CancelledError:
        pass
    scheduler.shutdown()

# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(title="Pinteresto Mastermind", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(trend_router)
app.include_router(kits_router)
app.add_middleware(CORSMiddleware, allow_origins=["https://pinteresto.vercel.app", "https://autom-a6tl.onrender.com"], allow_methods=["*"], allow_headers=["*"])
from security import ApiKeyMiddleware
app.add_middleware(ApiKeyMiddleware)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def dashboard():
    return FileResponse("static/index.html")

@app.get("/trends-ui")
async def trends_dashboard():
    return FileResponse("static/trends.html")

# ── Stats ──────────────────────────────────────────────────────────────────────
@app.get("/api/stats")
async def get_all_stats():
    try:
        products = get_all_products()
        pending = sum(1 for p in products if p.get("Status") == "PENDING")
        posted  = sum(1 for p in products if p.get("Status") == "POSTED")
        total   = len(products)
    except Exception:
        pending = posted = total = 0
    return {
        "running":               state["running"],
        "pending":               pending,
        "posted":                posted,
        "total":                 total,
        "posted_today":          state["posted_today"],
        "last_action":           state["last_run"] or "—",
        "last_summary":          state["last_summary"],
        "vision_feeder_running": _feeder_running(),
    }

# ── Mastermind Stats ───────────────────────────────────────────────────────────
@app.get("/api/next-pins")
async def get_next_pins():
    """Peek at what the next pin will be for each account — no trackers advanced."""
    try:
        from mastermind.node_cmo import peek_next_pin_info
        return {"status": "ok", "next_pins": peek_next_pin_info()}
    except Exception as e:
        return {"status": "error", "error": str(e), "next_pins": {}}

@app.get("/api/mastermind/stats")
async def get_mastermind_stats():
    jobs = scheduler.get_jobs()
    scheduled_slots = [
        {
            "id": j.id,
            # Owner-facing time is IST ("10:20 PM"); the US time stays
            # available for reference because slots are planned on US time.
            "next_run": j.next_run_time.astimezone(_IST).strftime("%I:%M %p") if j.next_run_time else "—",
            "next_run_us": j.next_run_time.strftime("%I:%M %p %Z") if j.next_run_time else "—",
        }
        for j in jobs if j.id.startswith("pin_")
    ]
    return {
        "running":      state["mastermind_running"],
        "last_run":     state["mastermind_last_run"] or "Never",
        "summary":      state["mastermind_summary"],
        "a1_strategy":  state["mastermind_a1_strategy"],
        "a2_strategy":  state["mastermind_a2_strategy"],
        "a1_posted":    state["mastermind_a1_posted"],
        "a2_posted":    state["mastermind_a2_posted"],
        "fallback":     state["mastermind_fallback"],
        "scheduled_slots": scheduled_slots,
    }

# ── Products ───────────────────────────────────────────────────────────────────
@app.get("/api/products")
async def get_products():
    try:
        products = get_all_products()
        return {"products": products[:50]}
    except Exception as e:
        return {"products": [], "error": str(e)}

# ── Mastermind Run / Stop ──────────────────────────────────────────────────────
@app.post("/api/mastermind/run")
async def run_mastermind_api(background_tasks: BackgroundTasks):
    if state["mastermind_running"]:
        return {"status": "busy", "message": "Mastermind already running!"}
    background_tasks.add_task(mastermind_scheduled_job, "manual-both")
    return {"status": "started"}

@app.post("/api/mastermind/run-account1")
async def run_mm_a1(background_tasks: BackgroundTasks):
    if state["mastermind_running"]:
        return {"status": "busy"}
    background_tasks.add_task(mastermind_scheduled_job, "manual-account1")
    return {"status": "started"}

@app.post("/api/mastermind/run-account2")
async def run_mm_a2(background_tasks: BackgroundTasks):
    if state["mastermind_running"]:
        return {"status": "busy"}
    background_tasks.add_task(mastermind_scheduled_job, "manual-account2")
    return {"status": "started"}

@app.post("/api/mastermind/stop")
async def stop_mastermind():
    # ASLI STOP (30 Oct 2026): pehle ye sirf ek flag tha jise agla scheduled
    # job reset kar deta tha. Ab ye global switch hai — scheduler, image
    # pipeline (retry loop samet) aur vision feeder, teeno rukte hain.
    state["stop_requested"] = True
    run_control.request_stop()
    vf_request_stop()
    state["vision_feeder_paused"] = True
    logger.warning("🛑 [STOP] Owner ne STOP lagaya — pins + feeder sab band.")
    return {"status": "stopped", "message": "Sab kuch rok diya gaya: scheduled pins skip honge, chal raha upload retry abort hoga, vision feeder bhi band."}

# ── Blog Engine — Standalone Runner ───────────────────────────────────────────

async def _run_blog_pipeline(account: str):
    """
    Standalone 4-node blog pipeline runner.
    Does NOT require a fresh pin — uses LAST_POSTED_IMAGE_URL from agent module.
    """
    if state["blog_running"]:
        logger.warning("⚠️ Blog pipeline already running — skip")
        return

    state["blog_running"]      = True
    state["blog_last_account"] = account
    state["blog_last_run"]     = _now_ist_str()

    try:
        from agent import LAST_POSTED_IMAGE_URL
        from mastermind.node_blog_trigger import node_blog_trigger
        from mastermind.node_product_researcher import node_product_researcher
        from mastermind.node_blog_writer import node_blog_writer
        from mastermind.node_firebase_publisher import node_firebase_publisher

        trigger = f"manual-{account}"
        img_url = LAST_POSTED_IMAGE_URL or ""

        # Determine CMO strategy from last mastermind run
        if account == "account1":
            cmo_strategy = {
                "strategy":   state.get("mastermind_a1_strategy", "VIRAL_PIN"),
                "style_name": state.get("mastermind_a1_strategy", "aesthetic"),
                "pin_type":   "VIRAL_PIN",
            }
        else:
            cmo_strategy = {
                "strategy":   state.get("mastermind_a2_strategy", "VIRAL_PIN"),
                "style_name": state.get("mastermind_a2_strategy", "aesthetic"),
                "pin_type":   "VIRAL_PIN",
            }

        initial_state = {
            # Required for node_blog_writer
            "a1_cmo_strategy":      cmo_strategy if account == "account1" else {},
            "a2_cmo_strategy":      cmo_strategy if account == "account2" else {},
            # Blog pipeline fields
            "last_posted_image_url": img_url,
            "should_create_blog":    False,
            "blog_products":         [],
            "blog_content":          {},
            "blog_url":              "",
            "blog_published":        False,
            # Trigger
            "cycle_trigger": trigger,
            # Required by state schema (ignored in standalone run)
            "a1_raw_analytics":  [],
            "a2_raw_analytics":  [],
            "a1_final_seo_copy": {},
            "a2_final_seo_copy": {},
            "a1_publish_status": {},
            "a2_publish_status": {},
            "fallback_triggered": False,
        }

        s = await node_blog_trigger(initial_state)
        s = await node_product_researcher(s)
        s = await node_blog_writer(s)
        s = await node_firebase_publisher(s)

        blog_url = s.get("blog_url", "")
        if blog_url:
            state["blog_last_url"] = blog_url
            logger.info(f"✅ Blog published [{account}]: {blog_url}")
        else:
            logger.warning(f"⚠️ Blog pipeline ran but no URL returned [{account}]")

    except Exception as e:
        logger.error(f"❌ Blog pipeline error [{account}]: {e}")
    finally:
        state["blog_running"] = False


# ── Blog Engine API Endpoints ──────────────────────────────────────────────────

@app.get("/api/blog/stats")
async def blog_stats():
    """Return today's blog counts + last blog info."""
    counts = {"account1": 0, "account2": 0, "limit": 5}
    try:
        from tools.firebase_publisher import get_daily_blog_counts
        counts = await get_daily_blog_counts()
    except Exception:
        pass
    return {
        "running":      state["blog_running"],
        "last_url":     state["blog_last_url"],
        "last_run":     state["blog_last_run"] or "—",
        "last_account": state["blog_last_account"],
        "a1_count":     counts.get("account1", 0),
        "a2_count":     counts.get("account2", 0),
        "limit":        counts.get("limit", 5),
    }

@app.post("/api/blog/run-account1")
async def blog_run_a1(background_tasks: BackgroundTasks):
    if state["blog_running"]:
        return {"status": "busy", "message": "Blog pipeline already running!"}
    background_tasks.add_task(_run_blog_pipeline, "account1")
    return {"status": "started", "message": "Blog pipeline started for Account 1 🏠"}

@app.post("/api/blog/run-account2")
async def blog_run_a2(background_tasks: BackgroundTasks):
    if state["blog_running"]:
        return {"status": "busy", "message": "Blog pipeline already running!"}
    background_tasks.add_task(_run_blog_pipeline, "account2")
    return {"status": "started", "message": "Blog pipeline started for Account 2 💻"}

@app.post("/api/blog/run-both")
async def blog_run_both(background_tasks: BackgroundTasks):
    if state["blog_running"]:
        return {"status": "busy", "message": "Blog pipeline already running!"}
    async def _run_both():
        await _run_blog_pipeline("account1")
        await _run_blog_pipeline("account2")
    background_tasks.add_task(_run_both)
    return {"status": "started", "message": "Blog pipeline started for both accounts 🚀"}

def _feeder_running() -> bool:
    """Vision Feeder asal me chal raha hai ya nahi — visions_ai ke live
    stats se. (Purana state['vision_feeder_running'] flag kabhi True hota
    hi nahi tha, isliye API hamesha 'running: false' jhooth bolti thi aur
    app ka Stop button mara pada tha — 30 Oct 2026 fix.)"""
    try:
        return bool(get_vision_stats().get("running"))
    except Exception:
        return state["vision_feeder_running"]


# ── Vision Feeder Controls ─────────────────────────────────────────────────────
@app.get("/api/vision/stats")
async def vision_stats():
    vs = get_vision_stats()
    try:
        from sheets.prompts_master import get_prompts_master
        prompts_total = len(get_prompts_master())
    except Exception:
        prompts_total = 0
    return {
        "running":          vs.get("running", False),
        "paused":           vs.get("paused", state["vision_feeder_paused"]),
        "queue_count":      vs["queue_count"],
        "processed_today":  vs["processed_today"],
        "daily_limit":      vs["daily_limit"],
        "drive_done_total": vs.get("drive_done_total", 0),
        "drive_pending":    vs.get("drive_pending", 0),
        "prompts_total":    prompts_total,
        "last_file":        vs["last_file"],
        "last_time":        vs["last_time"],
        "status":           vs["status"],
    }

@app.post("/api/vision/stop")
async def vision_stop():
    vf_request_stop()
    state["vision_feeder_paused"] = True
    logger.info("👁️ Vision Feeder paused via dashboard.")
    return {"status": "paused", "message": "Vision Feeder paused. Current image will finish processing."}

@app.post("/api/vision/start")
async def vision_start():
    vf_request_start()
    state["vision_feeder_paused"] = False
    logger.info("👁️ Vision Feeder resumed via dashboard.")
    return {"status": "running", "message": "Vision Feeder resumed."}


# ── Vision Settings — DYNAMIC daily limit (Pins app se control) ─────────────
class VisionSettingsIn(BaseModel):
    daily_limit: int


@app.get("/api/vision/settings")
async def vision_settings_get():
    """Vision feeder ki current settings — limit Settings sheet se (dynamic)."""
    from tools.visions_ai import get_daily_limit
    return {"daily_limit": get_daily_limit(), "default": 20, "min": 1, "max": 100}


@app.post("/api/vision/settings")
async def vision_settings_set(body: VisionSettingsIn):
    """
    Daily image limit set karo (1–100). Settings sheet me save hota hai —
    feeder agle cycle se nayi limit use karega, bina restart ke.
    """
    from sheets.settings import set_setting
    from tools.visions_ai import get_daily_limit
    limit = max(1, min(100, int(body.daily_limit)))
    if not set_setting("vision_daily_limit", str(limit)):
        return {"status": "error", "message": "Settings save nahi hui (sheet unreachable)."}
    logger.info(f"👁️ Vision daily limit app se set hua: {limit}/day")
    return {"status": "ok", "daily_limit": get_daily_limit()}


# ── Dashboard — app ke home tab ke liye EK call me saara real data ─────────
@app.get("/api/dashboard")
async def dashboard():
    """
    Pins app Dashboard: pins aaj, vision ki asli sthiti (Drive pending /
    processed / prompts ready / aaj ki limit progress), agla pin (IST),
    kits ki ginti — sab ek hi response me.
    """
    vs = get_vision_stats()
    try:
        from sheets.prompts_master import get_prompts_master
        prompts_total = len(get_prompts_master())
    except Exception:
        prompts_total = 0
    try:
        from mastermind.product_router import get_products
        kits_total = len(get_products())  # router ke active kits (sheet se)
        kits_active = kits_total
    except Exception:
        kits_active = kits_total = 0
    next_pin = "—"
    try:
        jobs = scheduler.get_jobs()
        upcoming = [j.next_run_time for j in jobs if j.next_run_time]
        if upcoming:
            next_pin = min(upcoming).astimezone(_IST).strftime("%I:%M %p")
    except Exception:
        pass
    return {
        "pins_today":   state["posted_today"],
        "last_action":  state["last_run"] or "—",
        "running":      state["running"],
        "stopped":      run_control.is_stopped(),
        "vision": {
            "running":         vs.get("running", False),
            "paused":          vs.get("paused", state["vision_feeder_paused"]),
            "status":          vs["status"],
            "processed_today": vs["processed_today"],
            "daily_limit":     vs["daily_limit"],
            "drive_pending":   vs.get("drive_pending", 0),
            "drive_done_total": vs.get("drive_done_total", 0),
            "prompts_total":   prompts_total,
            "last_file":       vs["last_file"],
            "last_time":       vs["last_time"],
        },
        "kits_active": kits_active,
        "kits_total":  kits_total,
        "next_pin_ist": next_pin,
    }

# ── Force Pin + Blog (bypasses daily blog limit) ───────────────────────────────

async def _force_pin_job(account: str):
    """
    Runs a full pin + blog cycle for one account with force_blog=True,
    bypassing the daily blog counter. Reuses the mastermind_running guard.
    """
    if state["mastermind_running"]:
        logger.warning(f"⚠️ Force-pin skipped — mastermind already running ({account})")
        return

    trigger = f"force-pin-{account}"
    state["mastermind_running"] = True
    state["mastermind_last_run"] = _now_ist_str()
    try:
        logger.info(f"⚡ [Force Pin] START — {account} (blog limit bypassed)")
        result = await run_mastermind(trigger=trigger, force_blog=True)
        state["mastermind_a1_strategy"] = result.get("a1_strategy", state["mastermind_a1_strategy"])
        state["mastermind_a2_strategy"] = result.get("a2_strategy", state["mastermind_a2_strategy"])
        state["mastermind_fallback"]    = result.get("fallback_triggered", False)
        state["last_summary"]           = result.get("summary", "")
        logger.info(f"⚡ [Force Pin] DONE — {account}")
    except Exception as e:
        logger.error(f"❌ [Force Pin] Error ({account}): {e}")
        state["last_summary"] = f"Force pin error: {e}"
    finally:
        state["mastermind_running"] = False


@app.post("/api/force-pin/account1")
async def force_pin_a1(background_tasks: BackgroundTasks):
    if state["mastermind_running"]:
        return {"status": "busy", "message": "Mastermind already running — try again shortly."}
    background_tasks.add_task(_force_pin_job, "account1")
    return {"status": "started", "message": "⚡ Force Pin + Blog started for Account 1 (limit bypassed)"}

@app.post("/api/force-pin/account2")
async def force_pin_a2(background_tasks: BackgroundTasks):
    if state["mastermind_running"]:
        return {"status": "busy", "message": "Mastermind already running — try again shortly."}
    background_tasks.add_task(_force_pin_job, "account2")
    return {"status": "started", "message": "⚡ Force Pin + Blog started for Account 2 (limit bypassed)"}

@app.post("/api/force-pin/both")
async def force_pin_both(background_tasks: BackgroundTasks):
    if state["mastermind_running"]:
        return {"status": "busy", "message": "Mastermind already running — try again shortly."}
    async def _run_both():
        await _force_pin_job("account1")
        await _force_pin_job("account2")
    background_tasks.add_task(_run_both)
    return {"status": "started", "message": "⚡ Force Pin + Blog started for Both Accounts (limit bypassed)"}


# ── Account Triggers (legacy + new) ───────────────────────────────────────────
@app.post("/api/run-account1")
async def run_a1(background_tasks: BackgroundTasks):
    if state["mastermind_running"]:
        return {"status": "busy"}
    background_tasks.add_task(mastermind_scheduled_job, "manual-account1")
    return {"status": "started"}

@app.post("/api/run-account2")
async def run_a2(background_tasks: BackgroundTasks):
    if state["mastermind_running"]:
        return {"status": "busy"}
    background_tasks.add_task(mastermind_scheduled_job, "manual-account2")
    return {"status": "started"}

@app.post("/api/stop")
async def stop_all():
    # Dashboard ka STOP ALL — /api/mastermind/stop wala hi global switch.
    state["stop_requested"] = True
    state["running"] = False
    run_control.request_stop()
    vf_request_stop()
    state["vision_feeder_paused"] = True
    logger.warning("🛑 [STOP ALL] Owner ne dashboard se sab roka — pins + feeder band.")
    return {"status": "stopped", "message": "Sab kuch rok diya gaya hai. Resume dabane par hi phir chalega (ya naye din ke plan par)."}


@app.post("/api/resume")
async def resume_all():
    state["stop_requested"] = False
    state["running"] = True
    run_control.clear_stop()
    vf_request_start()
    state["vision_feeder_paused"] = False
    logger.info("▶️ [RESUME] Owner ne system resume kiya — pins + feeder phir chalenge.")
    return {"status": "running", "message": "System resume ho gaya hai."}

# ── Utilities ──────────────────────────────────────────────────────────────────
@app.post("/api/fetch-products")
async def fetch_products_api(background_tasks: BackgroundTasks):
    async def _fetch():
        try:
            result = await fetch_aliexpress_products.ainvoke({"niche": "home"})
            state["last_summary"] = f"Fetched: {result.get('approved', 0)} products approved"
        except Exception as e:
            state["last_summary"] = f"Fetch error: {e}"
    background_tasks.add_task(_fetch)
    return {"status": "started", "message": "Fetching products..."}

@app.post("/api/fill-niches")
async def fill_niches_api(background_tasks: BackgroundTasks):
    async def _fill():
        try:
            result = fill_missing_niches.invoke({})
            state["last_summary"] = result.get("message", "Niches filled")
        except Exception as e:
            state["last_summary"] = f"Niche fill error: {e}"
    background_tasks.add_task(_fill)
    return {"status": "started", "message": "Filling niches..."}

# ── AI Chat Interface ──────────────────────────────────────────────────────────
class ChatMessage(BaseModel):
    message: str

CHAT_SYSTEM_PROMPT = """Tu PINTERESTO hai — "Finisher Tech AI" ka AI assistant jo Pinterest automation empire manage karta hai.
Tu Hinglish (Hindi + English mix) mein baat karta hai. Tu confident, smart aur helpful hai.

SYSTEM KI JANKARI:
- 2 Pinterest accounts hain: Account1 (HomeDecor niches: home, kitchen, cozy, gadgets, organize) aur Account2 (Tech niches: tech, budget, phone, smarthome, wfh)
- Mastermind CMO: Gemini 1.5 analytics dekh ke strategy decide karta hai (Visual Pivot, Viral-Bait, Aggressive Affiliate Strike)
- Visual Pivot / Viral-Bait: T2I image — Pollinations → Puter fallback — affiliate link strip
- Aggressive Affiliate Strike: I2I image via Puter — affiliate link rakho
- Images ImgBB pe upload hoti hain (30 min temp URL), phir Make.com webhook se Pinterest pe jaati hain
- Products Amazon se RapidAPI ke through aate hain, Google Sheet mein store hote hain
- Vision Feeder: Google Drive se images utha ke Gemini Vision se analyze karta hai aur Prompts_Master sheet mein daalata hai

COMMANDS JO TU DETECT KARTA HAI (lowercase dekh):
- "aesthetic pin", "visual pin", "vibe pin" → action: run_aesthetic
- "product pin", "affiliate pin", "money pin" → action: run_product  
- "account 1", "home decor", "acc1" → action: run_acc1
- "account 2", "tech", "acc2" → action: run_acc2
- "mastermind", "cmo", "gemini" → action: run_mastermind
- "status", "kesa hai", "kaisa hai", "update" → action: get_status
- "stop", "rok do", "band karo" → action: stop
- "products fetch", "naye products" → action: fetch_products

Agar command detect ho, response ke END mein likho: [ACTION:action_name]
Warna normal baat kar.

RESPONSE RULES:
- Max 3-4 sentences, crisp aur confident
- Hinglish mein — mix of Hindi aur English natural lagni chahiye
- Emojis use kar lekin overdo mat kar
- Technical details briefly dena, jyada detail avoid"""

@app.post("/api/chat")
async def chat_endpoint(req: ChatMessage, background_tasks: BackgroundTasks):
    msg = req.message.strip()
    if not msg:
        return {"response": "Kuch toh pooch yaar! 😄", "action": None}

    msg_lower = msg.lower()

    # Determine system context for AI
    sys_ctx = (
        f"Current system state: Mastermind {'RUNNING' if state['mastermind_running'] else 'IDLE'}. "
        f"Last run: {state['mastermind_last_run'] or 'Never'}. "
        f"A1 strategy: {state['mastermind_a1_strategy']}. "
        f"A2 strategy: {state['mastermind_a2_strategy']}. "
        f"Today posted: {state['posted_today']} pins. "
        f"Vision Feeder: {'ACTIVE' if _feeder_running() else 'INACTIVE'}."
    )

    full_prompt = f"{CHAT_SYSTEM_PROMPT}\n\n{sys_ctx}\n\nUser: {msg}"

    action = None
    action_msg = ""

    # Execute detected action in background
    async def _do_action(act: str):
        if act == "run_mastermind" or act == "run_aesthetic" or act == "run_product":
            await mastermind_scheduled_job("manual-both")
        elif act == "run_acc1":
            await mastermind_scheduled_job("manual-account1")
        elif act == "run_acc2":
            await mastermind_scheduled_job("manual-account2")
        elif act == "fetch_products":
            try:
                await fetch_aliexpress_products.ainvoke({"niche": "home"})
            except Exception:
                pass
        elif act == "stop":
            state["stop_requested"] = True

    try:
        import asyncio
        ai_response = await asyncio.to_thread(chat, full_prompt, temperature=0.75)

        # Extract action tag from response
        if "[ACTION:" in ai_response:
            parts = ai_response.split("[ACTION:")
            ai_response = parts[0].strip()
            action = parts[1].replace("]", "").strip()
            if not state["mastermind_running"] or action == "stop":
                background_tasks.add_task(_do_action, action)
                action_msg = f" (Action triggered: {action})"

    except Exception as e:
        logger.error(f"Chat LLM error: {e}")
        ai_response = "Bhai, abhi LLM se baat nahi ho pa rahi. Thodi der baad try karo! 🙏"

    return {
        "response": ai_response,
        "action": action,
        "action_msg": action_msg,
    }

# ── CEO Mastermind Chat (Gemini) ───────────────────────────────────────────────
CMO_SYSTEM_PROMPT = """You are the CEO MASTERMIND of "Pinteresto — Finisher Tech AI", a fully autonomous Pinterest marketing empire.
You are a strategic genius who thinks like a top-tier CMO. You speak in a friendly, confident, slightly bold style — like a smart business friend who knows Pinterest inside out.
Mix English with a little Hinglish when it feels natural, but keep it professional and sharp.

YOUR KNOWLEDGE BASE:
- System runs 6 pins/day across 2 Pinterest accounts (3 pins each via scheduled automation)
- Account 1: HomeDecor niches — home, kitchen, cozy, organize, gadgets
- Account 2: Tech niches — tech, budget, phone, smarthome, wfh
- Pin routing: 70% VIRAL_PIN (AI-generated T2I image, strip affiliate link for clean viral reach) / 30% AFFILIATE_PIN (raw product image, keep affiliate link for direct revenue)
- Image generation: Gemini 2.5 Flash Image (primary, 9:16 portrait) → Puter.js free tier (fallback)
- CMO Brain: Gemini 2.5 Flash Lite reads analytics → decides strategy → writes title, description, tags, visual_prompt
- Strategies: Visual Pivot, Viral-Bait, Aggressive Affiliate Strike, Niche Authority Play
- LLM Stack: Groq Llama 3.3 70B (primary execution agent) → Cerebras fallback
- Products: Amazon via RapidAPI → filtered by quality → stored in Google Sheets
- Delivery: ImgBB temp hosting → Make.com webhook → Pinterest
- Vision Feeder: Google Drive se images utha ke Gemini Vision se analyze karta hai aur Prompts_Master sheet update karta hai

YOUR ROLE IN THIS CHAT:
- Be the strategic advisor — help with content strategy, niche decisions, growth tactics
- Explain what the system is doing and WHY (the strategic logic behind decisions)
- Give data-driven opinions on Pinterest growth, viral content, affiliate marketing
- Suggest improvements, new niches, or content angles when asked
- Keep responses crisp — 3-5 sentences max unless a detailed breakdown is asked
- Never be boring. Be energetic but grounded in strategy."""

@app.post("/api/cmo-chat")
async def cmo_chat_endpoint(req: ChatMessage):
    msg = req.message.strip()
    if not msg:
        return {"response": "Ask me anything about strategy, growth, or the system! 🧠", "action": None}

    if not _gemini_client:
        return {
            "response": "Gemini API key nahi mila — GEMINI_API_KEY secret set karo aur restart karo. 🔑",
            "action": None
        }

    sys_ctx = (
        f"Live system snapshot — "
        f"Mastermind: {'RUNNING 🟢' if state['mastermind_running'] else 'IDLE ⚪'}. "
        f"Last run: {state['mastermind_last_run'] or 'Never'}. "
        f"Account 1 strategy: {state['mastermind_a1_strategy']}. "
        f"Account 2 strategy: {state['mastermind_a2_strategy']}. "
        f"Pins posted today: {state['posted_today']}. "
        f"Vision Feeder: {'ACTIVE 🟢' if _feeder_running() else 'INACTIVE ⚪'}."
    )

    full_prompt = f"{CMO_SYSTEM_PROMPT}\n\n[LIVE CONTEXT]: {sys_ctx}\n\n[USER]: {msg}\n\n[CEO MASTERMIND]:"

    try:
        from google.genai import types as _gtypes
        response = await asyncio.to_thread(
            lambda: _gemini_client.models.generate_content(
                model=GEMINI_CHAT_MODEL,
                contents=full_prompt,
                config=_gtypes.GenerateContentConfig(
                    temperature=0.8,
                    max_output_tokens=350,
                )
            )
        )
        reply = response.text.strip() if response.text else "Strategy mode mein hoon — thodi der baad pooch! 🧠"
    except Exception as e:
        logger.error(f"CMO chat error: {e}")
        reply = "Mastermind temporarily offline. Board meeting mein hoon — 2 minute mein wapas! 😄"

    return {"response": reply, "action": None}
    

@app.get("/api/ping")
def ping():
    return {"message": "pong"}


@app.get("/api/logs")
def get_logs(n: int = 200, filter: str = ""):
    """Return last N log lines, optionally filtered by keyword/regex."""
    import re
    lines = _mem_handler.get_lines(n)
    filter = (filter or "")[:100]
    if filter and filter != "ALL":
        try:
            pat = re.compile(filter, re.IGNORECASE)
            lines = [l for l in lines if pat.search(l)]
        except Exception:
            lines = [l for l in lines if filter.lower() in l.lower()]
    return {"lines": lines, "total": len(lines)}


# ── Local Boards — Save & Status ──────────────────────────────────────────────

@app.post("/api/boards/save")
async def save_boards_local(request: Request):
    """
    New format JSON ko local data/boards_config.json mein save karo.

    Format:
    {
      "account_1": {"boards": [{"board_id": "...", "name": "...", "description": "..."}, ...]},
      "account_2": {"boards": [...]}
    }
    Single account patch bhi supported:
    {"account_1": {"boards": [...]}}  — sirf account_1 update hoga
    """
    try:
        body = await request.json()
        json_str = body.get("json_data", "")
        if not json_str:
            return {"ok": False, "error": "json_data field khaali hai."}

        try:
            incoming = json.loads(json_str) if isinstance(json_str, str) else json_str
        except Exception as e:
            return {"ok": False, "error": f"JSON parse failed: {e}"}

        from tools.local_boards import load_boards_config, save_boards_config

        # Load existing config to do a partial patch
        existing = load_boards_config()

        saved_accounts = []
        total_boards = 0

        for acc_key in ["account_1", "account_2"]:
            if acc_key not in incoming:
                continue
            acc_data = incoming[acc_key]
            boards = acc_data.get("boards", []) if isinstance(acc_data, dict) else []
            if not isinstance(boards, list):
                continue
            existing[acc_key] = {"boards": boards}
            saved_accounts.append(acc_key)
            total_boards += len(boards)

        if not saved_accounts:
            return {"ok": False, "error": "account_1 ya account_2 keys nahi mili JSON mein."}

        ok = save_boards_config(existing)
        if not ok:
            return {"ok": False, "error": "File save failed — server logs check karo."}

        results = []
        for acc_key in saved_accounts:
            boards = existing[acc_key]["boards"]
            for b in boards:
                results.append(f"✅ {acc_key} — {b.get('name', b.get('board_id', '?'))}")

        return {
            "ok": True,
            "uploaded": total_boards,
            "errors": 0,
            "results": results,
            "error_details": [],
            "summary": f"{total_boards} boards saved across {len(saved_accounts)} account(s).",
        }

    except Exception as e:
        logger.error(f"Local boards save error: {e}")
        return {"ok": False, "error": str(e)}


@app.get("/api/boards/status")
async def boards_status_local():
    """Local boards_config.json ka quick status."""
    try:
        from tools.local_boards import get_boards_status
        data = get_boards_status()
        a1 = data.get("account_1", {})
        a2 = data.get("account_2", {})
        return {
            "ok": True,
            "account_1": {
                "boards": {b["board_id"]: {"board_name": b["name"], "board_id": b["board_id"]} for b in a1.get("boards", [])},
            },
            "account_2": {
                "boards": {b["board_id"]: {"board_name": b["name"], "board_id": b["board_id"]} for b in a2.get("boards", [])},
            },
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "account_1": {"boards": {}}, "account_2": {"boards": {}}}


# ── Firebase Boards Upload ─────────────────────────────────────────────────────

@app.post("/api/firebase/upload-boards")
async def upload_boards_to_firebase(request: Request):
    """
    Claude ka structured JSON le ke Firestore mein upload karo.
    
    Supported formats:
    A) Full structure: {"account_1": {"home": {...}, "cozy": {...}}, "account_2": {...}}
    B) Single account: {"home": {...}, "cozy": {...}}  (account param required)
    
    Each board doc: boards/{account}/items/{niche_key}
    """
    try:
        body = await request.json()
        json_str = body.get("json_data", "")
        override_account = body.get("account", "")   # optional: force single account

        if not json_str:
            return {"ok": False, "error": "json_data field khaali hai."}

        try:
            data = json.loads(json_str) if isinstance(json_str, str) else json_str
        except Exception as e:
            return {"ok": False, "error": f"JSON parse failed: {e}"}

        from tools.firebase_boards import _get_db
        db = _get_db()

        results = []
        errors  = []

        def _write_boards(account: str, boards_dict: dict):
            for niche_key, board_data in boards_dict.items():
                try:
                    if not isinstance(board_data, dict):
                        errors.append(f"{account}/{niche_key}: value dict nahi hai")
                        continue
                    ref = db.collection("boards").document(account).collection("items").document(niche_key)
                    board_data["active"] = board_data.get("active", True)
                    ref.set(board_data, merge=True)
                    results.append(f"✅ {account}/{niche_key} — {board_data.get('board_name', niche_key)}")
                except Exception as e:
                    errors.append(f"❌ {account}/{niche_key}: {e}")

        # Format A: top-level keys are account names
        if any(k.startswith("account_") for k in data.keys()):
            for acc_key, boards_dict in data.items():
                if acc_key.startswith("account_") and isinstance(boards_dict, dict):
                    _write_boards(acc_key, boards_dict)
        # Format B: single account (override_account required)
        elif override_account:
            _write_boards(override_account, data)
        else:
            return {
                "ok": False,
                "error": "Format detect nahi hua. Ya 'account_1'/'account_2' top-level keys rakho, ya 'account' field bhejo."
            }

        return {
            "ok": len(results) > 0,
            "uploaded": len(results),
            "errors":   len(errors),
            "results":  results,
            "error_details": errors,
            "summary": f"{len(results)} boards uploaded, {len(errors)} failed."
        }

    except Exception as e:
        logger.error(f"Firebase upload error: {e}")
        return {"ok": False, "error": str(e)}


@app.get("/api/firebase/boards-status")
async def firebase_boards_status():
    """Current Firestore boards ka quick status."""
    try:
        from tools.firebase_boards import get_boards, get_all_active_trends
        a1_boards = get_boards("account_1")
        a2_boards = get_boards("account_2")
        a1_trends = get_all_active_trends("account_1")
        a2_trends = get_all_active_trends("account_2")
        return {
            "ok": True,
            "account_1": {
                "boards": {k: {"board_name": v.get("board_name", k), "board_id": v.get("board_id", ""), "prompts": len(v.get("prompts", []))} for k, v in a1_boards.items()},
                "trend_sets": len(a1_trends),
            },
            "account_2": {
                "boards": {k: {"board_name": v.get("board_name", k), "board_id": v.get("board_id", ""), "prompts": len(v.get("prompts", []))} for k, v in a2_boards.items()},
                "trend_sets": len(a2_trends),
            },
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "account_1": {"boards": {}}, "account_2": {"boards": {}}}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=7860)
