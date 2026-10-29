import httpx
import logging
from config import PINTEREST_ACCOUNTS

logger = logging.getLogger(__name__)

async def post_to_pinterest(
    image_url: str,
    title: str,
    description: str,
    link: str,
    tags: list,
    niche: str = "default",
    target_account: str = None,
    alt_text: str = "",
    blog_url: str = "",
    board_id: str = "",    # Firebase dynamic board_id — overrides niche-based lookup
    board_name: str = "",  # human-friendly board name for logging
    destination_url: str = "",  # V4 Product Router — explicit product destination; wins over blog_url/link
) -> bool:

    # Strictly target account find karo
    if target_account:
        account = next((a for a in PINTEREST_ACCOUNTS if a["name"] == target_account), PINTEREST_ACCOUNTS[0])
    else:
        account = next((a for a in PINTEREST_ACCOUNTS if a["niche"] == niche), PINTEREST_ACCOUNTS[0])

    # Firebase dynamic board_id overrides niche-based static config
    if not board_id:
        board_id = account["boards"].get(niche, account["boards"]["default"])

    # Safety: ensure we have an explicit board_id before posting
    if not board_id:
        logger.error("[Webhook] board_id is None — skipping pin or posting without board")
        return False

    hashtags = " ".join([f"#{t.strip()}" for t in tags])
    caption  = f"{description}\n\n{hashtags}"

    # Destination priority (V4): a Product Router decision is an explicit,
    # per-pin product destination, so it wins; then the blog URL; then the
    # legacy link param. Empty everywhere = pin posts clean (no destination).
    final_link = destination_url if destination_url else (blog_url if blog_url else link)

    payload = {
        "image_url": image_url,
        "title":     title[:100],
        "caption":   caption[:500],
        "link":      final_link,
        "board_id":  board_id,
        "board_name": board_name,
        "alt_text":  alt_text,
    }

    _link_kind = "product" if destination_url else ("blog" if blog_url else ("affiliate" if link else "none"))
    logger.info(f"📌 [{account['name']}] Niche: {niche} → Board: {board_name or board_id} (id={board_id}) | Link: {_link_kind}")

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(account["webhook_url"], json=payload)

        if r.status_code == 200:
            logger.info(f"✅ [{account['name']}] Posted: {title[:50]}")
            return True
        else:
            logger.error(f"❌ [{account['name']}] Error {r.status_code}: {r.text}")
            return False

    except Exception as e:
        logger.error(f"❌ [{account['name']}] Webhook failed: {e}")
        return False
