"""
tools/image_creator.py — T2I Image Pipeline  [VARIETY ENGINE v4 — CLEAN & OPTIMIZED]

MODELS (in order):
  1. Cloudflare     — @cf/black-forest-labs/flux-2-klein-4b (Primary, free)
  2. Replicate      — black-forest-labs/flux-schnell (V4 addition; only when
                      REPLICATE_API_TOKEN is set — $0.003/img, identical
                      schnell output for Cloudflare 5xx/outage days)
  3. Hugging Face   — black-forest-labs/FLUX.1-schnell (legacy; HF removed
                      schnell from hf-inference in Jul 2026 — 410 fast-bail)
  4. Pollinations   — free, URL-based (Last resort fallback)

Cloudflare 520-524 (incl. 523 "origin unreachable") are transient infra
errors per Cloudflare's own docs — the retry loop below already absorbs the
common case; a persistent 523 is exactly what the Replicate layer is for.

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
import json
import logging
import os
import random
import re
import urllib.parse
from typing import Optional

import httpx

from config import (
    IMGBB_API_KEY,
    CLOUDFLARE_ACCOUNT_ID,
    CLOUDFLARE_API_TOKEN,
    CLOUDFLARE_IMAGE_MODEL,
    POLLINATIONS_MODEL
)

try:
    from config import IMAGE_MOOD_ENGINE as _IMAGE_MOOD_ENGINE_CFG
except Exception:  # older config without the switches section
    _IMAGE_MOOD_ENGINE_CFG = None

try:
    from config import IMAGE_VISION_GATE as _IMAGE_VISION_GATE_CFG
except Exception:  # older config without the switches section
    _IMAGE_VISION_GATE_CFG = None

try:
    from config import GEMINI_API_KEY as _GEMINI_API_KEY_CFG
    from config import GEMINI_CMO_MODEL as _GEMINI_MODEL_CFG
except Exception:
    _GEMINI_API_KEY_CFG = None
    _GEMINI_MODEL_CFG = None

logger = logging.getLogger(__name__)

# ── HuggingFace token from env ─────────────────────────────────────────────────
HF_API_TOKEN = os.environ.get("HF_TOKEN", "")

# ── Replicate token from env (V4 fallback layer — off until set) ───────────────
REPLICATE_API_TOKEN = os.environ.get("REPLICATE_API_TOKEN", "")
_REPLICATE_FLUX_URL = "https://api.replicate.com/v1/models/black-forest-labs/flux-schnell/predictions"
_REP_MAX_RETRIES    = 2
_REP_RETRY_DELAY    = 10

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
# V5 note: "dark, moody, gloomy, desaturated" were REMOVED — they banned the
# warm evening/candlelit moods the Mood Engine now deliberately creates.
# Quality negatives stay; mood is steered by the positive prompt instead.
_NEGATIVE_PROMPT = (
    "ugly, blurry, watermark, text overlay, logo, signature, "
    "cartoon, anime, illustration, drawing, painting, CGI plastic, "
    "overexposed, underexposed, low quality, jpeg artifacts, "
    "distorted, deformed, duplicate, cropped, frame, border, "
    "harsh shadows, cluttered mess, dull flat lighting"
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


# ══════════════════════════════════════════════════════════════════════════════
# VARIETY ENGINE v5 — MOOD & STORY LAYER (owner request, 29 Oct 2026)
#
# Why: the same Sheets prompts kept producing the same FEEL — v4 only varied
# angle/light/composition/season inside one bright-daylight family. v5 adds
# the missing dimension: a MOOD with a small lived-in STORY (storytelling
# props), scene-aware (garden vs interior), including warm romantic moods
# ("evening for two", "rainy window", "love story moment" with a soft-focus
# couple seen from behind — implied romance, no face-garbling close-ups).
#
# This runs inside generate_pin_image(), the single choke point that BOTH
# prompt paths pass through — the CMO's expanded prompt AND the Sheets
# t2i_base used on fallback — so every image gets enhanced, whichever path
# produced its prompt. Zero LLM calls, zero quota: pure composition.
# An anti-repeat history (data/variety_history.json) re-rolls combos that
# were used recently. Disable with IMAGE_MOOD_ENGINE=off (falls back to v4).
# ══════════════════════════════════════════════════════════════════════════════

# Switch config.py ke "SAARE ON/OFF SWITCHES" section se aata hai
# (Render env IMAGE_MOOD_ENGINE se badalta hai); config purana ho toh
# yahi file env padh legi.
if _IMAGE_MOOD_ENGINE_CFG is not None:
    MOOD_ENGINE_ENABLED = bool(_IMAGE_MOOD_ENGINE_CFG)
else:
    MOOD_ENGINE_ENABLED = os.environ.get("IMAGE_MOOD_ENGINE", "on").strip().lower() not in ("off", "false", "0", "no")

# Vision gate switch — same pattern (config first, env fallback).
if _IMAGE_VISION_GATE_CFG is not None:
    VISION_GATE_ENABLED = bool(_IMAGE_VISION_GATE_CFG)
else:
    VISION_GATE_ENABLED = os.environ.get("IMAGE_VISION_GATE", "on").strip().lower() not in ("off", "false", "0", "no")

# Each mood: name, scenes it fits, its own lighting, 1-2 story props picked
# at random, and an optional season override (a winter-hearth mood must not
# collide with a spring season line).
_MOODS = [
    {"name": "candlelit_evening_for_two", "scenes": ("interior",), "season": None,
     "lighting": "warm candlelit evening glow with soft golden ambient light",
     "props": ["two steaming mugs side by side on a wooden tray",
               "an open book resting on a shared knitted throw over the sofa",
               "two glasses and a small vase of roses on the side table"]},
    {"name": "lazy_sunday_morning", "scenes": ("interior",), "season": None,
     "lighting": "soft lazy morning light through linen curtains",
     "props": ["a breakfast tray for two with croissants and coffee",
               "rumpled soft bedding and an open paperback left mid-chapter"]},
    {"name": "rainy_window_evening", "scenes": ("interior",), "season": "rainy evening outside, droplets glistening on the window glass",
     "lighting": "cozy warm interior light against a rainy window evening",
     "props": ["two cups of tea beside a rain-streaked window",
               "a shared blanket and a small stack of novels by the armchair"]},
    {"name": "fireside_winter_night", "scenes": ("interior",), "season": "snowy winter evening outside the window",
     "lighting": "flickering fireplace glow with warm amber light",
     "props": ["two armchairs angled toward the fire with a shared throw",
               "hot chocolate mugs and a plate of cookies on the table between them"]},
    {"name": "golden_hour_garden_for_two", "scenes": ("garden",), "season": None,
     "lighting": "romantic golden hour backlight through garden foliage",
     "props": ["a garden bench for two with cushions and a shared shawl",
               "two lemonade glasses on a small iron table"]},
    {"name": "fairy_light_evening", "scenes": ("garden",), "season": "soft summer dusk",
     "lighting": "twinkling string lights at dusk with warm bokeh",
     "props": ["a candlelit table for two under string lights",
               "two chairs pulled close around a small glowing fire bowl"]},
    {"name": "botanical_morning", "scenes": ("interior", "garden"), "season": None,
     "lighting": "fresh greenhouse morning light filtering through leaves",
     "props": ["a watering can and freshly cut flowers in a ceramic jug",
               "an open garden journal and terracotta pots on the bench"]},
    {"name": "seaside_breeze", "scenes": ("interior", "garden"), "season": None,
     "lighting": "bright sea-breeze daylight, white and airy",
     "props": ["striped linen draped over a woven basket",
               "a bowl of lemons and a glass pitcher on the table"]},
    {"name": "autumn_harvest", "scenes": ("interior", "garden"), "season": "golden autumn day, amber leaves drifting",
     "lighting": "warm autumn afternoon light with amber tones",
     "props": ["pumpkins and a basket of apples by the door",
               "a plaid throw folded over the chair arm"]},
    {"name": "zen_calm", "scenes": ("interior",), "season": None,
     "lighting": "serene soft daylight, calm and uncluttered",
     "props": ["a single ceramic vase with eucalyptus stems",
               "stacked linen cushions in soft neutral tones"]},
    {"name": "vintage_film_afternoon", "scenes": ("interior",), "season": None,
     "lighting": "nostalgic warm afternoon light like vintage film",
     "props": ["a record player with vinyl sleeves leaning on the shelf",
               "polaroid photos pinned in a row above the desk"]},
    {"name": "festive_hearth", "scenes": ("interior",), "season": "cozy winter celebration season",
     "lighting": "festive warm glow with twinkling accent lights",
     "props": ["a garland with oranges and cinnamon sticks on the mantel",
               "wrapped gifts and a bowl of walnuts by the tree"]},
    {"name": "market_morning", "scenes": ("interior", "garden"), "season": None,
     "lighting": "cheerful market-morning brightness",
     "props": ["a basket of fresh bread and sunflowers",
               "jam jars on a checked cloth on the table"]},
    {"name": "love_story_moment", "scenes": ("interior", "garden"), "season": None,
     "lighting": "dreamy golden light with a soft romantic glow, main subject in sharp focus",
     "props": ["in the blurred background, a couple seen from behind holding hands, faces not visible",
               "two coffee cups side by side and one shared open book in the foreground"]},
]

# One short sensory detail — the texture v4 removed for bloat, back in a
# single-phrase budget so prompts stay lean.
_MICRO_DETAILS = [
    "visible linen and warm wood-grain textures",
    "soft dew drops glistening on leaves",
    "gentle steam rising from a warm cup",
    "delicate lace-curtain shadows on the wall",
    "ceramic glaze catching the light",
    "a few fresh petals scattered naturally",
    "warm wood tones with brushed brass accents",
    "crisp folded textiles in soft layers",
]

_GARDEN_WORDS = ("garden", "backyard", "patio", "porch", "terrace", "balcony",
                 "greenhouse", "lawn", "cottage exterior", "flower bed", "courtyard")

_HISTORY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "variety_history.json",
)
_HISTORY_CAP = 60


def _detect_scene(base_prompt: str) -> str:
    text = base_prompt.lower()
    return "garden" if any(w in text for w in _GARDEN_WORDS) else "interior"


def _load_history() -> list:
    try:
        import json
        with open(_HISTORY_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_history(history: list) -> None:
    try:
        import json
        os.makedirs(os.path.dirname(_HISTORY_PATH), exist_ok=True)
        with open(_HISTORY_PATH, "w", encoding="utf-8") as fh:
            json.dump(history[-_HISTORY_CAP:], fh)
    except Exception:
        pass  # history is best-effort; never block an image over it


def _pick_variety_modifiers(base_prompt: str = "") -> dict:
    """Pick variety modifiers — v5 mood-aware when enabled, else v4 pools."""
    if not MOOD_ENGINE_ENABLED:
        return {
            "angle":    random.choice(_CAMERA_ANGLES),
            "lighting": random.choice(_LIGHTING_MOODS),
            "comp":     random.choice(_COMP_STYLES),
            "season":   random.choice(_SEASONS_WEATHER),
            "mood":     "legacy_v4",
            "props":    "",
            "detail":   "",
        }

    scene = _detect_scene(base_prompt)
    fitting = [m for m in _MOODS if scene in m["scenes"]] or _MOODS
    history = _load_history()

    # Anti-repeat: re-roll the (mood, angle, comp) combo if it was used in
    # the recent window — same base prompt must not produce the same look.
    best = None
    for _attempt in range(6):
        mood = random.choice(fitting)
        angle = random.choice(_CAMERA_ANGLES)
        comp = random.choice(_COMP_STYLES)
        key = mood["name"] + "|" + angle + "|" + comp
        best = (mood, angle, comp, key)
        if key not in history:
            break
    mood, angle, comp, key = best
    history.append(key)
    _save_history(history)

    props = ", ".join(random.sample(mood["props"], k=min(2, len(mood["props"]))))
    return {
        "angle":    angle,
        "lighting": mood["lighting"],
        "comp":     comp,
        "season":   mood["season"] or random.choice(_SEASONS_WEATHER),
        "mood":     mood["name"],
        "props":    props,
        "detail":   random.choice(_MICRO_DETAILS),
    }


def _inject_variety(base_prompt: str) -> tuple[str, dict]:
    """
    Enrich the base visual prompt (CMO-expanded or Sheets t2i_base — both
    land here) with variety + mood/story modifiers.
    Returns (enriched_prompt, modifiers_used) — modifiers logged for debugging.
    """
    mods = _pick_variety_modifiers(base_prompt)

    parts = [mods["angle"], mods["lighting"]]
    if mods.get("props"):
        parts.append(mods["props"])
    if mods.get("detail"):
        parts.append(mods["detail"])
    parts += [mods["comp"], mods["season"]]
    variety_block = ", ".join(parts)

    quality_tail = _QUALITY_TAIL

    # Strip existing quality tail if prompts master already added it, re-add cleanly
    base_clean = base_prompt.replace(quality_tail, "").rstrip(", ").strip()

    enriched = f"{base_clean}, {variety_block}, {quality_tail}"
    return enriched, mods


# ── Helpers ────────────────────────────────────────────────────────────────────

_QUALITY_TAIL = (
    "4K ultra HD, photorealistic, razor-sharp focus on the main subject, "
    "crisp fine details, highly detailed, award-winning photography"
)


def _enrich_prompt(prompt: str, max_chars: int = 1400) -> str:
    """Quality tail hamesha rakho — truncation use KABHI na kaate.

    Purana bug (quality girne ki jad, 30 Oct 2026): cap 750 tha aur kataav
    seedha END se hota tha. Variety engine ke baad prompts 850+ chars ke
    ho gaye, toh har image ka quality tail hi kat jata tha — model ko
    '4K... photorealistic... sharp' wala hissa milta hi nahi tha, images
    soft/blurry aati thin. Ab: cap 1400, aur lamba ho toh BASE ka beech
    kat-ta hai, tail hamesha poora rehta hai.
    """
    base = prompt.strip()
    if "4K" not in base and "4k" not in base:
        base += ", " + _QUALITY_TAIL
    if len(base) <= max_chars:
        return base
    marker = ", 4K ultra HD"
    idx = base.rfind(marker)
    if idx > 0:
        head, tail = base[:idx], base[idx:]
        return head[: max_chars - len(tail)] + tail
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


async def _upload_to_imgbb(image_bytes: bytes) -> Optional[str]:
    if not IMGBB_API_KEY:
        logger.error("❌ [ImgBB] IMGBB_API_KEY not set.")
        return None
    encoded = base64.b64encode(image_bytes).decode("utf-8")
    logger.info(f"⬆️  [ImgBB] Uploading {len(image_bytes):,} bytes (permanent)...")
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.imgbb.com/1/upload",
                data={"key": IMGBB_API_KEY, "image": encoded},
            )
            resp.raise_for_status()
            url = resp.json()["data"]["url"]
        logger.info(f"✅ [ImgBB] Hosted: {url}")
        return url
    except Exception as e:
        logger.error(f"❌ [ImgBB] Upload failed: {e}")
        return None


def _is_valid(image_bytes: Optional[bytes]) -> bool:
    return bool(image_bytes) and len(image_bytes) >= _MIN_VALID_BYTES


# ── Model 1: Cloudflare Workers AI ─────────────────────────────────────────────

async def _cloudflare_once(prompt: str, ratio: str) -> Optional[bytes]:
    if not CLOUDFLARE_ACCOUNT_ID or not CLOUDFLARE_API_TOKEN:
        raise RuntimeError("CLOUDFLARE_ACCOUNT_ID or CLOUDFLARE_API_TOKEN not configured.")

    w, h     = _get_dims(ratio)
    # Removing resolution text from prompt to be safe as well
    enriched = _enrich_prompt(prompt)

    url = (
        f"https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}"
        f"/ai/run/{CLOUDFLARE_IMAGE_MODEL}"
    )

    headers = {
        "Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}",
        "Content-Type":  "application/json",
        "User-Agent":    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    model = str(CLOUDFLARE_IMAGE_MODEL)

    async with httpx.AsyncClient(timeout=_CALL_TIMEOUT) as client:
        if "flux-2" in model:
            # FLUX.2 (klein/dev): multipart form, supports real width/height (256-1920)
            form = {
                "prompt": (None, enriched),
                "width":  (None, str(w)),
                "height": (None, str(h)),
            }
            resp = await client.post(
                url,
                headers={k: v for k, v in headers.items() if k != "Content-Type"},
                files=form,
            )
        else:
            # flux-1-schnell: ONLY prompt + steps (max 8). No width/height -> always 1024x1024.
            resp = await client.post(url, headers=headers, json={"prompt": enriched, "steps": 8})
        resp.raise_for_status()

        if "application/json" in resp.headers.get("content-type", "").lower():
            data    = resp.json()
            b64_str = data.get("result", {}).get("image", "")
            if b64_str:
                img = base64.b64decode(b64_str)
            else:
                logger.error(f"❌ [Cloudflare] No image in response: {str(data)[:200]}")
                return None
        else:
            img = resp.content

    # schnell can't do 9:16 -> crop square to ratio + upscale (only if needed)
    if "flux-2" not in model:
        img = _fit_to_ratio(img, ratio)
    return img


def _fit_to_ratio(img_bytes: bytes, ratio: str) -> bytes:
    """Center-crop to target ratio, resize with LANCZOS. Needs Pillow; falls back to raw bytes."""
    try:
        import io
        from PIL import Image
        w, h = _get_dims(ratio)
        im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        iw, ih = im.size
        target = w / h
        if iw / ih > target:            # too wide -> crop sides
            nw = int(ih * target)
            im = im.crop(((iw - nw) // 2, 0, (iw - nw) // 2 + nw, ih))
        elif iw / ih < target:          # too tall -> crop top/bottom
            nh = int(iw / target)
            im = im.crop((0, (ih - nh) // 2, iw, (ih - nh) // 2 + nh))
        im = im.resize((w, h), Image.LANCZOS)
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=95, subsampling=0)
        return out.getvalue()
    except Exception as e:
        logger.warning(f"⚠️ [Ratio fit] skipped: {e!r}")
        return img_bytes


async def _t2i_cloudflare(prompt: str, ratio: str) -> Optional[bytes]:
    for attempt in range(1, _CF_MAX_RETRIES + 1):
        try:
            logger.info(f"🎨 [Cloudflare] Attempt {attempt}/{_CF_MAX_RETRIES} | ratio={ratio}")
            img = await _cloudflare_once(prompt, ratio)
            if _is_valid(img):
                logger.info(f"✅ [Cloudflare] {len(img):,} bytes on attempt {attempt}")
                return img
            logger.warning(f"⚠️ [Cloudflare] Attempt {attempt}: invalid/too small")
        except Exception as e:
            logger.warning(
                f"⚠️ [Cloudflare] Attempt {attempt} error: "
                f"{type(e).__name__}: {e!r} | cause={e.__cause__!r}"
            )

        if attempt < _CF_MAX_RETRIES:
            delay = random.randint(30, 50)
            logger.info(f"⏳ [Cloudflare] Waiting {delay}s before retry...")
            await asyncio.sleep(delay)

    logger.error("❌ [Cloudflare] All attempts failed — moving to Replicate.")
    return None


# ── Model 2: Replicate FLUX schnell (V4 — Cloudflare outage fallback) ─────────
# Official model endpoint with Prefer: wait — Replicate runs the prediction
# synchronously and returns output image URLs. Same schnell model family as
# the old HF layer, so fallback pins keep the same visual character.
# Cost: $0.003 per output image (flat) — even a full month of fallback
# traffic (~600 pins) is under $2. Layer is OFF until REPLICATE_API_TOKEN
# is set in the environment.

async def _replicate_once(prompt: str, ratio: str) -> Optional[bytes]:
    enriched = _enrich_prompt(prompt, max_chars=500)
    headers = {
        "Authorization": f"Bearer {REPLICATE_API_TOKEN}",
        "Content-Type": "application/json",
        "Prefer": "wait",
    }
    payload = {
        "input": {
            "prompt": enriched,
            "aspect_ratio": "9:16" if ratio == "9:16" else "1:1",
            "output_format": "jpg",
            "output_quality": 90,
            "num_outputs": 1,
        }
    }
    async with httpx.AsyncClient(timeout=_CALL_TIMEOUT) as client:
        resp = await client.post(_REPLICATE_FLUX_URL, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
        output = data.get("output") or []
        if isinstance(output, str):
            output = [output]
        if not output:
            logger.error(f"❌ [Replicate] No output in response: {str(data)[:200]}")
            return None
        return await _download_bytes(output[0])


async def _t2i_replicate(prompt: str, ratio: str) -> Optional[bytes]:
    if not REPLICATE_API_TOKEN:
        logger.info("ℹ️ [Replicate] REPLICATE_API_TOKEN not set — layer skipped.")
        return None
    for attempt in range(1, _REP_MAX_RETRIES + 1):
        try:
            logger.info(f"🎨 [Replicate] Attempt {attempt}/{_REP_MAX_RETRIES} | ratio={ratio}")
            img = await _replicate_once(prompt, ratio)
            if _is_valid(img):
                logger.info(f"✅ [Replicate] {len(img):,} bytes on attempt {attempt}")
                return img
            logger.warning(f"⚠️ [Replicate] Attempt {attempt}: invalid/too small")
        except Exception as e:
            logger.warning(f"⚠️ [Replicate] Attempt {attempt} error: {type(e).__name__}: {e!r}")
        if attempt < _REP_MAX_RETRIES:
            logger.info(f"⏳ [Replicate] Waiting {_REP_RETRY_DELAY}s before retry...")
            await asyncio.sleep(_REP_RETRY_DELAY)
    logger.error("❌ [Replicate] All attempts failed — moving to HuggingFace.")
    return None


# ── Model 3: Hugging Face FLUX.1-schnell ───────────────────────────────────────

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
# PROMPT DIET + VISION GATE (owner ki shikayat, 30 Oct 2026)
# ──────────────────────────────────────────────────────────────────────────────
# Hybrid/gadbad images (bed+sofa mixture, disgusting clutter room) ki jad:
# chhota 4B image model 20+ objects wala 1000+ char prompt nahi sambhal
# pata — paas ke shabdon ke features ghul jate hain. Ilaaj do hisso me:
#   1) DIET — image model ko sirf saaf-suthra chhota prompt: pehle ~10
#      clauses / ~430 chars (subject + hero furniture pehle aate hain,
#      object-bheed peeche kat jati hai). Mood/variety/tail upar lagte hain.
#   2) GATE — bani image post se pehle Gemini Vision se check; hybrid ya
#      pighli shape mile toh diet prompt se EK retry, phir bhi kharab ho
#      toh pin skip (board par gadbad ja hi nahi payegi).
# Gate FAIL-OPEN hai: Gemini down/429 ho toh image rukti nahi, post hoti
# hai (raat ke peak par pipeline band karna ilaaj nahi hai).
# ══════════════════════════════════════════════════════════════════════════════
_DIET_MAX_CHARS   = 470
_DIET_MAX_CLAUSES = 11

# Diet SMART hai, andha kataav nahi (owner ki sahi chinta, 30 Oct 2026):
# prompts me sirf furniture nahi hota — RANG (palette), ROSHNI (light) aur
# CAMERA wali details aksar prompt ke AAKHIR me hoti hain. Seedha pehle-10
# wala kataav unhe uda deta. Isliye pehle category-wise sabse achhi clause
# chun kar rakhi jati hai, phir bheed (object list) kat-ti hai.
_DIET_LIGHT_RE  = re.compile(
    r"(light|sunlight|golden hour|backlit|shadow|glow|candlelit|dusk|dawn|"
    r"kelvin|\d{4}k\b|window light|twilight)", re.IGNORECASE)
_DIET_COLOR_RE  = re.compile(
    r"(palette|colou?r|pastel|tones?|hues?|sage|blush|terracotta|ivory|"
    r"cream|mustard|emerald|monochrome)", re.IGNORECASE)
_DIET_CAMERA_RE = re.compile(
    r"(shot on|canon|sony|nikon|lens|\d+mm|f/1\.|depth of field|bokeh|"
    r"editorial|photography|magazine)", re.IGNORECASE)


def _diet_base_prompt(prompt: str, max_chars: int = _DIET_MAX_CHARS,
                      max_clauses: int = _DIET_MAX_CLAUSES) -> str:
    """
    Category-aware trim:
      1) pehle 2 clauses hamesha (subject + hero furniture wahi hote hain)
      2) LIGHT, COLOR, CAMERA — teeno ki sabse keyword-rich ek-ek clause
         hamesha bachegi, chahe prompt ke aakhir me hi kyun na ho
      3) bachi jagah me object clauses apne order me bharenge
    Output original order me hi rehta hai; duplicates hate; kataav sirf
    clause-boundary par (aadha phrase kabhi nahi katega).
    """
    clauses = [c.strip() for c in str(prompt or "").split(",") if c.strip()]
    seen, uniq = set(), []
    for c in clauses:
        k = c.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    if len(uniq) <= 3:
        return ", ".join(uniq)

    keep = set(range(min(2, len(uniq))))                    # subject + hero
    for rx in (_DIET_LIGHT_RE, _DIET_COLOR_RE, _DIET_CAMERA_RE):
        best, best_hits = None, 0
        for i, c in enumerate(uniq):
            hits = len(rx.findall(c))
            if hits > best_hits:
                best, best_hits = i, hits
        if best is not None:
            keep.add(best)

    total = sum(len(uniq[i]) + 2 for i in keep)
    for i in range(len(uniq)):                              # bheed order me
        if i in keep:
            continue
        if len(keep) >= max_clauses or (len(keep) >= 3 and total + len(uniq[i]) > max_chars):
            continue
        keep.add(i)
        total += len(uniq[i]) + 2
    return ", ".join(uniq[i] for i in sorted(keep))


_GATE_PROMPT = """You are a strict photo-quality inspector for a home-decor Pinterest account.
Look at this AI-generated interior/garden photo and reply with ONLY this JSON:
{"ok": true, "issues": []}
Set "ok": false ONLY for clear defects:
- furniture that is a HYBRID of two different objects (e.g. half bed + half sofa)
- melted, warped or physically impossible shapes or geometry
- a chaotic, cluttered, disgusting-looking room with random junk everywhere
- badly distorted people or faces
Normal cozy styling, plants, warm light and decor objects are FINE. When unsure, ok=true."""

_gate_clients: dict = {}


def _get_gate_client(api_key: str):
    """Per-key Gemini client cache (key 1 aur key 2 ke alag quota hote hain)."""
    if api_key not in _gate_clients:
        try:
            from google import genai
            _gate_clients[api_key] = genai.Client(api_key=api_key)
        except Exception:
            return None
    return _gate_clients[api_key]


def _parse_gate_verdict(raw: str, judge: str) -> tuple:
    m = re.search(r"\{.*\}", raw or "", re.S)
    data = json.loads(m.group(0)) if m else {}
    ok = bool(data.get("ok", True))
    issues = ", ".join(str(i) for i in (data.get("issues") or []))
    return ok, f"[{judge}] " + (issues or ("clean" if ok else "defects found"))


def _gemini_judge(api_key: str, image_bytes: bytes, model: str = None) -> str:
    from google.genai import types
    client = _get_gate_client(api_key)
    if client is None:
        raise RuntimeError("Gemini client nahi bana")
    model = model or os.environ.get("VISION_GATE_MODEL") or _GEMINI_MODEL_CFG or "gemini-2.5-flash"
    resp = client.models.generate_content(
        model=model,
        contents=[
            _GATE_PROMPT,
            types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
        ],
        config=types.GenerateContentConfig(
            temperature=0.0, max_output_tokens=300,
            response_mime_type="application/json",
        ),
    )
    return resp.text or ""


def _groq_judge(image_bytes: bytes) -> str:
    """Teesra judge — Groq Vision (llama-4-scout). Provider hi alag hai:
    jis raat Gemini peak par hota hai, Groq aaram se chalta hai.
    (Model kabhi free tier se hate toh ye step khud skip ho jayega —
    exception upar jaakar agla raasta lega, kuch rukega nahi.)"""
    groq_key = os.environ.get("GROQ_API_KEY", "")
    if not groq_key:
        raise RuntimeError("GROQ_API_KEY not configured")
    from groq import Groq
    gk = os.environ.get("GROQ_API_KEY", "")
    client = Groq(api_key=gk)
    model = os.environ.get("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
    resp = client.chat.completions.create(
        model=model,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": _GATE_PROMPT},
                {"type": "image_url",
                 "image_url": {"url": "data:image/jpeg;base64,"
                                      + base64.b64encode(image_bytes).decode()}},
            ],
        }],
        temperature=0.0,
        max_tokens=300,
    )
    return resp.choices[0].message.content or ""


async def _vision_gate(image_bytes: bytes) -> tuple:
    """
    Judge CHAIN — pehle jo judge jinda mile, usi ka faisla:
      1) Gemini key 1 → 2) Gemini key 2 (alag quota) → 3) Groq Vision
    Teeno down hon TAB fail-open (True) — judge ki outage par pipeline
    nahi rukegi, par ek judge ke peak par hone se gate andha nahi hoga.
    """
    if not VISION_GATE_ENABLED:
        return True, "gate off"

    key1 = _GEMINI_API_KEY_CFG or os.environ.get("GEMINI_API_KEY", "")
    key2 = os.environ.get("GEMINI_API_KEY_2", "")
    # Owner ki sahi baat (30 Oct 2026): model PEAK par ho toh dono keys EK HI
    # model par girengi — key badalna kaafi nahi, MODEL bhi badalna hoga.
    # Isliye judge 2 alag key ke saath ALAG MODEL (2.5-flash) use karta hai:
    # 3.8-flash ki bheed 2.5-flash ko nahi rokti, aur iska quota bhi alag hai.
    model1 = os.environ.get("VISION_GATE_MODEL") or _GEMINI_MODEL_CFG or "gemini-3.8-flash"
    model2 = os.environ.get("VISION_GATE_MODEL_2", "gemini-2.5-flash")
    judges = []
    if key1:
        judges.append((f"Gemini#1({model1})", lambda: _gemini_judge(key1, image_bytes, model1)))
    if key2:
        judges.append((f"Gemini#2({model2})", lambda: _gemini_judge(key2, image_bytes, model2)))
    judges.append(("GroqVision", lambda: _groq_judge(image_bytes)))

    for name, fn in judges:
        try:
            raw = await asyncio.to_thread(fn)
            return _parse_gate_verdict(raw, name)
        except Exception as e:
            logger.warning(
                f"👁️ [VisionGate] {name} judge fail ({type(e).__name__}: "
                f"{str(e)[:100]}) — agla judge try hoga"
            )
    logger.warning("👁️ [VisionGate] Saare judge down — fail-open, image bina check post hogi")
    return True, "all judges down — fail-open"


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
      4. Upload result to ImgBB for permanent hosting

    Args:
        visual_prompt: The detailed visual prompt from prompts master
        ratio: "9:16" (Pinterest portrait) or "1:1" (square)

    Returns:
        ImgBB hosted URL or None if all models fail
    """
    w, h = _get_dims(ratio)
    logger.info(f"🎨 [Image Pipeline] PIN | ratio={ratio} ({w}x{h})")

    # PROMPT DIET — base ki object-bheed pehle kaato (hybrid ki jad),
    # phir variety/mood upar lagega. Dono raaste (CMO + Sheets) yahi se.
    diet_base = _diet_base_prompt(visual_prompt)
    if len(diet_base) < len(str(visual_prompt)):
        logger.info(
            f"🥗 [PromptDiet] base {len(str(visual_prompt))} → {len(diet_base)} chars "
            f"(pehle clauses rakhe, object-bheed kati)"
        )

    # Inject variety + mood/story (v5) — same treatment for CMO prompts
    # and Sheets fallback prompts alike; this is the single choke point.
    enriched_prompt, mods = _inject_variety(diet_base)
    logger.info(
        f"🎲 [Variety Engine v5] mood={mods.get('mood', '?')} | "
        f"angle={mods['angle'][:35]} | "
        f"light={mods['lighting'][:35]} | "
        f"comp={mods['comp'][:35]} | "
        f"season={mods['season'][:35]}"
    )
    logger.info(f"📝 [Prompt] {len(enriched_prompt)} chars: {enriched_prompt[:120]}...")

    image_bytes = await _generate_bytes(enriched_prompt, ratio)
    if not image_bytes:
        logger.error("❌ [Image Pipeline] All models exhausted — no image generated.")
        return None

    # ── VISION GATE — post se pehle quality check ──────────────────────────
    ok, reason = await _vision_gate(image_bytes)
    if not ok:
        logger.warning(f"👁️ [VisionGate] REJECT — {reason} | aur tight diet se ek retry...")
        tighter = _diet_base_prompt(visual_prompt, max_chars=280, max_clauses=6)
        retry_prompt, _retry_mods = _inject_variety(tighter)
        # Retry poore layer cascade se — pehli image kisi fallback layer se
        # aayi ho aur Cloudflare down ho, toh bhi retry ka mauka mile.
        retry_bytes = await _generate_bytes(retry_prompt, ratio)
        if not retry_bytes:
            logger.error("👁️ [VisionGate] Retry par image bani hi nahi — pin SKIP.")
            return None
        ok2, reason2 = await _vision_gate(retry_bytes)
        if not ok2:
            logger.error(
                f"👁️ [VisionGate] Retry bhi REJECT — {reason2} | "
                f"pin SKIP (kharab image board par nahi jayegi)"
            )
            return None
        logger.info(f"👁️ [VisionGate] Retry PASS — {reason2}")
        image_bytes = retry_bytes
    else:
        logger.info(f"👁️ [VisionGate] PASS — {reason}")

    return await _upload_to_imgbb(image_bytes)


