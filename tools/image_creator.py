"""
tools/image_creator.py — Triple-Layer T2I Image Pipeline  [VARIETY ENGINE v4 — CLEAN & OPTIMIZED]

MODELS (in order):
  1. Cloudflare     — @cf/black-forest-labs/flux-1-schnell (Primary)
  2. Hugging Face   — black-forest-labs/FLUX.1-schnell (Secondary fallback)
  3. Pollinations   — free, URL-based (Last resort fallback)

VARIETY ENGINE v4 — What changed from v3:
  REMOVED (were hurting quality):
    ✗ _CAMERA_LENSES    — Flux Schnell ignores real camera specs, produces garbage
    ✗ _EDITORIAL_STYLE  — "Architectural Digest editorial" has zero effect on model
    ✗ _COLOR_ACCENTS    — Conflicts with prompts master which already sets colors
    ✗ _TEXTURE_DETAILS  — Over-specification overwhelms the model
    ✗ _TIME_OF_DAY      — Redundant, already covered by _LIGHTING_MOODS

  KEPT (genuinely useful):
    ✓ _CAMERA_ANGLES    — Fresh perspective every pin
    ✓ _LIGHTING_MOODS   — Controls brightness & mood well
    ✓ _COMP_STYLES      — Gives composition variety
    ✓ _SEASONS_WEATHER  — Background context variety

  RESULT: Prompt stays ~350-450 chars (was bloating to 900).
          Model gets clear signal → sharper, cleaner, on-theme output.

RATIO SUPPORT:
  • 9:16 portrait → 1080x1920  (primary, Pinterest-native)
  • 1:1 square    → 1080x1080  (alternate, carousel-friendly)
"""

import asyncio
import base64
import logging
import os
import random
import time
import urllib.parse
from typing import Optional

import httpx

try:
    from tools.run_control import is_stopped as _run_stopped
except Exception:  # flat import (module seedha run ho tab)
    try:
        from run_control import is_stopped as _run_stopped
    except Exception:
        def _run_stopped() -> bool:
            return False

from config import (
    IMGBB_API_KEY,
    CLOUDFLARE_ACCOUNT_ID,
    CLOUDFLARE_API_TOKEN,
    CLOUDFLARE_IMAGE_MODEL,
    POLLINATIONS_MODEL
)

logger = logging.getLogger(__name__)

# ── HuggingFace token from env ─────────────────────────────────────────────────
HF_API_TOKEN = os.environ.get("HF_TOKEN", "")

# ── Constants ──────────────────────────────────────────────────────────────────
_CALL_TIMEOUT        = 180
_CF_RETRY_DELAY      = 40   # Cloudflare: 40s wait between retries (GPU overload needs time)
_HF_RETRY_DELAY      = 30   # HuggingFace: 30s wait between retries
_POLL_RETRY_DELAY    = 10   # Pollinations: 10s wait between retries
_MIN_VALID_BYTES     = 5_000
_CF_MAX_RETRIES      = 2
_HF_MAX_RETRIES      = 2
_POLL_MAX_RETRIES    = 2

_RATIO_DIMS = {
    "9:16": (1080, 1920),
    "1:1":  (1080, 1080),
}

_POLLINATIONS_BASE = "https://image.pollinations.ai/prompt"
_HF_FLUX_URL       = "https://api-inference.huggingface.co/models/black-forest-labs/FLUX.1-schnell"

# ── Negative prompt — always appended to block garbage outputs ─────────────────
_NEGATIVE_PROMPT = (
    "ugly, blurry, watermark, text overlay, logo, signature, "
    "cartoon, anime, illustration, drawing, painting, CGI plastic, "
    "overexposed, underexposed, low quality, jpeg artifacts, "
    "distorted, deformed, duplicate, cropped, frame, border, "
    "dark, moody, noir, desaturated, gloomy, depressing"
)

# ══════════════════════════════════════════════════════════════════════════════
# VARIETY ENGINE v4 — 4 focused pools only
# Prompts master already handles: subject, colors, props, style details
# These pools only add: angle, light, composition, season — nothing more
# ══════════════════════════════════════════════════════════════════════════════

