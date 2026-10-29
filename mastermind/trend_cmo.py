"""Trend-aware CMO strategy; None means the unchanged regular strategy should run."""
import logging
import os
import random
import re
import requests

from sheets import load_prompt_tracker, save_prompt_tracker
from sheets.trends import get_active_trends, get_trend_prompts, update_trend_board_id

logger = logging.getLogger(__name__)
_TAIL = ", 4K ultra HD, photorealistic, highly detailed, award-winning photography"

# Webhook URLs ab environment se aate hain (code me secret mat rakho).
#   MAKE_BOARD_WEBHOOK_URL             -> default webhook
#   MAKE_BOARD_WEBHOOK_URL_ACCOUNT_1   -> (optional) account_1 ka alag Pinterest account
#   MAKE_BOARD_WEBHOOK_URL_ACCOUNT_2   -> (optional) account_2 ka alag Pinterest account
MAKE_BOARD_WEBHOOK_URL = os.environ.get("MAKE_BOARD_WEBHOOK_URL", "")
_WEBHOOK_TIMEOUT = 60  # Make scenario slow ho sakta hai; 20s kam tha


def _webhook_for(account_key: str) -> str:
    return (
        os.environ.get(f"MAKE_BOARD_WEBHOOK_URL_{account_key.upper()}")
        or MAKE_BOARD_WEBHOOK_URL
    )


def _camel(keyword: str) -> str:
    words = re.sub(r"[^A-Za-z0-9 ]", " ", keyword)
    return "".join(word.capitalize() for word in words.split())


def _ensure_board_exists(trend_name: str, keywords: list, account_key: str = "") -> str:
    """Pinterest board banata hai (Make.com se). Fail hone par "" return karta hai."""
    from mastermind import node_cmo as n

    webhook = _webhook_for(account_key)
    if not webhook:
        logger.error("❌ MAKE_BOARD_WEBHOOK_URL env variable set nahi hai — board nahi ban sakta.")
        return ""

    # 1. AI se catchy Board Name aur Description
    prompt = f"""You are a Pinterest SEO expert.
    Create a viral Pinterest board name and description for the trend: "{trend_name}".
    Keywords to include: {', '.join(keywords)}
    STRICT RULES:
    - board_name MUST be UNDER 45 characters.
    - board_description MUST be UNDER 300 characters.
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

    # Make.com Error 400 prevention
    board_name = str(board_name)[:45].strip()
    board_desc = str(board_desc)[:300].strip()

    payload = {
        "trend_id": trend_name,
        "account": account_key,
        "board_name": board_name,
        "board_description": board_desc,
    }
    logger.info(
        f"🚀 Creating new Pinterest board for '{trend_name}' via Make.com "
        f"(account={account_key or 'default'}, name length={len(board_name)})"
    )

    try:
        res = requests.post(webhook, json=payload, timeout=_WEBHOOK_TIMEOUT)
        if res.status_code == 200:
            try:
                res_data = res.json()
            except ValueError:
                res_data = {}
            new_board_id = str(res_data.get("board_id", "")).strip().replace('"', "")
            if new_board_id:
                logger.info(f"✅ Board created successfully! ID: {new_board_id}")
                try:
                    update_trend_board_id(trend_name, new_board_id)
                except Exception as e:
                    # Board ban chuka hai — ID is run me use hoga, par sheet save fail hua
                    logger.error(
                        f"❌ Board ban gaya (ID {new_board_id}) par Sheet me save fail: {e}. "
                        f"Is ID ko Trends sheet me manually daal do warna duplicate boards bante rahenge."
                    )
                return new_board_id
            logger.error(f"❌ Make returned 200 but no board_id. Response: {res.text[:200]}")
        elif res.status_code == 202:
            logger.error("❌ Make.com returned 202 Accepted. Scenario OFF ho sakta hai, ya "
                         "'Webhook response' module missing hai.")
        else:
            logger.error(f"❌ Make.com Error: Status {res.status_code} - {res.text[:200]}")
    except Exception as e:
        logger.error(f"❌ Failed to create board via Make.com: {type(e).__name__}: {e}")

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


def _pick_relevant_keywords(keyword_pool: list, row: dict, limit: int = 3) -> list:
    """
    Sirf wahi trend keywords lo jo is prompt row (image) se match karte hain.
    Isse title/description hamesha usi cheez ke baare me hoga jo image me hai.
    Koi match nahi mila -> [] (copy sirf image ke hisaab se likhi jayegi).
    """
    row_text = " ".join(
        str(row.get(k, "")) for k in
        ("style_key", "label", "description", "t2i_base", "niche_affinity")
    ).casefold()
    row_text = re.sub(r"[^a-z0-9 ]", " ", row_text.replace("_", " "))
    row_words = set(row_text.split())

    def _norm(word: str) -> str:
        return word[:-1] if len(word) > 3 and word.endswith("s") else word

    row_norm = {_norm(w) for w in row_words}

    scored = []
    for keyword in keyword_pool:
        tokens = [
            _norm(t) for t in re.sub(r"[^a-z0-9 ]", " ", str(keyword).casefold()).split()
            if len(t) > 2
        ]
        if not tokens:
            continue
        hits = sum(1 for t in tokens if t in row_norm)
        # keyword tabhi lo jab uske saare (ya kam se kam aadhe) words image se match karein
        if hits and hits >= max(1, (len(tokens) + 1) // 2):
            scored.append((hits / len(tokens), keyword))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [kw for _, kw in scored[:limit]]


def call_trend_cmo(account_key: str, boards: dict = None, trends: dict = None):
    # Lazy import prevents the CMO's regular path from forming an import cycle.
    from mastermind import node_cmo as n

    trend, row = _pick_trend_and_row(account_key)
    if not trend:
        return None

    keyword_pool = trend["keywords"] or [trend["trend"]]
    # Board ka naam banane ke liye poora trend pool chalega (random)
    board_keywords = random.sample(keyword_pool, min(3, len(keyword_pool)))
    # Pin ke title/desc/tags ke liye sirf image se match karne wale keywords
    keywords = _pick_relevant_keywords(keyword_pool, row)
    logger.info(
        f"   [{account_key}] Trend keywords matched to image: {keywords or 'NONE (copy follows image only)'}"
    )

    # ---- SMART BOARD CREATOR LOGIC ----
    board_id = str(trend.get("board_id", "")).strip()
    if not board_id:
        logger.info(f"⚠️ No Board ID found for '{trend['trend']}'. Starting auto-creation...")
        board_id = _ensure_board_exists(trend["trend"], board_keywords, account_key)
        trend["board_id"] = board_id

    # FIX: board nahi mila/bana to trend pin mat banao — regular flow chale
    # (warna pin kisi random board me chala jata tha).
    if not board_id:
        logger.error(
            f"❌ [{account_key}] Trend '{trend['trend']}' ke liye board ID nahi mila — "
            f"regular strategy par fallback."
        )
        return None
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
    if keywords:
        row["tags"] = ",".join(_camel(keyword) for keyword in keywords)

    # FIX: boards_formatted / trends_formatted khali — trend mode me LLM board
    # select na kare (board already trend ka fixed hai).
    prompt = n._build_forced_style_prompt(
        n._ACCOUNT_PROFILES[account_key],
        style_key,
        ratio,
        t2i_base,
        sheet_row=row,
        boards_formatted="",
        trends_formatted="",
    ) + f"""

