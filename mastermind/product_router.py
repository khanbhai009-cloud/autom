"""
mastermind/product_router.py — Pinteresto V4 Phase 1: Product Router

Decides, at pin-creation time, which digital product (if any) a new pin
should carry as its Pinterest destination link:

    garden pin  -> Dream Garden Makeover Kit   ($15, Gumroad)
    decor pin   -> Home Decor Styling Kit      ($15, Gumroad)
    no clear fit -> NO destination (pin posts clean — by design)

WHY (owner's logic, 29 Oct 2026): Pinterest is post-and-forget. A pin takes
1-2 months to earn impressions, and ~600 new pins will be created in that
window — retroactively hand-linking pins (58 pins edited by hand on
28-29 Oct) can never scale. The decision must happen inside the pipeline,
at creation time. Forcing a product onto a non-matching pin hurts both
conversion and Pinterest's relevance signal, so "none" is a first-class
outcome, not a failure.

DECISION FLOW (cheapest first, LLM only when needed):
  1. Rule scoring over title/description/visual_prompt/board/tags —
     weighted phrase signals per product + traffic/scenery signals that
     push towards "none".
  2. Clear rule winner (confidence >= threshold) -> decided, zero LLM cost.
  3. Ambiguous -> LLM judge chain: Groq gpt-oss-20b -> Groq qwen -> Gemini.
     Judge sees both product descriptions; returns product + confidence.
  4. Final confidence < PRODUCT_ROUTER_THRESHOLD (default 0.70) -> none.

Scope: Account 1 (HomeDecor) only. The two kits are home/garden products;
Account 2 (Tech) always routes to none.

Every decision can be logged to Firestore collection `product_router_log`
via log_router_decision() — best-effort, never raises, never blocks a pin.

Env knobs (all optional):
  PRODUCT_ROUTER_ENABLED   master switch (default "true")
  PRODUCT_ROUTER_THRESHOLD min confidence to attach a link (default "0.70")
  ROUTER_LLM_ENABLED       allow the LLM judge step (default "true")
  ROUTER_JUDGE_MODEL       Groq judge model (default "openai/gpt-oss-20b")
  PRODUCT_ROUTER_UTM       append utm params to the destination URL
                           (default "false" — clean URLs, exactly as the
                           hand-linked pins that were verified live)
  PRODUCT_GARDEN_URL / PRODUCT_DECOR_URL   override destinations (config.py)
"""

import hashlib
import json
import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
try:
    from config import PRODUCT_DECOR_URL, PRODUCT_GARDEN_URL
except Exception:  # config not updated yet / import context — use live defaults
    PRODUCT_GARDEN_URL = os.getenv(
        "PRODUCT_GARDEN_URL", "https://cozycanvasstudio.gumroad.com/l/nmkawd"
    )
    PRODUCT_DECOR_URL = os.getenv(
        "PRODUCT_DECOR_URL", "https://cozycanvasstudio.gumroad.com/l/ictsf"
    )

ROUTER_ENABLED = os.getenv("PRODUCT_ROUTER_ENABLED", "true").strip().lower() in (
    "1", "true", "yes", "on",
)
ROUTER_THRESHOLD = float(os.getenv("PRODUCT_ROUTER_THRESHOLD", "0.70"))
ROUTER_LLM_ENABLED = os.getenv("ROUTER_LLM_ENABLED", "true").strip().lower() in (
    "1", "true", "yes", "on",
)
ROUTER_JUDGE_MODEL = os.getenv("ROUTER_JUDGE_MODEL", "openai/gpt-oss-20b")
ROUTER_UTM_ENABLED = os.getenv("PRODUCT_ROUTER_UTM", "false").strip().lower() in (
    "1", "true", "yes", "on",
)

