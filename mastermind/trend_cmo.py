"""Trend-aware CMO strategy; None means the unchanged regular strategy should run."""
import logging
import random
import re
import requests

from sheets import load_prompt_tracker, save_prompt_tracker
from sheets.trends import get_active_trends, get_trend_prompts, update_trend_board_id

logger = logging.getLogger(__name__)
_TAIL = ", 4K ultra HD, photorealistic, highly detailed, award-winning photography"

MAKE_BOARD_WEBHOOK_URL = "https://hook.eu1.make.com/cujz1avg4cx7ld2cve7qnt31bticeiqy"

def _camel(keyword: str) -> str:
    words = re.sub(r"[^A-Za-z0-9 ]", " ", keyword)
    return "".join(word.capitalize() for word in words.split())

def _ensure_board_exists(trend_name: str, keywords: list) -> str:
    from mastermind import node_cmo as n
    
    # 1. AI se catchy Board Name aur Description likhwao
    prompt = f"""You are a Pinterest SEO expert. 
    Create a viral Pinterest board name and description for the trend: "{trend_name}".
    Keywords to include: {', '.join(keywords)}
    Output ONLY a JSON object:
    {{"board_name": "...", "board_description": "..."}}"""
    
    try:
        raw = n._call_gemini_sync(prompt)
        data = n._extract_json(raw)
        board_name = data.get("board_name", f"{trend_name.title()} Aesthetic")
        board_desc = data.get("board_description", f"Best {trend_name} ideas and inspiration.")
    except Exception as e:
        logger.warning(f"AI board generation failed, using defaults. Error: {e}")
        board_name = f"{trend_name.title()} Aesthetic"
        board_desc = f"Discover the best {trend_name} aesthetics, ideas, and inspiration."

    # 2. Make.com Webhook ko data bhejo
    payload = {
        "trend_id": trend_name,
        "board_name": board_name,
        "board_description": board_desc
    }
    logger.info(f"🚀 Creating new Pinterest board for '{trend_name}' via Make.com...")
    
    try:
        res = requests.post(MAKE_BOARD_WEBHOOK_URL, json=payload, timeout=20)
        if res.status_code == 200:
            res_data = res.json() 
            new_board_id = str(res_data.get("board_id", "")).strip().replace('"', '')
            if new_board_id:
                logger.info(f"✅ Board created successfully! ID: {new_board_id}")
                # 3. Sheet me update kar do
                update_trend_board_id(trend_name, new_board_id)
                return new_board_id
    except Exception as e:
        logger.error(f"❌ Failed to create board via Make.com: {e}")
    
    return ""

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
    
    # ---- SMART BOARD CREATOR LOGIC ----
    board_id = str(trend.get("board_id", "")).strip()
    if not board_id:
        logger.info(f"⚠️ No Board ID found for '{trend['trend']}'. Starting auto-creation...")
        board_id = _ensure_board_exists(trend['trend'], keywords)
        trend['board_id'] = board_id  # Update local dict so loop can use it
    # -----------------------------------
    
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
    result["selected_board_id"] = trend.get("board_id", "")
    _apply_trend_rules(result, trend, keywords)
    return result
