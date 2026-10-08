# PriceLedger API

Private FastAPI backend for real India product lookup and durable, sourced price observations. Python 3.12+, SQLAlchemy/Alembic and PostgreSQL; SQLite runs locally without additional services.

## Run locally

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```sh
uv sync --locked
uv run alembic upgrade head
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Start with an empty catalog. No sample seed is included. Demo records from older development versions are excluded from every public product/history endpoint.

Optional settings are in `.env.example`. Copy it to `.env` to configure your local environment. For PostgreSQL, use `PRICELEDGER_DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST:5432/priceledger`, then run migrations. Never commit credentials. API reference: http://127.0.0.1:8000/docs.

## Real product lookup

`POST /api/v1/lookup` accepts `{"query":"PRODUCT_URL_OR_BRAND_MODEL_VARIANT"}`.

A product URL is checked against Amazon India, Flipkart or Meesho. Full Flipkart links retain `pid` to identify the actual variant and discard tracking parameters. Recognized short links are resolved only through allowlisted retailer hosts. Readable product pages supply actual INR item prices, explicit delivery charges, availability, identity and source. A successful lookup creates a real product and timestamped observation and returns its `product_id`. Recent observations are cached for 15 minutes by default. Link requests never substitute catalog samples.

A brand/model query searches retailer pages concurrently and returns candidate listings and per-retailer status. The user selects the exact colour, size, storage and pack before a price is recorded. Search snippets are not historical price evidence. Similar names are not automatically merged across stores.

Public page access varies. Some pages need JavaScript, refuse automated requests or change markup; blocked or unreadable pages return a specific error and record nothing. The adapter does not log in, solve CAPTCHAs, rotate proxies or guess a price. It reads a single explicit INR offer or Amazon's main price-to-pay, and rejects ambiguous offers, wrong variants and invalid amounts. Tests use controlled contracts, while the local browser checks also exercise the owner's supplied real product URL.

These are public-page observations in a private development prototype, not licensed historical feeds. Before public operation, review retailer/source permissions and obtain appropriate data access. Sources can be disabled by revoking their display/history flags or expiry; ingestion, lookup caching, display and collection enforce those settings. Source activation flags are technical configuration, not a representation that a retailer granted a license.

## Collection and history

Run a bounded refresh pass:

```sh
uv run python -m app.worker --once
```

Or keep a collector running alongside the API:

```sh
uv run python -m app.worker --interval-minutes 15
```

Each pass checks up to ten due listings, oldest first. A retailer refusal or connection failure stops further requests to that retailer in that pass. This initial collector suits a small private pilot; a durable distributed scheduler, per-source backoff and aggregate history queries are later work.

History starts at the first recorded observation. Launch dates and earlier prices remain unavailable unless actual historical evidence is imported. Missing shipping is never treated as free. Offline prices require dated, exact-variant shop evidence; there is no universal offline-price feed or fabricated percentage estimate.

- `GET /api/v1/products?q=&category=&limit=24&offset=0`: stored real variants.
- `GET /api/v1/products/{id}`: offers, retailer links, online/offline channel, timestamps, price components and coverage.
- `GET /api/v1/products/{id}/history?days=90&basis=item`: source-labelled timeline; `basis=delivered` requires known shipping. Maximum range is 36,500 days.
- `GET /api/v1/products/{id}/assessment`: comparable delivered-total reference and coverage.
- `GET /api/v1/categories`: stored categories.
- `GET /api/v1/health`: service health.
- `POST /api/v1/ingest`: administrative evidence import, disabled until `PRICELEDGER_ADMIN_TOKEN` is set; requires `X-Admin-Token`. Never expose this secret to the frontend.

Administrative imports require a permitted source, mapped exact offer, matching attributes, INR integer paise, aware timestamp and unique source event ID. Identical repeated events are idempotent; conflicting reuse is rejected. Backfills preserve newer current prices.

Assessments use the median of daily delivered totals over 30 days ending seven days ago, with at least 20 observed days. Recorded lows cover a stated offer and period. A rise/drop pattern does not prove manipulation. Item-price charts and delivered-total assessments are labelled separately.

Lookup is limited to 12 requests/minute/client in one process. HTTP access uses an exact host allowlist, public-address checks, validated redirects, bounded timeouts and a 3 MB decoded page limit. Shared rate limiting and enforced egress policy are required before running multiple public replicas.

## Verify

```sh
uv run pytest -q
uv run alembic check
```

GitHub CI checks unit contracts and PostgreSQL 17 migrations, lookup persistence, backfills and assessment roundtrips without external retailer dependencies.

Frontend: https://github.com/codeWith-Ashwani/priceledger-web. Only README documentation is published; research and sprints remain local. Both repositories remain private until the owner explicitly changes visibility.