_CAMERA_ANGLES = [
    "eye-level lifestyle shot",
    "slight high angle looking into room",
    "low angle emphasizing height of space",
    "straight-on symmetrical architectural shot",
    "three-quarter angle showing room depth",
    "diagonal foreground prop framing shot",
    "wide establishing interior shot",
    "intimate close-up detail and texture shot",
    "over-the-shoulder lifestyle perspective",
    "corner angle showing two walls for depth",
]

_LIGHTING_MOODS = [
    "bright soft morning window light",
    "warm golden hour afternoon sunlight streaming in",
    "diffused bright daylight, zero harsh shadows",
    "gentle dappled sunlight through sheer curtains",
    "crisp clean mid-morning bright light",
    "warm ambient with soft window fill",
    "bright airy spring daylight, high key",
    "soft side window light with gentle fill",
    "cheerful sunny afternoon interior light",
    "warm cozy lamp light complementing daylight",
]

_COMP_STYLES = [
    "rule of thirds with strong foreground prop",
    "perfect centered symmetry, interior architecture",
    "leading lines drawing eye through room depth",
    "layered depth: close props, mid furniture, background wall",
    "foreground bokeh flower or plant framing subject",
    "wide establishing room context shot",
    "tight detail crop isolating single beautiful prop",
    "s-curve natural flow composition",
    "framed within doorway or window arch",
    "negative space minimalism with single hero prop",
]

_SEASONS_WEATHER = [
    "lush bright spring, cherry blossoms visible through window",
    "warm golden summer, vibrant greenery outside",
    "fresh spring morning, soft green buds on trees",
    "bright clear summer day, blue sky outside",
    "warm late spring afternoon, flowers in full bloom",
    "sunny golden autumn, warm amber light",
    "fresh spring after rain, everything glistening green",
    "clear crisp summer blue-sky day",
    "warm mid-spring garden visible through window",
    "bright cheerful overcast spring diffused light",
]


def _pick_variety_modifiers() -> dict:
    """Pick one random modifier from each of the 4 focused pools."""
    return {
        "angle":    random.choice(_CAMERA_ANGLES),
        "lighting": random.choice(_LIGHTING_MOODS),
        "comp":     random.choice(_COMP_STYLES),
        "season":   random.choice(_SEASONS_WEATHER),
    }


def _inject_variety(base_prompt: str) -> tuple[str, dict]:
    """
    Inject 4 focused variety modifiers into the base prompts-master visual_prompt.
    Keeps total prompt lean (~350-450 chars) so the model stays on-theme.
    Returns (enriched_prompt, modifiers_used) — modifiers logged for debugging.
    """
    mods = _pick_variety_modifiers()

    variety_block = (
        f"{mods['angle']}, {mods['lighting']}, "
        f"{mods['comp']}, {mods['season']}"
    )

    quality_tail = "4K ultra HD, photorealistic, highly detailed, award-winning photography"

    # Strip existing quality tail if prompts master already added it, re-add cleanly
    base_clean = base_prompt.replace(quality_tail, "").rstrip(", ").strip()

    enriched = f"{base_clean}, {variety_block}, {quality_tail}"
    return enriched, mods


# ── Helpers ────────────────────────────────────────────────────────────────────

def _enrich_prompt(prompt: str, max_chars: int = 600) -> str:
    """Ensure quality tail present, then truncate. Variety already injected upstream."""
    base = prompt.strip()
    if "4K" not in base and "4k" not in base:
        base += ", 4K ultra HD, photorealistic, highly detailed, award-winning photography"
    return base[:max_chars]


def _get_dims(ratio: str) -> tuple[int, int]:
    return _RATIO_DIMS.get(ratio, _RATIO_DIMS["9:16"])


async def _download_bytes(url: str, timeout: int = _CALL_TIMEOUT) -> Optional[bytes]:
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content
    except Exception as e:
        logger.error(f"❌ Download failed [{url[:60]}]: {e}")
        return None


