"""FastAPI endpoints for the V4 Product Router's digital products.

Backs the Pins app "Products" screen so the owner never has to open
Google Sheets by hand: products live in the `Products` sheet tab
(the router's source of truth) and these endpoints read/append it.

  GET  /api/kits          list all products from the sheet
  POST /api/kits          add a product (one sheet row; router picks it up)
  POST /api/kits/toggle   activate/deactivate a product by product_id

All routes sit behind the existing API-key middleware like everything else.
"""
import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from mastermind.product_router import _slugify, invalidate_products_cache
from sheets.base import _open_worksheet, _throttled_read, _throttled_write

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/kits", tags=["kits"])
PRODUCTS_TAB = "Products"
# Column order in the Products tab (see sheets/setup.py REQUIRED_SHEETS)
COLUMNS = ["product_id", "name", "url", "price", "description", "keywords", "cta", "active"]
ACTIVE_COL_INDEX = 8  # 1-based column of `active`


class KitIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=8, max_length=500)
    price: float | None = Field(default=None, ge=0)
    description: str = Field(default="", max_length=2000)
    keywords: str = Field(default="", max_length=1000)
    cta: str = Field(default="", max_length=300)


class KitToggle(BaseModel):
    product_id: str = Field(min_length=1, max_length=120)
    active: bool


def _list_kits_sync() -> list[dict]:
    ws = _open_worksheet(PRODUCTS_TAB)
    records = _throttled_read(lambda: ws.get_all_records())
    kits = []
    for row in records or []:
        kits.append({
            "product_id": str(row.get("product_id", "")),
            "name": str(row.get("name", "")),
            "url": str(row.get("url", "")),
            "price": row.get("price", ""),
            "description": str(row.get("description", "")),
            "keywords": str(row.get("keywords", "")),
            "cta": str(row.get("cta", "")),
            "active": str(row.get("active", "TRUE")).strip().lower() not in ("false", "0", "no", "off"),
        })
    return kits


def _append_kit_sync(kit: dict) -> None:
    ws = _open_worksheet(PRODUCTS_TAB)
    row = [kit[col] for col in COLUMNS]
    _throttled_write(lambda: ws.append_row(row))


def _set_active_sync(product_id: str, active: bool) -> bool:
    ws = _open_worksheet(PRODUCTS_TAB)
    records = _throttled_read(lambda: ws.get_all_records())
    for idx, row in enumerate(records or []):
        if str(row.get("product_id", "")).strip() == product_id:
            sheet_row = idx + 2  # header is row 1
            _throttled_write(lambda: ws.update_cell(sheet_row, ACTIVE_COL_INDEX, "TRUE" if active else "FALSE"))
            return True
    return False


@router.get("")
async def list_kits():
    try:
        kits = await asyncio.to_thread(_list_kits_sync)
    except Exception as error:
        raise HTTPException(503, "Products sheet is unavailable. Check Google Sheets configuration.") from error
    return {"kits": kits}


@router.post("")
async def add_kit(body: KitIn):
    url = body.url.strip()
    if not url.startswith("http"):
        raise HTTPException(400, "URL must start with http(s)://")
    product_id = _slugify(body.name)
    try:
        existing = await asyncio.to_thread(_list_kits_sync)
        if any(k["product_id"] == product_id for k in existing):
            raise HTTPException(409, f"A product with id '{product_id}' already exists.")
        if any(k["url"].strip() == url for k in existing):
            raise HTTPException(409, "That URL is already in the Products sheet.")
        kit = {
            "product_id": product_id,
            "name": body.name.strip(),
            "url": url,
            "price": body.price if body.price is not None else "",
            "description": body.description.strip(),
            "keywords": body.keywords.strip(),
            "cta": body.cta.strip(),
            "active": "TRUE",
        }
        await asyncio.to_thread(_append_kit_sync, kit)
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(503, "Could not save the product. Check Google Sheets configuration.") from error
    invalidate_products_cache()
    logger.info(f"🛍️ [Kits] Added product '{product_id}' via API")
    return {"ok": True, "product_id": product_id}


@router.post("/toggle")
async def toggle_kit(body: KitToggle):
    try:
        updated = await asyncio.to_thread(_set_active_sync, body.product_id.strip(), body.active)
    except Exception as error:
        raise HTTPException(503, "Could not update the product. Check Google Sheets configuration.") from error
    if not updated:
        raise HTTPException(404, "Product not found.")
    invalidate_products_cache()
    return {"ok": True, "product_id": body.product_id, "active": body.active}
