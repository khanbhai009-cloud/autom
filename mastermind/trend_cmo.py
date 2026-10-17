"""Trend-aware CMO strategy; None means the unchanged regular strategy should run."""
import logging
import random
import re

from sheets import load_prompt_tracker, save_prompt_tracker
from sheets.trends import get_active_trends, get_trend_prompts

logger = logging.getLogger(__name__)
_TAIL = ", 4K ultra HD, photorealistic, highly detailed, award-winning photography"


def _camel(keyword: str) -> str:
    words = re.sub(r"[^A-Za-z0-9 ]", " ", keyword)
    return "".join(word.capitalize() for word in words.split())


def _pick_trend_and_row(account_key: str):
    active = get_active_trends()
    if not active:
        return None, None

    tracker = load_prompt_tracker()
    pick_key = f"trend_pick__{account_key}"
    start = (tracker.get(pick_key, -1) + 1) % len(active)
    tracker[pick_key] = start

    for offset in range(len(active)):
        trend = active[(start + offset) % len(active)]
        rows = get_trend_prompts(trend["trend"])
        account_rows = [
            row for row in rows
            if str(row.get("account", "")).strip() == account_key
        ]
        candidates = account_rows or rows
        if not candidates:
            continue

        row_key = f"trend__{trend['trend']}__{account_key}"
        index = (tracker.get(row_key, -1) + 1) % len(candidates)
        tracker[row_key] = index
        save_prompt_tracker(tracker)
        logger.info(
            "   [%s] TREND '%s' prompt [%s/%s]",
            account_key,
            trend["trend"],
            index + 1,
            len(candidates),
        )
        return trend, dict(candidates[index])
    return None, None


def _apply_trend_rules(result: dict, trend: dict, keywords: list):
    """Ensure user-provided trend keywords appear in the copy and tags."""
    usable_keywords = [str(keyword).strip() for keyword in keywords if str(keyword).strip()]
    if not usable_keywords:
        usable_keywords = [trend["trend"]]

    text = f"{result.get('title', '')} {result.get('description', '')}".casefold()
    if not any(keyword.casefold() in text for keyword in usable_keywords):
        result["title"] = f"{usable_keywords[0].title()}: {result.get('title', '')}"[:90]

    trend_tags = list(dict.fromkeys(_camel(keyword) for keyword in usable_keywords))
    trend_tags = [tag for tag in trend_tags if tag][:3]
    existing_tags = [
        tag for tag in result.get("tags", [])
        if tag not in trend_tags
    ]
    result["tags"] = (trend_tags + existing_tags)[:5]

    existing_board_keywords = result.get("board_keywords", [])
    if not isinstance(existing_board_keywords, list):
        existing_board_keywords = []
    result["board_keywords"] = list(
        dict.fromkeys(usable_keywords + existing_board_keywords)
    )[:4]


def call_trend_cmo(account_key: str, boards: dict = None, trends: dict = None):
    # Lazy import prevents the CMO's regular path from forming an import cycle.
    from mastermind import node_cmo as n
    from tools.firebase_boards import format_boards_for_prompt, format_trends_for_prompt

    trend, row = _pick_trend_and_row(account_key)
    if not trend:
        return None

    keyword_pool = trend["keywords"] or [trend["trend"]]
    keywords = random.sample(keyword_pool, min(3, len(keyword_pool)))
    style_key = str(row.get("style_key", "")).strip() or (
        f"{_camel(trend['trend'])}Style"
    )
    t2i_base = str(row.get("t2i_base", "")).strip()
    ratio = n._pick_ratio()
    niche_affinity = [
        value.strip()
        for value in str(row.get("niche_affinity", "")).split(",")
        if value.strip()
    ]
    niche = random.choice(niche_affinity) if niche_affinity else "home"
    row["tags"] = ",".join(_camel(keyword) for keyword in keywords)

    prompt = n._build_forced_style_prompt(
        n._ACCOUNT_PROFILES[account_key],
        style_key,
        ratio,
        t2i_base,
        sheet_row=row,
        boards_formatted=format_boards_for_prompt(boards or {}),
        trends_formatted=format_trends_for_prompt(trends or {}),
    ) + f"""

TREND MODE (IMPORTANT — overrides the account's default mood)
Active trend: {trend['trend']}
Trend keywords for this pin: {', '.join(keywords)}
- Follow the trend's mood and theme even if the account profile says otherwise.
- Title and description MUST naturally use at least one trend keyword.
- Tags MUST include CamelCase versions of these keywords: {', '.join(_camel(k) for k in keywords)}
- Keep the JSON format exactly as specified above."""

    result = None
    for provider, generate in (
        ("Gemini", n._call_gemini_sync),
        ("Groq", n._call_groq_sync),
    ):
        try:
            result = n._extract_json(generate(prompt))
            n._validate(result, account_key)
            logger.info("   [%s] Trend copy generated via %s", account_key, provider)
            break
        except Exception as error:
            logger.warning(
                "   [%s] Trend %s failed: %s: %s",
                account_key,
                provider,
                type(error).__name__,
                error,
            )
            result = None

    if result is None:
        label = str(row.get("label", "")).strip() or trend["trend"]
        description = (
            str(row.get("description", "")).strip()
            or f"{trend['trend']} aesthetic."
        )
        result = {
            "pin_type": "VIRAL_PIN",
            "strategy": f"Trend Direct (no LLM) — {trend['trend']}",
            "vibe": description[:80],
            "title": f"{keywords[0].title()} Ideas You'll Want To Save"[:90],
            "description": description[:380],
            "tags": [],
            "alt_text": f"{label} — {keywords[0]} aesthetic photography"[:200],
            "visual_prompt": t2i_base.rstrip(", ")
            + ("" if "4K ultra HD" in t2i_base else _TAIL),
        }

    result["pin_type"] = "VIRAL_PIN"
    result["visual_style"] = style_key
    result["ratio"] = result.get("ratio", ratio)
    result["niche"] = niche
    result["trend"] = trend["trend"]
    _apply_trend_rules(result, trend, keywords)
    return result