class ImgBBHostingError(Exception):
    """Hosting failure — image GENERATION safal tha, sirf ImgBB upload
    fail hua. Generation failure se alag pehchan ke liye alag type:
    logs/callers dono ko saaf dikhe ki gadbad hosting ki hai, image ki nahi."""


_IMGBB_MAX_ATTEMPTS = 3       # sirf UPLOAD retry hoga — generation kabhi dobara nahi
_IMGBB_EXPIRATION   = 1800    # seconds (30 min) — temporary hosting; pin post
                              # hote hi Pinterest image apne CDN par le leta hai

# ── Circuit breaker — ImgBB DOWN ho toh loop band ────────────────────────────
# Seekh (30 Oct 2026, owner ka gussa sahi tha): ImgBB bilkul jawab na de
# (ReadTimeout har attempt) toh har pin 3 lambe attempts me atakta tha aur
# scheduler agla pin phir wahi karta — pura system ek fail-loop ban gaya.
# Ab: lagatar 2 pin-level hosting failures ke baad circuit 15 min ke liye
# KHUL jata hai — us dauran generation bhi nahi hogi aur upload try bhi
# nahi; system chup rahega. Cooldown ke baad pehla pin khud test banega,
# safal hua toh streak reset aur sab normal.
_HOSTING_FAIL_STREAK   = 0
_CIRCUIT_OPEN_UNTIL    = 0.0   # time.monotonic() deadline
_CIRCUIT_THRESHOLD     = 2
_CIRCUIT_COOLDOWN_SEC  = 900   # 15 min


def _circuit_open() -> bool:
    return time.monotonic() < _CIRCUIT_OPEN_UNTIL


def _note_hosting_success() -> None:
    global _HOSTING_FAIL_STREAK
    _HOSTING_FAIL_STREAK = 0


def _note_hosting_failure() -> None:
    global _HOSTING_FAIL_STREAK, _CIRCUIT_OPEN_UNTIL
    _HOSTING_FAIL_STREAK += 1
    if _HOSTING_FAIL_STREAK >= _CIRCUIT_THRESHOLD and not _circuit_open():
        _CIRCUIT_OPEN_UNTIL = time.monotonic() + _CIRCUIT_COOLDOWN_SEC
        logger.error(
            f"⚡ [Hosting] CIRCUIT OPEN — saare hosts lagatar {_HOSTING_FAIL_STREAK}x fail; "
            f"{_CIRCUIT_COOLDOWN_SEC // 60} min tak generation + upload dono band. "
            "Faltu loop rukega, cooldown ke baad system khud dobara try karega."
        )