# ── Product registry ──────────────────────────────────────────────────────────
# signals: phrase -> weight. Multi-word phrases are the strongest signal (3),
# single topical words medium (2), weak context words (1). Matching is on the
# full pin text (title + description + visual_prompt + board + niche + tags).
PRODUCTS: dict = {
    "garden_kit": {
        "name": "Dream Garden Makeover Kit",
        "price": 15,
        "url": PRODUCT_GARDEN_URL,
        "blurb": (
            "A printable workbook for planning a dream garden makeover: "
            "layouts, planting zones, cottage-garden styling and step-by-step "
            "planning pages for gardens, backyards, balconies and porches."
        ),
        "signals": {
            "cottage garden": 3, "garden makeover": 3, "garden design": 3,
            "dream garden": 3, "backyard garden": 3, "balcony garden": 3,
            "garden path": 3, "garden swing": 3, "flower garden": 3,
            "garden gate": 3, "garden plan": 3, "kitchen garden": 3,
            "garden": 2, "backyard": 2, "balcony": 2, "porch": 2,
            "patio": 2, "flower bed": 2, "flowerbed": 2, "planting": 2,
            "greenhouse": 2, "terrace garden": 2, "courtyard": 1,
            "bloom": 1, "blossom": 1, "roses": 1, "hydrangea": 1,
            "lavender": 1, "climbing plants": 1, "potted plants": 1,
        },
        "ctas": [
            "Love this garden look? Plan your own step-by-step with the Dream Garden Makeover Kit — link below 🌿",
            "Want a garden like this? The Dream Garden Makeover Kit walks you through the full plan — tap the link 🌸",
            "Save this for your garden plans — and grab the Dream Garden Makeover Kit (link below) to plan it properly ✨",
            "This look starts with a plan. Get the Dream Garden Makeover Kit via the link and design yours 🌼",
        ],
    },
    "decor_kit": {
        "name": "Home Decor Styling Kit",
        "price": 15,
        "url": PRODUCT_DECOR_URL,
        "blurb": (
            "A printable workbook for styling cozy home interiors: room-by-room "
            "styling formulas, colour palettes, layout and finishing touches "
            "for bedrooms, living rooms and cozy corners."
        ),
        "signals": {
            "living room": 3, "bedroom decor": 3, "home decor": 3,
            "room makeover": 3, "interior styling": 3, "cozy corner": 3,
            "reading nook": 3, "bedroom styling": 3, "small space styling": 3,
            "gallery wall": 3, "room styling": 3,
            "bedroom": 2, "interior": 2, "decor": 2, "styling": 2,
            "makeover": 1, "boho": 2, "pastel room": 2, "vanity": 2,
            "bookshelf styling": 2, "coffee table": 1, "sofa": 1,
            "curtains": 1, "bedding": 1, "cushion": 1, "apartment decor": 2,
            "kitchen decor": 2, "dining room": 2,
        },
        "ctas": [
            "Love this look? Style it room-by-room with the Home Decor Styling Kit — link below 🏡",
            "Want your room to feel like this? The Home Decor Styling Kit has the formulas — tap the link ✨",
            "Save this inspo — and get the Home Decor Styling Kit (link below) to recreate the look step-by-step 🛋️",
            "This vibe is a formula, not luck. Grab the Home Decor Styling Kit via the link and style yours 🤍",
        ],
    },
}

# Pins whose job is traffic/trust, not selling (nature/scenery is this
# account's strongest save-driver). Strong traffic signals + weak product
# signals => route to none. Christmas/seasonal pins were deliberately left
# unlinked by hand on 29 Oct — same rule here.
TRAFFIC_SIGNALS = {
    "mountain": 2, "ocean": 2, "sea view": 2, "landscape": 2, "waterfall": 2,
    "sunset view": 2, "scenic": 2, "nature view": 2, "lake": 1, "forest": 1,
    "valley": 1, "freedom": 1, "wanderlust": 1, "travel": 1, "sky": 1,
    "christmas": 2, "halloween": 2, "xmas": 2,
}


# ── Text + rule scoring ───────────────────────────────────────────────────────

def _pin_text(strategy: dict) -> str:
    parts = [
        str(strategy.get("title", "")),
        str(strategy.get("description", "")),
        str(strategy.get("visual_prompt", "")),
        str(strategy.get("board_name", "")),
        str(strategy.get("niche", "")),
        str(strategy.get("alt_text", "")),
        " ".join(str(t) for t in (strategy.get("tags") or [])),
        " ".join(str(t) for t in (strategy.get("board_keywords") or [])),
    ]
    return " ".join(parts).lower()


def _score_signals(text: str, signals: dict) -> tuple[int, list]:
    points, hits = 0, []
    for phrase, weight in signals.items():
        if re.search(r"(?<![a-z])" + re.escape(phrase) + r"(?![a-z])", text):
            points += weight
            hits.append(phrase)
    return points, hits


def _rule_scores(text: str) -> dict:
    out = {}
    for pid, product in PRODUCTS.items():
        pts, hits = _score_signals(text, product["signals"])
        out[pid] = {"points": pts, "hits": hits}
    traffic_pts, traffic_hits = _score_signals(text, TRAFFIC_SIGNALS)
    out["_traffic"] = {"points": traffic_pts, "hits": traffic_hits}
    return out


