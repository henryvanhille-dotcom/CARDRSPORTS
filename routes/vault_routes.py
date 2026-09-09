from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from vault import (
    add_vault_card,
    analyze_vault_card,
    delete_vault_card,
    get_vault_card,
    get_vault_cards,
    get_vault_summary,
    toggle_favorite,
    update_vault_card,
)


router = APIRouter()

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))


@router.get("/vault", response_class=HTMLResponse)
async def vault_page() -> RedirectResponse:
    return RedirectResponse(url="/?view=vault", status_code=303)


@router.get("/api/vault")
async def api_get_vault(
    search: str = "",
    favorite_only: bool = False,
):
    cards = get_vault_cards(
        search=search,
        favorite_only=favorite_only,
    )

    summary = get_vault_summary()

    return {
        "success": True,
        "cards": cards,
        "summary": summary,
    }


@router.get("/api/vault/{card_id}")
async def api_get_vault_card(card_id: int):
    card = get_vault_card(card_id)

    if card is None:
        raise HTTPException(
            status_code=404,
            detail="Vault card not found.",
        )

    return {
        "success": True,
        "card": card,
    }


@router.post("/api/vault")
async def api_add_vault_card(
    data: Dict[str, Any],
):
    try:
        card = add_vault_card(
            player=data.get("player", ""),
            year=data.get("year"),
            set_name=data.get("set_name", ""),
            card_type=data.get("card_type", ""),
            card_number=data.get("card_number", ""),
            parallel=data.get("parallel", ""),
            grade=data.get("grade", ""),
            image_url=data.get("image_url"),
            purchase_price_cad=data.get(
                "purchase_price_cad"
            ),
            purchase_date=data.get(
                "purchase_date"
            ),
            estimated_value_cad=data.get(
                "estimated_value_cad"
            ),
            value_confidence=data.get(
                "value_confidence"
            ),
            notes=data.get("notes", ""),
            favorite=data.get("favorite", False),
        )

        return {
            "success": True,
            "card": card,
        }

    except ValueError as error:
        raise HTTPException(
            status_code=422,
            detail=str(error),
        )


@router.put("/api/vault/{card_id}")
async def api_update_vault_card(
    card_id: int,
    data: Dict[str, Any],
):
    try:
        card = update_vault_card(
            card_id,
            **data,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=422,
            detail=str(error),
        )

    if card is None:
        raise HTTPException(
            status_code=404,
            detail="Vault card not found.",
        )

    return {
        "success": True,
        "card": card,
    }


@router.post("/api/vault/{card_id}/favorite")
async def api_toggle_favorite(
    card_id: int,
):
    card = toggle_favorite(card_id)

    if card is None:
        raise HTTPException(
            status_code=404,
            detail="Vault card not found.",
        )

    return {
        "success": True,
        "card": card,
    }


@router.post("/api/vault/{card_id}/analyze")
async def api_analyze_vault_card(card_id: int):
    result = analyze_vault_card(card_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail="Vault card not found.",
        )
    return {
        "success": True,
        **result,
    }


@router.delete("/api/vault/{card_id}")
async def api_delete_vault_card(
    card_id: int,
):
    deleted = delete_vault_card(card_id)

    if not deleted:
        raise HTTPException(
            status_code=404,
            detail="Vault card not found.",
        )

    return {
        "success": True,
    }