async def _upload_to_imgbb(image_bytes: bytes) -> str:
    """
    ImgBB upload with hosting-only retries.

    Bug history (30 Oct 2026): pehle ye function EK attempt karta tha aur
    `except: logger.error(f"...{e}")` me sirf exception ka str() chhapta tha —
    httpx timeouts ka str() aksar KHALI hota hai, aur HTTP error par ImgBB ka
    asli error message response BODY me hota hai jo raise_for_status() ki
    wajah se kabhi padha hi nahi jata tha. Nateeja: "Upload failed: " (khaali)
    aur acchi-bani image ke saath pin fail — agle run me poori generation
    dobara. Ab:
      • WAHI bytes par max 3 upload attempts, exponential backoff (2s, 4s)
      • Har failure par: HTTP status + poora response body + exception type
        + traceback — kuch chhupa nahi
      • Key sirf "set hai / length" ke roop me log hoti hai, value kabhi nahi
      • Response validate hota hai (success flag + https direct URL)
      • Teeno attempts fail → ImgBBHostingError (generation failure NAHI)
    """
    if not IMGBB_API_KEY:
        logger.error("❌ [ImgBB] IMGBB_API_KEY config me set NAHI hai.")
        raise ImgBBHostingError("IMGBB_API_KEY not configured")
    if not image_bytes or len(image_bytes) < _MIN_VALID_BYTES:
        raise ImgBBHostingError(
            f"Invalid image payload: {0 if not image_bytes else len(image_bytes)} bytes"
        )

    encoded = base64.b64encode(image_bytes).decode("utf-8")
    logger.info(
        f"⬆️  [ImgBB] Uploading {len(image_bytes):,} bytes "
        f"(expiration={_IMGBB_EXPIRATION}s, key: set, length={len(IMGBB_API_KEY)})..."
    )

    last_error: Optional[BaseException] = None
    for attempt in range(1, _IMGBB_MAX_ATTEMPTS + 1):
        if _run_stopped():
            raise ImgBBHostingError("Upload aborted — owner ne STOP lagaya hai")
        try:
            # Timeout chhota rakho: ImgBB jawab hi na de toh 60s latakne ki
            # jagah 20s me attempt khatam — 3 attempts ka worst case ~66s.
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10.0, read=20.0, write=20.0, pool=10.0)
            ) as client:
                resp = await client.post(
                    "https://api.imgbb.com/1/upload",
                    data={
                        "key": IMGBB_API_KEY,
                        "image": encoded,
                        "expiration": str(_IMGBB_EXPIRATION),
                    },
                )
            body = (resp.text or "")[:500]
            if resp.status_code != 200:
                logger.error(
                    f"❌ [ImgBB] Attempt {attempt}/{_IMGBB_MAX_ATTEMPTS} — "
                    f"HTTP {resp.status_code} | body: {body}"
                )
                raise ImgBBHostingError(f"HTTP {resp.status_code}: {body[:200]}")
            payload = resp.json()
            data = payload.get("data") or {}
            url = data.get("url") or ""
            if payload.get("success") is not True or not url.startswith("https://"):
                logger.error(
                    f"❌ [ImgBB] Attempt {attempt}/{_IMGBB_MAX_ATTEMPTS} — "
                    f"response invalid (success={payload.get('success')}, url={url!r}) | body: {body}"
                )
                raise ImgBBHostingError(f"Invalid ImgBB response: {body[:200]}")
            logger.info(f"✅ [ImgBB] Hosted (attempt {attempt}): {url}")
            return url
        except ImgBBHostingError as e:
            last_error = e
        except Exception as e:
            # Asli exception kabhi mat chhupao — type + poora traceback
            logger.exception(
                f"❌ [ImgBB] Attempt {attempt}/{_IMGBB_MAX_ATTEMPTS} — "
                f"{type(e).__name__}: {e}"
            )
            last_error = ImgBBHostingError(f"{type(e).__name__}: {e}")
        if attempt < _IMGBB_MAX_ATTEMPTS:
            wait = 2 ** attempt  # 2s, phir 4s
            logger.info(f"🔁 [ImgBB] {wait}s baad upload dobara try hoga (wahi image, generation NAHI)...")
            await asyncio.sleep(wait)

    raise ImgBBHostingError(
        f"All {_IMGBB_MAX_ATTEMPTS} upload attempts failed. Last error: {last_error}"
    )


# ── TEMPORARY HOSTING CASCADE (31 Oct 2026 — ImgBB down, owner ka order) ────
# ImgBB ke servers down chal rahe the (har upload ReadTimeout). Owner ki
# requirement: hosting TEMPORARY ho (pin post hote hi Pinterest image apne
# CDN par le leta hai, uske baad hosted copy ka koi kaam nahi), bilkul
# FREE, bina signup/key, ~30 images/day.
# Order: Litterbox (24h auto-delete, expiry khud choose hota hai) → Uguu
# (temporary, ~24h me delete) → ImgBB (ab sirf FALLBACK, apne retry +
# breaker ke saath). Har temp host par sirf EK tez attempt (25s timeout)
# — ek host latke toh dusra fauran try hota hai, purana 3-minute wala
# latakna ab namumkin hai.
_HOST_UA = {"User-Agent": "Pinteresto/4.0 (Pinterest automation; contact: owner)"}
_HOST_TIMEOUT = httpx.Timeout(connect=10.0, read=25.0, write=25.0, pool=10.0)


def _img_file(image_bytes: bytes) -> tuple:
    """Magic bytes se asli format pehchano — sahi filename + content-type
    host ko do (PNG ko .jpg kehna kuch hosts par reject hota hai)."""
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return ("pin.png", "image/png")
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return ("pin.webp", "image/webp")
    return ("pin.jpg", "image/jpeg")


