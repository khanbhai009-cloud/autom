"""
sheets/prompts_master.py — Prompts_Master sheet tab fetch with TTL cache.

Tab columns: style_key | account | label | description | t2i_base | niche_affinity | tags
Account values: account_1 | account_2
TTL: 30 minutes — new rows added to the sheet auto-picked up within 30 min.
"""
import time
import logging
from sheets.base import _open_worksheet, _throttled_write, _throttled_read

logger = logging.getLogger(__name__)

_cache: list | None = None
_cache_ts: float    = 0.0
_TTL: int           = 1800   # 30 minutes


def get_prompts_master() -> list:
    """
    Fetch all rows from Prompts_Master tab.
    TTL-cached (30 min) — new rows in the sheet are picked up automatically.
    Raises on connection failure — caller handles fallback.
    """
    global _cache, _cache_ts
    now = time.time()
    if _cache is not None and (now - _cache_ts) < _TTL:
        return _cache

    def _read():
        ws = _open_worksheet("Prompts_Master")
        return ws.get_all_records()

    records   = _throttled_read(_read)
    _cache    = records
    _cache_ts = now
    logger.info(f"✅ [Prompts_Master] {len(records)} prompts loaded (TTL refreshed).")
    return records


def invalidate_cache() -> None:
    """Force next call to re-fetch from Sheets."""
    global _cache, _cache_ts
    _cache    = None
    _cache_ts = 0.0


def _cell(value) -> str:
    """Sheets cell hamesha STRING hona chahiye. Vision models `tags` (aur
    kabhi kabhi doosre fields) ko JSON array me bhej dete hain jabki prompt
    unse comma-separated string maangta hai — list seedha append_row me
    jata tha toh Google Sheets API 400 deta tha:
    'Invalid values[383][6]: list_value {...}' (30 Oct 2026 production).
    List/tuple → ", ".join; None → ""; baaki → str()."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value)


def append_prompt_row(data: dict) -> None:
    """
    Vision Feeder se extract ki gayi DNA row ko Prompts_Master mein append karo.
    TTL cache invalidate karta hai taaki next fetch fresh rows laye.
    Columns: style_key | account | label | description | t2i_base | niche_affinity | tags
    """
    def _write():
        ws  = _open_worksheet("Prompts_Master")
        row = [
            _cell(data.get("style_key",      "")),
            _cell(data.get("account",        "account_1")),
            _cell(data.get("label",          "")),
            _cell(data.get("description",    "")),
            _cell(data.get("t2i_base",       "")),
            _cell(data.get("niche_affinity", "")),
            _cell(data.get("tags",           "")),
        ]
        ws.append_row(row)

    _throttled_write(_write)
    invalidate_cache()
    logger.info(f"✅ [Prompts_Master] Row appended: {data.get('style_key')}")
