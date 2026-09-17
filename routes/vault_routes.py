from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from accounts import get_uploaded_file, workspace_context
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


def _workspace_image_url(value: Any, context) -> Any:
    """Accept a Cardr upload only when it belongs to this private workspace."""

    if value in (None, ""):
        return value
    image_url = str(value)
    if not image_url.startswith("/uploads/"):
        return value
    filename = image_url[len("/uploads/") :]
    try:
        uploaded_file = get_uploaded_file(filename)
    except ValueError as error:
        raise ValueError("Choose a valid Cardr card photo.") from error

    if uploaded_file is None:
        if context.guest_mode:
            # Backward-compatible only for photos from the local guest
            # workspace that predate ownership tracking.
            return value
        raise ValueError("Choose a card photo uploaded to your Cardr workspace.")
    if uploaded_file.get("owner_id") != context.owner_id:
        raise ValueError("Choose a card photo uploaded to your Cardr workspace.")
    return value


@router.get("/vault", response_class=HTMLResponse)
async def vault_page() -> RedirectResponse:
    return RedirectResponse(url="/?view=vault", status_code=303)


@router.get("/api/vault")
async def api_get_vault(
    request: Request,
    search: str = "",
    favorite_only: bool = False,
):
    context = workspace_context(request)
    cards = get_vault_cards(
        search=search,
        favorite_only=favorite_only,
        owner_id=context.owner_id,
    )

    summary = get_vault_summary(owner_id=context.owner_id)

    return {
        "success": True,
        "cards": cards,
        "summary": summary,
    }


@router.get("/api/vault/{card_id}")
async def api_get_vault_card(card_id: int, request: Request):
    context = workspace_context(request)
    card = get_vault_card(card_id, owner_id=context.owner_id)

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
    request: Request,
):
    context = workspace_context(request)
    try:
        card = add_vault_card(
            player=data.get("player", ""),
            year=data.get("year"),
            set_name=data.get("set_name", ""),
            card_type=data.get("card_type", ""),
            card_number=data.get("card_number", ""),
            parallel=data.get("parallel", ""),
            grade=data.get("grade", ""),
            image_url=_workspace_image_url(data.get("image_url"), context),
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
            owner_id=context.owner_id,
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
    request: Request,
):
    context = workspace_context(request)
    safe_updates = {key: value for key, value in data.items() if key != "owner_id"}
    try:
        if "image_url" in safe_updates:
            safe_updates["image_url"] = _workspace_image_url(safe_updates["image_url"], context)
        card = update_vault_card(
            card_id,
            owner_id=context.owner_id,
            **safe_updates,
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
    request: Request,
):
    context = workspace_context(request)
    card = toggle_favorite(card_id, owner_id=context.owner_id)

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
async def api_analyze_vault_card(card_id: int, request: Request):
    context = workspace_context(request)
    result = analyze_vault_card(card_id, owner_id=context.owner_id)
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
    request: Request,
):
    context = workspace_context(request)
    deleted = delete_vault_card(card_id, owner_id=context.owner_id)

    if not deleted:
        raise HTTPException(
            status_code=404,
            detail="Vault card not found.",
        )

    return {
        "success": True,
    }
