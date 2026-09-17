"""HTTP endpoints for the private CARDr Watchlist."""

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from accounts import workspace_context

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
async def api_get_watchlist(request: Request, search: str = "", priority_only: bool = False) -> Dict[str, Any]:
    context = workspace_context(request)
    return {
        "success": True,
        "cards": get_watchlist_cards(search=search, priority_only=priority_only, owner_id=context.owner_id),
        "summary": get_watchlist_summary(owner_id=context.owner_id),
    }


@router.get("/api/watchlist/{watchlist_id}")
async def api_get_watchlist_card(watchlist_id: int, request: Request) -> Dict[str, Any]:
    context = workspace_context(request)
    card = get_watchlist_card(watchlist_id, owner_id=context.owner_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Watchlist card not found.")
    return {"success": True, "card": card}


@router.post("/api/watchlist")
async def api_add_watchlist_card(data: Dict[str, Any], request: Request) -> Dict[str, Any]:
    context = workspace_context(request)
    safe_values = {key: value for key, value in data.items() if key != "owner_id"}
    try:
        card = add_watchlist_card(owner_id=context.owner_id, **safe_values)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error))
    return {"success": True, "card": card}


@router.put("/api/watchlist/{watchlist_id}")
async def api_update_watchlist_card(watchlist_id: int, data: Dict[str, Any], request: Request) -> Dict[str, Any]:
    context = workspace_context(request)
    safe_updates = {key: value for key, value in data.items() if key != "owner_id"}
    try:
        card = update_watchlist_card(watchlist_id, owner_id=context.owner_id, **safe_updates)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error))
    if card is None:
        raise HTTPException(status_code=404, detail="Watchlist card not found.")
    return {"success": True, "card": card}


@router.delete("/api/watchlist/{watchlist_id}")
async def api_delete_watchlist_card(watchlist_id: int, request: Request) -> Dict[str, bool]:
    context = workspace_context(request)
    if not delete_watchlist_card(watchlist_id, owner_id=context.owner_id):
        raise HTTPException(status_code=404, detail="Watchlist card not found.")
    return {"success": True}
