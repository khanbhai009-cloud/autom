"""
sheets/setup.py — Auto-create all required Google Sheet tabs on app startup.

App restart hone par automatically:
  - Required tabs check karta hai
  - Jo missing ho → naya tab banata hai + headers inject karta hai
  - Jo already exist kare → touch nahi karta (idempotent)
"""
import logging
from config import GOOGLE_CREDS_JSON
from sheets.base import _get_spreadsheet

logger = logging.getLogger(__name__)

REQUIRED_SHEETS: dict[str, list] = {
    "Prompts_Master":  ["style_key", "account", "label", "description", "t2i_base", "niche_affinity", "tags"],
    "Style_Tracker":   ["account_1", "account_2"],
    "Prompt_Tracker":  ["tracker_key", "last_idx"],
    "Vision_Tracker":  ["date", "file_name", "style_key", "account", "status", "timestamp"],
    # Analytics tabs intentionally NOT required — analytics was removed
    # from the pipeline (owner, 30 Oct 2026); direction comes from Trends.
    # V4 Product Router — owner-managed product registry + decision log.
    # Owner workflow: list the product on Gumroad, then add ONE row here
    # (url + description/guide + keywords). The router reads this tab.
    "Products":           ["product_id", "name", "url", "price", "description", "keywords", "cta", "active"],
    "Product_Router_Log": ["timestamp", "account", "title", "board", "product_id", "destination_url", "confidence", "method", "reason"],
}

# Seed rows for a freshly created Products tab — the two live Gumroad kits,
# so the router works from minute one. The owner can edit/extend these rows
# in the sheet itself; the sheet is the source of truth from then on.
_PRODUCTS_SEED_ROWS: list[list] = [
    [
        "garden_kit", "Dream Garden Makeover Kit",
        "https://cozycanvasstudio.gumroad.com/l/nmkawd", 15,
        "A printable workbook for planning a dream garden makeover: layouts, planting zones, cottage-garden styling and step-by-step planning pages for gardens, backyards, balconies and porches.",
        "cottage garden, garden makeover, dream garden, garden design, backyard garden, balcony garden, garden, backyard, balcony, porch, patio, flower bed, planting, greenhouse, garden path, garden swing",
        "Love this garden look? Plan your own step-by-step with the Dream Garden Makeover Kit — link below 🌿",
        "TRUE",
    ],
    [
        "decor_kit", "Home Decor Styling Kit",
        "https://cozycanvasstudio.gumroad.com/l/ictsf", 15,
        "A printable workbook for styling cozy home interiors: room-by-room styling formulas, colour palettes, layout and finishing touches for bedrooms, living rooms and cozy corners.",
        "home decor, living room, bedroom decor, interior styling, cozy corner, reading nook, room makeover, bedroom, interior, decor, styling, boho, vanity, gallery wall, kitchen decor, dining room, apartment decor, bookshelf styling",
        "Love this look? Style it room-by-room with the Home Decor Styling Kit — link below 🏡",
        "TRUE",
    ],
]


def init_sheets() -> None:
    """
    App startup pe call karo — spreadsheet mein saari required tabs
    automatically bana deta hai + headers inject karta hai.
    GOOGLE_CREDS_JSON set nahi? → silently skip karta hai.
    """
    if not GOOGLE_CREDS_JSON:
        logger.warning("⚠️ [init_sheets] GOOGLE_CREDS_JSON not set — skipping auto-sheet creation.")
        return

    try:
        spreadsheet     = _get_spreadsheet()
        existing_titles = {ws.title for ws in spreadsheet.worksheets()}
        logger.info(f"📋 [init_sheets] Existing tabs: {existing_titles}")

        for sheet_name, headers in REQUIRED_SHEETS.items():
            if sheet_name not in existing_titles:
                ws = spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=len(headers))
                ws.append_row(headers)
                if sheet_name == "Style_Tracker":
                    ws.append_row([0, 0])
                if sheet_name == "Products":
                    for seed_row in _PRODUCTS_SEED_ROWS:
                        ws.append_row(seed_row)
                    logger.info("✅ [init_sheets] Products tab seeded with the 2 live Gumroad kits")
                logger.info(f"✅ [init_sheets] Created: '{sheet_name}' with headers {headers}")
            else:
                ws   = spreadsheet.worksheet(sheet_name)
                row1 = ws.row_values(1)
                if not row1:
                    ws.insert_row(headers, 1)
                    if sheet_name == "Style_Tracker":
                        ws.append_row([0, 0])
                    logger.info(f"✅ [init_sheets] Injected headers into empty tab: '{sheet_name}'")
                else:
                    logger.info(f"☑️  [init_sheets] Already set up: '{sheet_name}'")

        logger.info("🚀 [init_sheets] All required sheets verified/created successfully.")
    except Exception as e:
        logger.error(f"❌ [init_sheets] Failed — {type(e).__name__}: {e}")