TREND MODE
Active trend: {trend['trend']}
Trend keywords that match this image: {', '.join(keywords) if keywords else 'none — write only about what the image shows'}
- IMAGE-FIRST RULE: title, description, alt_text and tags MUST describe the exact scene in the T2I Base above.
- NEVER mention subjects that are not in the image (e.g. costumes, nails, makeup, recipes) unless the T2I Base contains them.
- Match the trend's seasonal mood, but the scene itself must stay exactly as in the T2I Base.
{('- Title and description should naturally use at least one of the matching keywords above.' + chr(10) + '- Tags MUST include CamelCase versions of: ' + ', '.join(_camel(k) for k in keywords)) if keywords else ''}
- Keep the JSON format exactly as specified above."""

    result = None
    for provider, generate in (
        ("Gemini", n._call_gemini_sync),
        ("Groq", n._call_groq_sync),
        ("GroqThinking", n._call_groq_thinking_sync),
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
        lead_kw = keywords[0] if keywords else label
        result = {
            "pin_type": "VIRAL_PIN",
            "strategy": f"Trend Direct (no LLM) — {trend['trend']}",
            "vibe": description[:80],
            "title": f"{lead_kw.title()} Ideas You'll Want To Save"[:90],
            "description": description[:380],
            "tags": [],
            "alt_text": f"{label} — {lead_kw} aesthetic photography"[:200],
            "visual_prompt": t2i_base.rstrip(", ")
            + ("" if "4K ultra HD" in t2i_base else _TAIL),
        }

    result["pin_type"] = "VIRAL_PIN"
    result["visual_style"] = style_key
    result["ratio"] = result.get("ratio", ratio)
    result["niche"] = niche
    result["trend"] = trend["trend"]

    # FIX: LLM ka board data hata do, sirf trend ka board rakho aur lock karo
    for stale in ("selected_board_name", "selected_board_niche",
                  "board_selection_reason", "primary_keyword", "active_keywords"):
        result.pop(stale, None)
    result["selected_board_id"] = board_id
    result["selected_board_name"] = trend["trend"]
    result["selected_board_niche"] = "trend"
    result["board_locked"] = True   # node_board_selector isko dekh kar Groq skip karega

    # Keywords tabhi force karo jab wo image se match karte hon
    if keywords:
        _apply_trend_rules(result, trend, keywords)
    return result
