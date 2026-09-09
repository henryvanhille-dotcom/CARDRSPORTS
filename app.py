"""Cardr web application.

The app is intentionally conservative: it shows a valuation only when stored,
non-demo sale evidence supports it. Live marketplace ingestion is opt-in and
requires the local CARD_API_KEY configuration used by ebay.py.
"""

import os
import uuid
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlencode

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from database import DATABASE, get_connection, initialize_database, save_sales
from cardr_score import build_card_price_history, calculate_cardr_score
from market_data import SalesRepository
from market_pulse import build_market_pulse
from plans import public_plan_catalog
from portfolio_insights import (
    build_cardr_pulse,
    build_portfolio_composition,
    build_portfolio_overview,
)
from valuation import CardQuery, calculate_valuation, find_comps
from routes.vault_routes import router as vault_router
from routes.watchlist_routes import router as watchlist_router
from vault import get_vault_card, get_vault_cards, get_vault_summary
from watchlist import initialize_watchlist

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
# Keep application assets in the release directory while allowing database
# backed data and uploaded photos to live on a managed host's persistent disk.
DATA_DIR = Path(os.getenv("CARDR_DATA_DIR", str(BASE_DIR))).expanduser()
UPLOADS_DIR = DATA_DIR / "uploads"
STATIC_DIR = BASE_DIR / "static"
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def _image_extension(content: bytes) -> Optional[str]:
    """Identify the supported image formats from their file signatures."""
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return ".webp"
    return None

UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
repository = SalesRepository(BASE_DIR, db_path=DATABASE)

app = FastAPI(
    title="Cardr — Baseball Card Intelligence",
    version="1.0.0",
    description="Evidence-first card intelligence powered by Prospectr predictions.",
)

# Keep local development working while allowing the production hostname. A
# deploy can override this comma-separated list with CARDR_ALLOWED_HOSTS.
allowed_hosts = [
    host.strip()
    for host in os.getenv(
        "CARDR_ALLOWED_HOSTS",
        "cardrsports.com,www.cardrsports.com,*.onrender.com,localhost,127.0.0.1",
    ).split(",")
    if host.strip()
]
render_hostname = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
if render_hostname:
    allowed_hosts.append(render_hostname)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.include_router(vault_router)
app.include_router(watchlist_router)

@app.on_event("startup")
def prepare_database() -> None:
    """Create the existing SQLite tables before a sales refresh is requested."""
    initialize_database()
    initialize_watchlist()


def _card_from_payload(payload: Dict[str, Any], required: bool = True) -> CardQuery:
    card = CardQuery.from_mapping(payload)
    missing = []
    if not card.player:
        missing.append("player")
    if not card.year:
        missing.append("year")
    if not card.set_name:
        missing.append("set_name")
    if required and missing:
        raise HTTPException(
            status_code=422,
            detail="Missing required card fields: " + ", ".join(missing),
        )
    return card