async def _upload_litterbox(image_bytes: bytes) -> str:
    """Litterbox (Catbox ki temporary service): anonymous upload,
    time=24h par file khud delete. Response = seedha plain-text URL."""
    name, mime = _img_file(image_bytes)
    async with httpx.AsyncClient(timeout=_HOST_TIMEOUT) as client:
        resp = await client.post(
            "https://litterbox.catbox.moe/resources/internals/api.php",
            data={"reqtype": "fileupload", "time": "24h"},
            files={"fileToUpload": (name, image_bytes, mime)},
            headers=_HOST_UA,
        )
    body = (resp.text or "").strip()
    if resp.status_code != 200 or not body.startswith("https://"):
        raise ImgBBHostingError(f"Litterbox HTTP {resp.status_code}: {body[:200]}")
    return body.splitlines()[0].strip()


async def _upload_uguu(image_bytes: bytes) -> str:
    """Uguu.se: temporary file host (files khud expire hoti hain),
    koi key/account nahi. JSON response: files[0].url = direct URL."""
    name, mime = _img_file(image_bytes)
    async with httpx.AsyncClient(timeout=_HOST_TIMEOUT) as client:
        resp = await client.post(
            "https://uguu.se/upload.php",
            files={"files[]": (name, image_bytes, mime)},
            headers=_HOST_UA,
        )
    body = (resp.text or "")[:300]
    if resp.status_code != 200:
        raise ImgBBHostingError(f"Uguu HTTP {resp.status_code}: {body}")
    payload = resp.json()
    url = ((payload.get("files") or [{}])[0].get("url") or "")
    if not payload.get("success") or not url.startswith("https://"):
        raise ImgBBHostingError(f"Uguu invalid response: {body}")
    return url


async def _upload_hosted(image_bytes: bytes) -> str:
    """Hosting ka ek darwaza — andar cascade hai:
    Litterbox → Uguu → ImgBB (fallback). Sab fail toh ImgBBHostingError
    (circuit breaker isi ko ginata hai)."""
    if _run_stopped():
        raise ImgBBHostingError("Upload aborted — owner ne STOP lagaya hai")
    for host_name, fn in (("Litterbox", _upload_litterbox), ("Uguu", _upload_uguu)):
        try:
            url = await fn(image_bytes)
            logger.info(f"✅ [Hosting:{host_name}] Hosted (temporary): {url}")
            return url
        except Exception as e:
            logger.warning(
                f"⚠️ [Hosting:{host_name}] fail — {type(e).__name__}: {str(e)[:150]} — agla host try..."
            )
    logger.info("🔁 [Hosting] Dono temp hosts fail — ImgBB fallback (retry wala)...")
    return await _upload_to_imgbb(image_bytes)


def _is_valid(image_bytes: Optional[bytes]) -> bool:
    return bool(image_bytes) and len(image_bytes) >= _MIN_VALID_BYTES


# ── Model 1: Cloudflare Workers AI (multi-model cascade) ──────────────────────

# Cascade order: config model first (FLUX.2 klein), then FLUX.1 schnell.
# FLUX.2 models accept multipart/form-data ONLY (a JSON body gets HTTP 400);
# FLUX.1 schnell takes the classic JSON {"prompt": ...} payload.
_CF_MODELS = list(dict.fromkeys([
    CLOUDFLARE_IMAGE_MODEL,
    "@cf/black-forest-labs/flux-1-schnell",
]))