def _rule_confidence(winner_pts: int, margin: int, traffic_pts: int) -> float:
    """Map rule points to a calibrated-ish confidence in [0, 0.95]."""
    conf = 0.45 + 0.06 * winner_pts + 0.04 * margin
    if traffic_pts >= 3:
        conf -= 0.15  # strong scenery/seasonal pull — demand real product fit
    return max(0.0, min(0.95, conf))


def _destination_url(product_id: str) -> str:
    url = PRODUCTS[product_id]["url"]
    if ROUTER_UTM_ENABLED:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}utm_source=pinterest&utm_medium=pin&utm_campaign={product_id}"
    return url


def _pick_cta(product_id: str, seed_text: str) -> str:
    ctas = PRODUCTS[product_id]["ctas"]
    idx = int(hashlib.md5(seed_text.encode("utf-8")).hexdigest(), 16) % len(ctas)
    return ctas[idx]


def _result(product_id: Optional[str], confidence: float, method: str,
            reason: str, seed_text: str) -> dict:
    if not product_id:
        return {
            "product_id": "",
            "destination_url": "",
            "product_confidence": round(confidence, 2),
            "product_method": method,
            "product_reason": reason,
            "product_cta": "",
        }
    return {
        "product_id": product_id,
        "destination_url": _destination_url(product_id),
        "product_confidence": round(confidence, 2),
        "product_method": method,
        "product_reason": reason,
        "product_cta": _pick_cta(product_id, seed_text),
    }


# ── LLM judge (only for ambiguous pins) ───────────────────────────────────────

_JUDGE_PROMPT = """You are the product router for a Pinterest home-decor account.
Two digital products exist:
- garden_kit: {garden_blurb}
- decor_kit: {decor_blurb}

PIN:
title: {title}
description: {description}
visual_prompt: {visual_prompt}
board: {board}

Decide which product this pin's audience would genuinely want. If neither
fits clearly (e.g. pure nature/scenery, seasonal, unrelated), answer none —
a wrong link is worse than no link.
Reply with ONLY this JSON: {{"product": "garden_kit"|"decor_kit"|"none", "confidence": 0.0-1.0, "reason": "one short line"}}"""


def _judge_groq(prompt: str, model: str) -> Optional[dict]:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return None
    from groq import Groq

    client = Groq(api_key=api_key)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        max_tokens=800,  # gpt-oss are reasoning models — leave room to finish
        response_format={"type": "json_object"},
    )
    return json.loads(resp.choices[0].message.content)


def _judge_gemini(prompt: str) -> Optional[dict]:
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY_2")
    if not api_key:
        return None
    from google import genai as _genai
    from google.genai import types as _gtypes

    client = _genai.Client(api_key=api_key)
    resp = client.models.generate_content(
        model=os.getenv("GEMINI_CMO_MODEL", "gemini-3.8-flash"),
        contents=prompt,
        config=_gtypes.GenerateContentConfig(
            temperature=0.1,
            max_output_tokens=500,
            response_mime_type="application/json",
        ),
    )
    return json.loads(resp.text.strip())


def _llm_judge(strategy: dict) -> Optional[dict]:
    prompt = _JUDGE_PROMPT.format(
        garden_blurb=PRODUCTS["garden_kit"]["blurb"],
        decor_blurb=PRODUCTS["decor_kit"]["blurb"],
        title=str(strategy.get("title", ""))[:120],
        description=str(strategy.get("description", ""))[:300],
        visual_prompt=str(strategy.get("visual_prompt", ""))[:300],
        board=str(strategy.get("board_name", ""))[:80],
    )
    chain = [
        (f"groq:{ROUTER_JUDGE_MODEL}", lambda: _judge_groq(prompt, ROUTER_JUDGE_MODEL)),
        ("groq:qwen/qwen3.8-27b", lambda: _judge_groq(prompt, "qwen/qwen3.8-27b")),
        ("gemini", lambda: _judge_gemini(prompt)),
    ]
    for label, call in chain:
        try:
            verdict = call()
            if verdict and verdict.get("product") in ("garden_kit", "decor_kit", "none"):
                logger.info(f"🧭 [Router] LLM judge ({label}): {verdict}")
                return {
                    "product": verdict["product"],
                    "confidence": float(verdict.get("confidence", 0)),
                    "reason": str(verdict.get("reason", ""))[:200],
                    "method": f"llm:{label}",
                }
        except Exception as e:
            logger.warning(f"⚠️ [Router] Judge {label} failed: {e}")
    return None