async def _generate_bytes(prompt: str, ratio: str) -> Optional[bytes]:
    """Layer cascade: Cloudflare → Replicate → HuggingFace → Pollinations."""
    image_bytes = await _t2i_cloudflare(prompt, ratio)
    if not image_bytes:
        logger.info("🔄 [Image Pipeline] Cloudflare exhausted — trying Replicate...")
        image_bytes = await _t2i_replicate(prompt, ratio)
    if not image_bytes:
        logger.info("🔄 [Image Pipeline] Replicate exhausted — trying HuggingFace...")
        image_bytes = await _t2i_huggingface(prompt, ratio)
    if not image_bytes:
        logger.info("🔄 [Image Pipeline] HuggingFace exhausted — trying Pollinations...")
        image_bytes = await _t2i_pollinations(prompt, ratio)
    return image_bytes


async def upload_raw_image(image_url: str) -> Optional[str]:
    """Download an affiliate product image and re-host on ImgBB."""
    logger.info(f"⬇️  [Image Pipeline] AFFILIATE_PIN — downloading: {image_url[:60]}...")
    image_bytes = await _download_bytes(image_url)
    if not image_bytes:
        logger.error("❌ [Image Pipeline] Product image download failed.")
        return None
    return await _upload_to_imgbb(image_bytes)
