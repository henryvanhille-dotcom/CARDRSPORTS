"""HTTP endpoints for the private CARDr Watchlist."""

from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from watchlist import (
    add_watchlist_card,
    delete_watchlist_card,
    get_watchlist_card,
    get_watchlist_cards,
    get_watchlist_summary,
    update_watchlist_card,
)


router = APIRouter()


@router.get("/api/watchlist")
async def api_get_watchlist(search: str = "", priority_only: bool = False) -> Dict[str, Any]:
    return {
        "success": True,
        "cards": get_watchlist_cards(search=search, priority_only=priority_only),
        "summary": get_watchlist_summary(),
    }


@router.get("/api/watchlist/{watchlist_id}")
async def api_get_watchlist_card(watchlist_id: int) -> Dict[str, Any]:
    card = get_watchlist_card(watchlist_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Watchlist card not found.")
    return {"success": True, "card": card}


@router.post("/api/watchlist")
async def api_add_watchlist_card(data: Dict[str, Any]) -> Dict[str, Any]:
    try:
        card = add_watchlist_card(**data)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error))
    return {"success": True, "card": card}


@router.put("/api/watchlist/{watchlist_id}")
async def api_update_watchlist_card(watchlist_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
    try:
        card = update_watchlist_card(watchlist_id, **data)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error))
    if card is None:
        raise HTTPException(status_code=404, detail="Watchlist card not found.")
    return {"success": True, "card": card}


@router.delete("/api/watchlist/{watchlist_id}")
async def api_delete_watchlist_card(watchlist_id: int) -> Dict[str, bool]:
    if not delete_watchlist_card(watchlist_id):
        raise HTTPException(status_code=404, detail="Watchlist card not found.")
    return {"success": True}