async def _cloudflare_once(prompt: str, ratio: str, model: str) -> Optional[bytes]:
    if not CLOUDFLARE_ACCOUNT_ID or not CLOUDFLARE_API_TOKEN:
        raise RuntimeError("CLOUDFLARE_ACCOUNT_ID or CLOUDFLARE_API_TOKEN not configured.")

    w, h     = _get_dims(ratio)
    # Removing resolution text from prompt to be safe as well
    enriched = _enrich_prompt(prompt)

    url = (
        f"https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}"
        f"/ai/run/{model}"
    )

    headers = {
        "Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}",
        "User-Agent":    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    async with httpx.AsyncClient(timeout=_CALL_TIMEOUT) as client:
        if "flux-2" in model:
            # FLUX.2 (klein): multipart form — prompt + width + height
            form = {
                "prompt": (None, enriched),
                "width":  (None, str(w)),
                "height": (None, str(h)),
            }
            resp = await client.post(url, headers=headers, files=form)
        else:
            # FLUX.1 schnell: classic JSON payload (prompt only)
            resp = await client.post(
                url,
                headers={**headers, "Content-Type": "application/json"},
                json={"prompt": enriched},
            )
        resp.raise_for_status()

        if "application/json" in resp.headers.get("content-type", "").lower():
            data    = resp.json()
            b64_str = data.get("result", {}).get("image", "")
            if b64_str:
                return base64.b64decode(b64_str)
            logger.error(f"❌ [Cloudflare] No image in response: {str(data)[:200]}")
            return None
        else:
            return resp.content


async def _t2i_cloudflare(prompt: str, ratio: str) -> Optional[bytes]:
    for model in _CF_MODELS:
        short = model.split("/")[-1]
        for attempt in range(1, _CF_MAX_RETRIES + 1):
            try:
                logger.info(f"🎨 [Cloudflare:{short}] Attempt {attempt}/{_CF_MAX_RETRIES} | ratio={ratio}")
                img = await _cloudflare_once(prompt, ratio, model)
                if _is_valid(img):
                    logger.info(f"✅ [Cloudflare:{short}] {len(img):,} bytes on attempt {attempt}")
                    return img
                logger.warning(f"⚠️ [Cloudflare:{short}] Attempt {attempt}: invalid/too small")
            except Exception as e:
                logger.warning(
                    f"⚠️ [Cloudflare:{short}] Attempt {attempt} error: "
                    f"{type(e).__name__}: {e!r} | cause={e.__cause__!r}"
                )

            if attempt < _CF_MAX_RETRIES:
                delay = random.randint(30, 50)
                logger.info(f"⏳ [Cloudflare:{short}] Waiting {delay}s before retry...")
                await asyncio.sleep(delay)

        logger.error(f"❌ [Cloudflare:{short}] All attempts failed — trying next model.")

    logger.error("❌ [Cloudflare] All models failed — moving to HuggingFace.")
    return None


# ── Model 2: Hugging Face FLUX.1-schnell ───────────────────────────────────────

async def _huggingface_once(prompt: str, ratio: str) -> Optional[bytes]:
    if not HF_API_TOKEN:
        raise RuntimeError("HF_TOKEN not set in environment.")

    w, h     = _get_dims(ratio)
    enriched = _enrich_prompt(prompt, max_chars=500)

    headers = {
        "Authorization": f"Bearer {HF_API_TOKEN}",
        "Content-Type":  "application/json",
        "User-Agent":    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    payload = {
        "inputs": enriched,
        "parameters": {
            "width":               w,
            "height":              h,
            "num_inference_steps": 4,    # FLUX schnell optimal: 1-4 steps
            "guidance_scale":      0.0,  # FLUX schnell needs 0.0
            "seed":                random.randint(1, 999_999),
        }
    }

    async with httpx.AsyncClient(timeout=_CALL_TIMEOUT) as client:
        resp = await client.post(_HF_FLUX_URL, headers=headers, json=payload)

        # Model may be loading — HF returns 503 with estimated_time
        if resp.status_code == 503:
            try:
                wait_time = resp.json().get("estimated_time", 30)
            except Exception:
                wait_time = 30
            logger.info(f"⏳ [HuggingFace] Model loading, waiting {wait_time:.0f}s...")
            await asyncio.sleep(min(float(wait_time), 60))
            # One more try after model loads
            resp = await client.post(_HF_FLUX_URL, headers=headers, json=payload)

        resp.raise_for_status()
        return resp.content


async def _t2i_huggingface(prompt: str, ratio: str) -> Optional[bytes]:
    if not HF_API_TOKEN:
        logger.warning("⚠️ [HuggingFace] HF_TOKEN not set — skipping.")
        return None

    for attempt in range(1, _HF_MAX_RETRIES + 1):
        try:
            logger.info(f"🤗 [HuggingFace] Attempt {attempt}/{_HF_MAX_RETRIES} | ratio={ratio}")
            img = await _huggingface_once(prompt, ratio)
            if _is_valid(img):
                logger.info(f"✅ [HuggingFace] {len(img):,} bytes on attempt {attempt}")
                return img
            logger.warning(f"⚠️ [HuggingFace] Attempt {attempt}: invalid/too small")
        except httpx.HTTPStatusError as e:
            if e.response is not None and e.response.status_code == 410:
                logger.error(
                    "❌ [HuggingFace] FLUX.1-schnell is permanently removed from "
                    "hf-inference (410 Gone since Jul 2026) — skipping retries, "
                    "moving straight to Pollinations."
                )
                return None
            logger.warning(f"⚠️ [HuggingFace] Attempt {attempt} error: {type(e).__name__}: {e!r}")
        except Exception as e:
            logger.warning(f"⚠️ [HuggingFace] Attempt {attempt} error: {type(e).__name__}: {e!r}")

        if attempt < _HF_MAX_RETRIES:
            logger.info(f"⏳ [HuggingFace] Waiting {_HF_RETRY_DELAY}s before retry...")
            await asyncio.sleep(_HF_RETRY_DELAY)

    logger.error("❌ [HuggingFace] All attempts failed — moving to Pollinations.")
    return None


# ── Model 3: Pollinations.ai (Last Resort) ─────────────────────────────────────

async def _pollinations_once(prompt: str, ratio: str) -> Optional[bytes]:
    w, h = _get_dims(ratio)

    # Pollinations quality tip: keep prompt under 300 chars — long prompts hurt quality here
    # Strip quality tail (Pollinations ignores "4K ultra HD" etc), keep core visual only
    core_prompt = prompt.replace(
        ", 4K ultra HD, photorealistic, highly detailed, award-winning photography", ""
    ).strip().rstrip(",").strip()
    core_prompt = core_prompt[:300]  # Hard cap — Pollinations degrades beyond this

    seed    = random.randint(1, 999_999)
    encoded = urllib.parse.quote(core_prompt)

    # flux-pro = noticeably better quality than flux on Pollinations (still free)
    url = (
        f"{_POLLINATIONS_BASE}/{encoded}"
        f"?width={w}&height={h}&nologo=true&enhance=true"
        f"&model=flux-pro&quality=high&seed={seed}&safe=false"
    )
    logger.info(f"🎨 [Pollinations] {w}x{h} | seed={seed} | ratio={ratio} | model=flux-pro")
    img = await _download_bytes(url, timeout=_CALL_TIMEOUT)
    if img is None:
        raise RuntimeError("Pollinations download returned None.")
    return img


async def _t2i_pollinations(prompt: str, ratio: str) -> Optional[bytes]:
    for attempt in range(1, _POLL_MAX_RETRIES + 1):
        try:
            logger.info(f"🎨 [Pollinations] Attempt {attempt}/{_POLL_MAX_RETRIES}")
            img = await _pollinations_once(prompt, ratio)
            if _is_valid(img):
                logger.info(f"✅ [Pollinations] {len(img):,} bytes on attempt {attempt}")
                return img
            logger.warning(f"⚠️ [Pollinations] Attempt {attempt}: too small")
        except Exception as e:
            logger.warning(f"⚠️ [Pollinations] Attempt {attempt} error: {e}")

        if attempt < _POLL_MAX_RETRIES:
            logger.info(f"⏳ [Pollinations] Waiting {_POLL_RETRY_DELAY}s before retry...")
            await asyncio.sleep(_POLL_RETRY_DELAY)

    logger.error("❌ [Pollinations] All attempts failed.")
    return None


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ══════════════════════════════════════════════════════════════════════════════

async def generate_pin_image(visual_prompt: str, ratio: str = "9:16") -> Optional[str]:
    """
    Generate a Pinterest image using the T2I pipeline.

    Flow:
      1. Inject 4 focused variety modifiers (angle, lighting, comp, season)
      2. Random seed every call for visual freshness
      3. Try Cloudflare (primary) → HuggingFace (secondary) → Pollinations (last resort)
      4. Upload result to temporary hosting cascade — Litterbox (24h) →
         Uguu → ImgBB fallback (har host sirf upload retry karta hai,
         generation dobara nahi hoti)

    Args:
        visual_prompt: The detailed visual prompt from prompts master
        ratio: "9:16" (Pinterest portrait) or "1:1" (square)

    Returns:
        ImgBB hosted URL or None if all models fail
    """
    if _run_stopped():
        logger.info("⏸️ [Image Pipeline] STOP active hai — pin skip, koi generation nahi.")
        return None
    if _circuit_open():
        logger.error("⚡ [ImgBB] Circuit OPEN hai — generation bhi skip (ImgBB down, cooldown chal raha hai).")
        return None

    w, h = _get_dims(ratio)
    logger.info(f"🎨 [Image Pipeline] PIN | ratio={ratio} ({w}x{h})")

    # Inject variety — 4 focused modifiers, lean prompt
    enriched_prompt, mods = _inject_variety(visual_prompt)
    logger.info(
        f"🎲 [Variety Engine v4] "
        f"angle={mods['angle'][:35]} | "
        f"light={mods['lighting'][:35]} | "
        f"comp={mods['comp'][:35]} | "
        f"season={mods['season'][:35]}"
    )
    logger.info(f"📝 [Prompt] {len(enriched_prompt)} chars: {enriched_prompt[:120]}...")

    # PRIMARY: Cloudflare
    image_bytes = await _t2i_cloudflare(enriched_prompt, ratio)

    # SECONDARY: HuggingFace
    if not image_bytes:
        logger.info("🔄 [Image Pipeline] Cloudflare exhausted — trying HuggingFace...")
        image_bytes = await _t2i_huggingface(enriched_prompt, ratio)

    # LAST RESORT: Pollinations
    if not image_bytes:
        logger.info("🔄 [Image Pipeline] HuggingFace exhausted — trying Pollinations...")
        image_bytes = await _t2i_pollinations(enriched_prompt, ratio)

    if not image_bytes:
        logger.error("❌ [Image Pipeline] All models exhausted — no image generated.")
        return None

    # Hosting generation se ALAG step hai: upload fail ho toh generation
    # dobara NAHI hoti (upar ke bytes fix hain) — _upload_to_imgbb khud
    # 3 baar wahi bytes retry karta hai. Yahan sirf pehchan ke liye alag log.
    if _circuit_open():
        logger.error(
            "⚡ [ImgBB] Circuit OPEN hai — ImgBB down hai, is pin ki image "
            "upload try bhi nahi hogi (cooldown chal raha hai)."
        )
        return None
    try:
        url = await _upload_hosted(image_bytes)
        _note_hosting_success()
        return url
    except ImgBBHostingError as e:
        _note_hosting_failure()
        logger.error(
            f"🛑 [Hosting] HOSTING FAILURE — image ban gayi thi, saare hosts fail: {e}"
        )
        return None


async def upload_raw_image(image_url: str) -> Optional[str]:
    """Download an affiliate product image and re-host on ImgBB."""
    if _run_stopped():
        logger.info("⏸️ [Image Pipeline] STOP active hai — product image skip.")
        return None
    if _circuit_open():
        logger.error("⚡ [ImgBB] Circuit OPEN hai — product image skip.")
        return None
    logger.info(f"⬇️  [Image Pipeline] AFFILIATE_PIN — downloading: {image_url[:60]}...")
    image_bytes = await _download_bytes(image_url)
    if not image_bytes:
        logger.error("❌ [Image Pipeline] Product image download failed.")
        return None
    if _circuit_open():
        logger.error("⚡ [ImgBB] Circuit OPEN hai — product image upload skip.")
        return None
    try:
        url = await _upload_hosted(image_bytes)
        _note_hosting_success()
        return url
    except ImgBBHostingError as e:
        _note_hosting_failure()
        logger.error(f"🛑 [Hosting] HOSTING FAILURE (product image) — saare hosts fail: {e}")
        return None
