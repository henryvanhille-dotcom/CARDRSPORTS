# Cardr

Cardr is an evidence-first baseball-card intelligence app. Its Prospectr
prediction engine estimates a card's current CAD market value from stored
marketplace sales and shows every transaction or predictive anchor used.

## What is implemented

- Card search fields for player, year, product, type, card number, parallel,
  and grade.
- One unified workspace for overview, analysis, market evidence, and the
  personal Vault—without moving between separate app pages.
- The Prospectr rare-card predictive engine for cards with no exact sale,
  including 1/1s, known print runs, and confirmed 1st Bowman autographs. It
  labels predictions separately from observed-sale valuations and shows the
  exact related sales, recent market segment, or user-supplied anchor used.
- A personal Vault with validated collection records, photo upload, favorite
  cards, and a one-click evidence-based value refresh for each saved card.
- A comparable-sales engine that ranks same-player sales by supplied card
  details and distinguishes exact-card matches from broader matches.
- A recency- and match-quality-weighted CAD estimate, range, confidence, and
  evidence score.
- Strict sample/demo exclusion: a row marked `sample`, `demo`, `mock`, `test`,
  `fixture`, or `placeholder` is never shown as a sale or used in a valuation.
- The existing SQLite store and a CSV import file as sale sources.
- Optional live refresh through `ebay.py` when `CARD_API_KEY` is configured.
- Safe photo storage. Automatic card recognition is intentionally not claimed
  until a recognition provider is added.

## Run locally

```bash
cd /Users/henryhille/Prospectr
python3 -m pip install -r requirements.txt
python3 -m uvicorn app:app --reload
```

Open `http://127.0.0.1:8000`. API documentation is at
`http://127.0.0.1:8000/docs`.

Run the regression tests with:

```bash
cd /Users/henryhille/Prospectr
PYTHONPYCACHEPREFIX=/private/tmp/prospectr-pycache python3 -m unittest discover -s tests -v
```

## Managed hosting and cardrsports.com

This project is ready to deploy as a Render Blueprint using `render.yaml`.
The selected service size is Render's smallest paid web-service tier because
CARDR needs the attached 1 GB disk to preserve its SQLite database and uploaded
card photos. The app writes those files to `/var/data` in production and keeps
the same local paths during development.

1. Put this project in a private Git repository. The included `.gitignore`
   intentionally excludes the local Vault database, uploads, and `.env` files.
2. In Render, create a new Blueprint from that repository. Render will deploy
   the Python service and assign an `onrender.com` address.
3. Confirm `https://YOUR-SERVICE.onrender.com/health` returns `"status":"ok"`.
4. In the service's Custom Domains settings, add both `cardrsports.com` and
   `www.cardrsports.com`. Then configure the requested Webnames DNS records:
   use Render's apex A record (`216.24.57.1` when Webnames does not offer an
   ALIAS/ANAME record) and point `www` at the exact `onrender.com` hostname
   Render assigns. Remove conflicting A/AAAA/CNAME records before verification.
5. Wait for Render's domain verification, then confirm both HTTPS addresses
   load. Render provisions and renews the TLS certificate after verification.

The production disk begins as an intentionally clean dataset; this deployment
configuration does not publish a developer's local Vault or uploaded images.
Before inviting customers to use Vault or Watchlist, add real user accounts and
server-side per-user plan enforcement. Without authentication, a shared public
deployment cannot safely treat Vault records as private.

## Sales data contract

`sales_data.csv` can supplement the SQLite store. A usable transaction needs:

```csv
player,year,set_name,card_type,card_number,parallel,grade,sale_price_usd,sale_date,source,listing_url,title
Mike Trout,2011,Bowman Chrome,RC,175,,PSA 9,215.00,2026-09-01,eBay,https://example.com/sale,2011 Bowman Chrome Mike Trout #175 PSA 9
```

Use a real completed-sale source and an actual date. Current conversion of USD
rows to CAD uses a documented fixed conversion factor in `market_data.py`; add
an audited FX-rate source before treating that conversion as production-grade.

## No-sale and one-of-one predictions

Predictive mode deliberately has a higher evidence bar than a simple guess:

- An exact stored sale always takes precedence over the model.
- Otherwise, the model needs either related same-player stored sales (same year
  or product), a recent 1st Bowman autograph market segment for a confirmed
  1st Bowman auto, or a user-supplied reference value. With none of these, it
  returns no dollar figure.
- The result exposes its baseline, scarcity multiplier, related-comp count,
  broad range, confidence cap, and a monthly sales chart of the exact evidence
  that anchored the result.
- One-of-ones use the broadest uncertainty range. Scarcity is a transparent
  model prior, not evidence that the exact card has sold at that premium.

## Honest current limits

- Cardr does not yet recognize a card from its photo.
- It does not infer player-comparison groups, PSA population, print run, or
  future prices yet. A print run must be supplied by the user when known.
- A public deployment needs authentication and rate limiting on
  `POST /api/sales/refresh` before that endpoint is exposed.
