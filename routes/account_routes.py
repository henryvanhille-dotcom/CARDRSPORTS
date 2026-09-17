"""Account, private-workspace, sharing, and collection-import endpoints."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from fastapi import APIRouter, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from accounts import (
    UPLOAD_FILENAME_PATTERN,
    claim_uploaded_files,
    clear_session_cookie,
    consume_import_preview,
    current_user,
    get_public_profile,
    guest_mode_enabled,
    list_notifications,
    register_account,
    remove_uploaded_files,
    require_user,
    revoke_session,
    save_import_preview,
    set_session_cookie,
    update_profile,
    workspace_context,
    authenticate,
)
from collection_import import CollectionImportError, parse_collection_csv, vault_create_payloads
from database import DATABASE, get_connection
from vault import add_vault_card, get_vault_cards
from watchlist import get_watchlist_cards
from market_data import SalesRepository
from portfolio_insights import build_cardr_pulse


router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))
MAX_CONFIRMED_IMPORT_CARDS = 10_000
BASE_DIR = Path(__file__).resolve().parents[1]
UPLOADS_DIR = Path(os.getenv("CARDR_DATA_DIR", str(BASE_DIR))).expanduser() / "uploads"


def _guest_upload_filenames(connection) -> list[str]:
    """Find only real, local guest photo files to move with an explicit claim."""

    rows = connection.execute(
        "SELECT image_url FROM vault_cards WHERE owner_id IS NULL AND image_url IS NOT NULL"
    ).fetchall()
    filenames = []
    for row in rows:
        image_url = str(row["image_url"] or "")
        if not image_url.startswith("/uploads/"):
            continue
        filename = image_url[len("/uploads/") :]
        if (
            UPLOAD_FILENAME_PATTERN.fullmatch(filename)
            and (UPLOADS_DIR / filename).is_file()
        ):
            filenames.append(filename)
    return list(dict.fromkeys(filenames))


def _account_payload(request: Request) -> Dict[str, Any]:
    user = current_user(request)
    return {
        "success": True,
        "authenticated": user is not None,
        "guest_mode": user is None and guest_mode_enabled(request),
        "user": user,
        "workspace_note": (
            "This local preview uses a single-device guest workspace. Create an account before publishing so your collection can sync privately."
            if user is None and guest_mode_enabled(request)
            else "Your collection is scoped to your signed-in Cardr account."
            if user is not None
            else "Sign in to open your private Cardr workspace."
        ),
    }


@router.get("/api/account")
async def account_status(request: Request) -> Dict[str, Any]:
    return _account_payload(request)


@router.post("/api/account/register")
async def account_register(data: Dict[str, Any], request: Request, response: Response) -> Dict[str, Any]:
    try:
        user, token = register_account(
            data.get("email"), data.get("password"), data.get("display_name")
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error))
    set_session_cookie(response, token, request)
    return {"success": True, "user": user, "message": "Your private Cardr workspace is ready."}


@router.post("/api/account/login")
async def account_login(data: Dict[str, Any], request: Request, response: Response) -> Dict[str, Any]:
    try:
        user, token = authenticate(data.get("email"), data.get("password"))
    except ValueError as error:
        raise HTTPException(status_code=401, detail=str(error))
    set_session_cookie(response, token, request)
    return {"success": True, "user": user, "message": "Welcome back to Cardr."}


@router.post("/api/account/logout")
async def account_logout(request: Request, response: Response) -> Dict[str, Any]:
    revoke_session(request.cookies.get("cardr_session"))
    clear_session_cookie(response, request)
    return {"success": True}


@router.delete("/api/account")
async def delete_account(data: Dict[str, Any], request: Request, response: Response) -> Dict[str, Any]:
    """Delete an account only after an explicit, deliberate confirmation.

    This endpoint is never called automatically.  It removes account-scoped
    collection data and local profile content; shared market evidence remains
    intact because it does not belong to an individual collector.
    """

    user = require_user(request)
    if data.get("confirmation") != "DELETE":
        raise HTTPException(status_code=422, detail='Type "DELETE" to permanently delete this account.')
    uploaded_filenames = remove_uploaded_files(user["id"])
    with get_connection() as connection:
        # SQLite foreign-key enforcement can vary in legacy local databases,
        # so these explicit deletes keep the privacy action reliable.
        for table in (
            "vault_cards",
            "watchlist_cards",
            "account_portfolio_snapshots",
            "collection_import_previews",
            "account_notifications",
            "account_alert_rules",
            "account_alert_delivery_state",
            "community_posts",
            "user_sessions",
        ):
            column = "user_id" if table == "user_sessions" else "owner_id"
            connection.execute("DELETE FROM {} WHERE {} = ?".format(table, column), (user["id"],))
        connection.execute("DELETE FROM users WHERE id = ?", (user["id"],))
        connection.commit()
    for filename in uploaded_filenames:
        image_path = UPLOADS_DIR / filename
        try:
            if image_path.is_file():
                image_path.unlink()
        except OSError:
            # The ownership record is already gone, so an inaccessible orphan
            # is safer than failing a completed account-deletion request.
            continue
    clear_session_cookie(response, request)
    return {"success": True, "message": "Your Cardr account and private workspace were deleted."}


@router.put("/api/account/profile")
async def account_profile(data: Dict[str, Any], request: Request) -> Dict[str, Any]:
    user = require_user(request)
    try:
        updated = update_profile(user["id"], data)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error))
    return {"success": True, "user": updated}


@router.post("/api/account/claim-local-workspace")
async def claim_local_workspace(request: Request) -> Dict[str, Any]:
    """Move only the local guest records into the signed-in account on request.

    This is intentionally explicit: existing local cards never become public,
    and a hosted app has guest mode disabled by default.
    """

    user = require_user(request)
    if not guest_mode_enabled(request):
        raise HTTPException(status_code=403, detail="Local workspace claiming is not available on this host.")
    with get_connection() as connection:
        guest_uploads = _guest_upload_filenames(connection)
        vault_count = connection.execute(
            "SELECT COUNT(*) FROM vault_cards WHERE owner_id IS NULL"
        ).fetchone()[0]
        watchlist_count = connection.execute(
            "SELECT COUNT(*) FROM watchlist_cards WHERE owner_id IS NULL"
        ).fetchone()[0]
        connection.execute("UPDATE vault_cards SET owner_id = ? WHERE owner_id IS NULL", (user["id"],))
        # Existing guest keys remain globally unique, and an account's newly
        # created watchlist rows use an owner-salted key.  Updating ownership
        # does not reveal or duplicate any private data.
        connection.execute("UPDATE watchlist_cards SET owner_id = ? WHERE owner_id IS NULL", (user["id"],))
        connection.commit()
    claim_uploaded_files(user["id"], guest_uploads)
    return {
        "success": True,
        "claimed": {"vault_cards": int(vault_count), "watchlist_cards": int(watchlist_count)},
        "message": "Local collection records are now inside your private account workspace.",
    }


@router.get("/api/account/export")
async def account_export(request: Request) -> Dict[str, Any]:
    """Portable account export.  It deliberately excludes password/session data."""

    user = require_user(request)
    return {
        "success": True,
        "export_version": 1,
        "profile": user,
        "vault": get_vault_cards(owner_id=user["id"]),
        "watchlist": get_watchlist_cards(owner_id=user["id"]),
        "notice": "This export contains your private collection data. Store it securely.",
    }


@router.get("/api/account/notifications")
async def account_notifications(request: Request, limit: int = 30) -> Dict[str, Any]:
    user = require_user(request)
    try:
        notifications = list_notifications(user["id"], limit=limit)
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail="Notification limit is invalid.") from error
    return {"success": True, "notifications": notifications}


@router.get("/api/account/weekly-recap")
async def weekly_recap(request: Request) -> Dict[str, Any]:
    """Build an account-private recap without pretending an email was sent."""

    user = require_user(request)
    cards = get_vault_cards(owner_id=user["id"])
    watchlist = get_watchlist_cards(owner_id=user["id"])
    sales = SalesRepository(BASE_DIR, db_path=DATABASE).all_observed_sales()
    pulse = build_cardr_pulse(cards, sales)
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    notifications = []
    for item in list_notifications(user["id"], limit=100):
        try:
            created = datetime.fromisoformat(str(item["created_at"]).replace("Z", "+00:00"))
        except ValueError:
            continue
        if created >= cutoff:
            notifications.append(item)
    return {
        "success": True,
        "period": {"days": 7, "from": cutoff.date().isoformat(), "to": datetime.now(timezone.utc).date().isoformat()},
        "vault_card_count": len(cards),
        "watchlist_card_count": len(watchlist),
        "new_in_app_alerts": notifications,
        "pulse": pulse,
        "delivery": {
            "status": "in_app_preview",
            "message": "This recap is ready inside Cardr. Email and push delivery are not connected, so nothing has been sent externally.",
        },
    }


@router.post("/api/imports/collection/preview")
async def collection_import_preview(
    request: Request,
    file: UploadFile = File(...),
) -> Dict[str, Any]:
    """Create a server-owned preview; writing cards requires a second request."""

    user = require_user(request)
    try:
        content = await file.read(5 * 1024 * 1024 + 1)
        preview = parse_collection_csv(content, filename=file.filename or "collection.csv")
        estimated_card_count = sum(int(record.get("quantity") or 1) for record in preview["records"])
        if estimated_card_count > MAX_CONFIRMED_IMPORT_CARDS:
            raise ValueError(
                "This file would create {:,} cards. Split it into imports of {:,} cards or fewer.".format(
                    estimated_card_count, MAX_CONFIRMED_IMPORT_CARDS
                )
            )
        stored = save_import_preview(user["id"], preview["records"])
    except (CollectionImportError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error))
    return {
        "success": True,
        "import_id": stored["id"],
        "expires_at": stored["expires_at"],
        "summary": {**preview["summary"], "expanded_card_count": estimated_card_count},
        "rows": preview["rows"],
        "header_warnings": preview.get("header_warnings", []),
        "message": "Review the preview, then explicitly confirm before anything is added to your Vault.",
    }


@router.post("/api/imports/collection/{import_id}/confirm")
async def collection_import_confirm(import_id: str, request: Request) -> Dict[str, Any]:
    user = require_user(request)
    try:
        records = consume_import_preview(import_id, user["id"])
        payloads = vault_create_payloads(records)
        if len(payloads) > MAX_CONFIRMED_IMPORT_CARDS:
            raise ValueError("This import exceeds the confirmed-card limit.")
        cards = [add_vault_card(owner_id=user["id"], **payload) for payload in payloads]
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error))
    return {
        "success": True,
        "imported_count": len(cards),
        "cards": cards,
        "message": "Collection import complete. Values remain blank until you choose to analyze each card.",
    }


@router.get("/api/public/{handle}")
async def public_profile(handle: str) -> Dict[str, Any]:
    try:
        profile = get_public_profile(handle)
    except ValueError:
        profile = None
    if profile is None:
        raise HTTPException(status_code=404, detail="That public Cardr profile is unavailable.")
    cards = get_vault_cards(owner_id=profile["id"])
    public_cards = [
        {
            "player": card.get("player"),
            "year": card.get("year"),
            "set_name": card.get("set_name"),
            "card_type": card.get("card_type"),
            "card_number": card.get("card_number"),
            "parallel": card.get("parallel"),
            "grade": card.get("grade"),
            "estimated_value_cad": card.get("estimated_value_cad"),
            "value_confidence": card.get("value_confidence"),
            "prospectr_status": card.get("prospectr_status"),
        }
        for card in cards
    ]
    return {
        "success": True,
        "profile": {key: value for key, value in profile.items() if key != "id"},
        "cards": public_cards,
        "privacy_note": "Only profile details and card identity/value fields chosen for the public profile are shown. Purchase prices, private notes, and uploaded images are never shared.",
    }


@router.get("/u/{handle}", response_class=HTMLResponse)
async def public_profile_page(request: Request, handle: str) -> HTMLResponse:
    return templates.TemplateResponse("public_profile.html", {"request": request, "handle": handle})