def _boolean_value(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off", ""}:
            return False
    raise HTTPException(status_code=422, detail="{} must be true or false.".format(field))


def _optional_print_run(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        print_run = int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Print run must be a whole number.")
    if print_run < 1 or print_run > 1_000_000:
        raise HTTPException(status_code=422, detail="Print run must be between 1 and 1,000,000.")
    return print_run


def _optional_reference_value(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        reference = float(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Reference value must be a number.")
    if not math.isfinite(reference) or reference <= 0 or reference > 10_000_000:
        raise HTTPException(
            status_code=422,
            detail="Reference value must be greater than 0 and no more than $10,000,000 CAD.",
        )
    return round(reference, 2)


def _prediction_inputs(payload: Dict[str, Any]) -> Dict[str, Any]:
    one_of_one = _boolean_value(payload.get("one_of_one", False), "One-of-one flag")
    predictive_mode = _boolean_value(payload.get("predictive_mode", False), "Predictive mode")
    first_bowman_auto = _boolean_value(
        payload.get("first_bowman_auto", False),
        "First-Bowman autograph flag",
    )
    print_run = _optional_print_run(payload.get("print_run"))
    reference_value_cad = _optional_reference_value(payload.get("reference_value_cad"))
    return {
        "predictive_mode": predictive_mode or one_of_one or first_bowman_auto or print_run is not None or reference_value_cad is not None,
        "one_of_one": one_of_one,
        "first_bowman_auto": first_bowman_auto,
        "print_run": print_run,
        "reference_value_cad": reference_value_cad,
    }


def _valuation_response(card: CardQuery, prediction_inputs: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    source_summary = repository.source_summary()
    prediction_inputs = prediction_inputs or {}
    observed_sales = repository.all_observed_sales()
    valuation = calculate_valuation(
        card,
        observed_sales,
        **prediction_inputs,
    )
    return {
        "success": True,
        "card": card.as_dict(),
        "valuation": valuation,
        "score": calculate_cardr_score(
            card,
            observed_sales,
            print_run=prediction_inputs.get("print_run"),
            one_of_one=bool(prediction_inputs.get("one_of_one")),
        ),
        "history": build_card_price_history(card, observed_sales),
        "data_quality": source_summary,
        "prediction_inputs": prediction_inputs,
    }


def _record_portfolio_snapshot(summary: Dict[str, Any]) -> None:
    """Store today's real portfolio state without inventing a back-history."""
    captured_at = datetime.now(timezone.utc)
    snapshot_date = captured_at.date().isoformat()
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO portfolio_snapshots (
                snapshot_date, total_value_cad, total_invested_cad,
                profit_loss_cad, card_count, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_date) DO UPDATE SET
                total_value_cad = excluded.total_value_cad,
                total_invested_cad = excluded.total_invested_cad,
                profit_loss_cad = excluded.profit_loss_cad,
                card_count = excluded.card_count
            """,
            (
                snapshot_date,
                float(summary.get("total_estimated_value_cad") or 0),
                float(summary.get("total_purchase_price_cad") or 0),
                float(summary.get("total_profit_loss_cad") or 0),
                int(summary.get("card_count") or 0),
                captured_at.isoformat(),
            ),
        )
        connection.commit()


def _portfolio_history() -> list[Dict[str, Any]]:
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT snapshot_date, total_value_cad, total_invested_cad,
                   profit_loss_cad, card_count
            FROM portfolio_snapshots
            ORDER BY snapshot_date ASC, id ASC
            """
        ).fetchall()
    return [
        {
            "date": row["snapshot_date"],
            "total_value_cad": row["total_value_cad"],
            "total_invested_cad": row["total_invested_cad"],
            "profit_loss_cad": row["profit_loss_cad"],
            "card_count": row["card_count"],
        }
        for row in rows
    ]
@app.get("/app", response_class=HTMLResponse)
async def app_home(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "app.html",
        {"request": request},
    )


@app.get("/sw.js", include_in_schema=False)
async def service_worker() -> FileResponse:
    """Serve the worker from the root so it can cover the Cardr app shell."""
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type="application/javascript",
        headers={
            "Cache-Control": "no-cache",
            "Service-Worker-Allowed": "/",
        },
    )

@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("app.html", {"request": request})


@app.get("/market", response_class=HTMLResponse)
async def market_page() -> RedirectResponse:
    return RedirectResponse(url="/?view=market", status_code=303)


@app.get("/card", response_class=HTMLResponse)
async def card_page(
    request: Request,
    player: str = "",
    year: str = "",
    set_name: str = "",
    card_type: str = "",
    card_number: str = "",
    parallel: str = "",
    grade: str = "",
) -> HTMLResponse:
    query = urlencode(
        {
            "view": "analyze",
            "player": player,
            "year": year,
            "set_name": set_name,
            "card_type": card_type,
            "card_number": card_number,
            "parallel": parallel,
            "grade": grade,
        }
    )
    return RedirectResponse(url="/?" + query, status_code=303)


@app.post("/api/value")
async def api_value(data: Dict[str, Any]) -> JSONResponse:
    return JSONResponse(
        _valuation_response(
            _card_from_payload(data),
            _prediction_inputs(data),
        )
    )


@app.post("/api/comps")
async def api_comps(data: Dict[str, Any]) -> Dict[str, Any]:
    card = _card_from_payload(data)
    comps = find_comps(card, repository.all_observed_sales())
    return {
        "success": True,
        "card": card.as_dict(),
        "count": len(comps),
        "comps": [comp.as_dict() for comp in comps],
    }


@app.get("/api/recent-sales")
async def recent_sales(limit: int = 12) -> Dict[str, Any]:
    summary = repository.source_summary()
    return {
        "success": True,
        "sales": [sale.as_dict() for sale in repository.recent_sales(limit)],
        "data_quality": summary,
    }


@app.get("/api/plans")
async def plans() -> Dict[str, Any]:
    """Expose the launch plan matrix without implying billing is live."""
    return {
        "success": True,
        "plans": public_plan_catalog(),
        "billing_status": "preview",
        "billing_note": (
            "Paid-trial checkout is not connected in this local preview; "
            "no plan selection can create a charge."
        ),
    }


@app.get("/recent-sales", include_in_schema=False)
async def legacy_recent_sales(limit: int = 12) -> Dict[str, Any]:
    """Keep the original homepage route working while clients move to /api."""
    return await recent_sales(limit)


@app.get("/api/market-pulse")
async def market_pulse() -> Dict[str, Any]:
    """Return trends only where the stored evidence meets a strict threshold.

    This endpoint intentionally does not manufacture broad player or product
    trends.  The underlying pulse engine groups only fully identified cards and
    withholds a trend unless two recent months each have enough observed sales.
    """
    sales = repository.all_observed_sales()
    pulse = build_market_pulse(sales)
    months = sorted(
        {
            sale.sale_date.strftime("%Y-%m")
            for sale in sales
            if sale.sale_date is not None
        }
    )
    return {
        "success": True,
        **pulse,
        "window": {
            "sales_count": len(sales),
            "month_count": len(months),
            "first_month": months[0] if months else None,
            "last_month": months[-1] if months else None,
        },
        "data_quality": repository.source_summary(),
    }


@app.get("/api/cards/{card_id}/history")
async def card_price_history(card_id: int) -> Dict[str, Any]:
    """Return only real observed sale points for a saved Vault card."""
    card = get_vault_card(card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Vault card not found.")
    return {
        "success": True,
        "card_id": card_id,
        **build_card_price_history(CardQuery.from_mapping(card), repository.all_observed_sales()),
    }


@app.get("/api/cards/{card_id}/score")
async def card_score(card_id: int) -> Dict[str, Any]:
    """Return CARDR's explainable recorded-market score for a Vault card."""
    card = get_vault_card(card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Vault card not found.")
    return {
        "success": True,
        "card_id": card_id,
        **calculate_cardr_score(CardQuery.from_mapping(card), repository.all_observed_sales()),
    }


@app.get("/api/portfolio")
async def portfolio() -> Dict[str, Any]:
    """Return current portfolio intelligence and record today's actual snapshot."""
    cards = get_vault_cards()
    summary = get_vault_summary()
    _record_portfolio_snapshot(summary)
    return {
        "success": True,
        "summary": summary,
        "overview": build_portfolio_overview(cards),
        "composition": build_portfolio_composition(cards),
        "history": _portfolio_history(),
        "history_note": (
            "Portfolio history begins with real snapshots recorded by CARDR; "
            "no earlier values were backfilled."
        ),
    }


@app.get("/api/pulse")
async def pulse() -> Dict[str, Any]:
    """Return the daily CARDR briefing from private Vault and observed sales."""
    return {
        "success": True,
        **build_cardr_pulse(get_vault_cards(), repository.all_observed_sales()),
        "data_quality": repository.source_summary(),
    }


@app.post("/api/sales/refresh")
async def refresh_sales(data: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch matching completed sales through the configured market-data source.

    The external provider and key stay server-side. This endpoint is useful in
    local development; production should protect it with authentication and
    rate limits before exposing it publicly.
    """
    card = _card_from_payload(data)
    if not os.getenv("CARD_API_KEY"):
        raise HTTPException(
            status_code=503,
            detail=(
                "CARD_API_KEY is not configured. Import observed sale records or "
                "configure a market-data provider first."
            ),
        )

    from ebay import search_card_sales  # Keeps the core app free of API-key work at startup.

    try:
        result = search_card_sales(
            player=card.player,
            year=int(card.year) if card.year.isdigit() else 0,
            set_name=card.set_name,
            card_type=card.card_type or "Base",
            parallel=card.parallel or None,
            grade=card.grade or None,
        )
        sales = result.get("sales", [])
        save_sales(
            sales,
            card.player,
            card.year,
            card.set_name,
            card.card_type,
            card.parallel,
            card.grade,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Market-data refresh failed: {}".format(exc))

    return {
        "success": True,
        "query_used": result.get("query_used", ""),
        "imported_count": len(sales),
        "valuation": _valuation_response(card)["valuation"],
    }


@app.post("/scan")
async def scan_card(
    image: Optional[UploadFile] = File(None),
    file: Optional[UploadFile] = File(None),
) -> Dict[str, Any]:
    """Store a card photo safely; recognition is intentionally not faked."""
    upload = image or file
    if upload is None:
        raise HTTPException(status_code=422, detail="Provide an image file.")
    extension = ALLOWED_IMAGE_TYPES.get(upload.content_type or "")
    if extension is None:
        raise HTTPException(status_code=415, detail="Please upload a JPG, PNG, or WEBP image.")

    content = await upload.read(MAX_UPLOAD_BYTES + 1)
    if not content:
        raise HTTPException(status_code=400, detail="The uploaded image is empty.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Images must be 8 MB or smaller.")

    if _image_extension(content) != extension:
        raise HTTPException(
            status_code=415,
            detail="The file contents do not match the selected image type.",
        )

    filename = "card-{}{}".format(uuid.uuid4().hex, extension)
    (UPLOADS_DIR / filename).write_bytes(content)
    return {
        "success": True,
        "filename": filename,
        "url": "/uploads/" + filename,
        "identification": None,
        "message": (
            "Photo saved. Card recognition is not configured yet; verify the "
            "card details before valuation."
        ),
    }


@app.get("/health")
async def health() -> Dict[str, Any]:
    summary = repository.source_summary()
    return {
        "status": "ok",
        "valuation_mode": "evidence_first",
        "sales_file_exists": (BASE_DIR / "sales_data.csv").exists(),
        **summary,
    }