# ── Main entry ────────────────────────────────────────────────────────────────

def route_product(strategy: dict, account_key: str = "account_1") -> dict:
    """
    Decide the destination for one pin.

    Args:
        strategy: CMO strategy dict (title, description, visual_prompt,
                  board_name, niche, tags, ...).
        account_key: "account_1" (HomeDecor) or "account_2" (Tech).

    Returns dict with: product_id, destination_url, product_confidence,
    product_method, product_reason, product_cta. Empty product_id/url means
    "post clean, no destination". Never raises.
    """
    seed = str(strategy.get("title", ""))
    try:
        if not ROUTER_ENABLED:
            return _result(None, 0.0, "disabled", "PRODUCT_ROUTER_ENABLED=false", seed)
        if account_key != "account_1":
            return _result(None, 1.0, "scope", "Kits are HomeDecor-only; tech account routes to none", seed)

        text = _pin_text(strategy)
        scores = _rule_scores(text)
        g, d = scores["garden_kit"], scores["decor_kit"]
        traffic = scores["_traffic"]

        ranked = sorted(
            (("garden_kit", g["points"]), ("decor_kit", d["points"])),
            key=lambda x: x[1], reverse=True,
        )
        (winner, w_pts), (_, l_pts) = ranked
        margin = w_pts - l_pts
        rule_conf = _rule_confidence(w_pts, margin, traffic["points"])

        logger.info(
            f"🧭 [Router] garden={g['points']} {g['hits'][:4]} | "
            f"decor={d['points']} {d['hits'][:4]} | traffic={traffic['points']} | "
            f"rule_conf={rule_conf:.2f}"
        )

        # Clear rule winner — no LLM spend.
        if w_pts >= 3 and rule_conf >= ROUTER_THRESHOLD:
            return _result(
                winner, rule_conf, "rules",
                f"signals {scores[winner]['hits'][:4]} (pts {w_pts} vs {l_pts})",
                seed,
            )

        # Ambiguous — ask the judge chain if allowed.
        if ROUTER_LLM_ENABLED and w_pts >= 1:
            verdict = _llm_judge(strategy)
            if verdict:
                if verdict["product"] != "none" and verdict["confidence"] >= ROUTER_THRESHOLD:
                    return _result(
                        verdict["product"], verdict["confidence"],
                        verdict["method"], verdict["reason"], seed,
                    )
                return _result(
                    None, verdict["confidence"], verdict["method"],
                    f"judge: {verdict['product']} @ {verdict['confidence']:.2f} — {verdict['reason']}",
                    seed,
                )

        # No clear fit — post clean. This is the designed outcome for
        # scenery/seasonal/traffic pins, not an error.
        return _result(
            None, rule_conf, "rules",
            f"no clear fit (garden {g['points']} / decor {d['points']} / traffic {traffic['points']})",
            seed,
        )
    except Exception as e:
        logger.error(f"❌ [Router] route_product failed (pin will post clean): {e}")
        return _result(None, 0.0, "error", f"router error: {e}"[:200], seed)


# ── Decision logging (Firestore, best-effort) ─────────────────────────────────

def log_router_decision(strategy: dict, decision: dict, account_key: str = "account_1") -> None:
    """Write one row to Firestore `product_router_log`. Never raises."""
    try:
        from tools.firebase_publisher import _get_db

        db = _get_db()
        if not db:
            logger.info("🧭 [Router] Firestore not configured — decision logged to stdout only")
            return
        from firebase_admin import firestore as _fs

        db.collection("product_router_log").add({
            "account": account_key,
            "title": str(strategy.get("title", ""))[:120],
            "board_name": str(strategy.get("board_name", ""))[:80],
            "product_id": decision.get("product_id", ""),
            "destination_url": decision.get("destination_url", ""),
            "confidence": decision.get("product_confidence", 0),
            "method": decision.get("product_method", ""),
            "reason": decision.get("product_reason", ""),
            "created_at": _fs.SERVER_TIMESTAMP,
        })
        logger.info(f"🧭 [Router] Decision logged → {decision.get('product_id') or 'none'}")
    except Exception as e:
        logger.warning(f"⚠️ [Router] Decision log failed (non-fatal): {e}")
