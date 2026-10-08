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

Amazon India, Flipkart and Meesho have dedicated URL rules. Other public ecommerce URLs use a generic reader when the page exposes one unambiguous INR Product/Offer in structured data. Full Flipkart links retain `pid` to identify the actual variant and discard tracking parameters. Recognized short links are resolved only through allowlisted retailer hosts. Readable product pages supply actual INR item prices, explicit delivery charges, availability, identity and source. A successful lookup creates a real product and timestamped observation and returns its `product_id`. Recent observations are cached for 15 minutes by default. Link requests never substitute catalog samples.

A brand/model query searches retailer pages concurrently and returns candidate listings and per-retailer status. The user selects the exact colour, size, storage and pack before a price is recorded. Search snippets are not historical price evidence. After selecting or pasting a listing, the frontend automatically starts a cross-store comparison. Similar names alone are not enough to merge prices.

Public page access varies. Some pages need JavaScript, refuse automated requests or change markup; blocked or unreadable pages return a specific error and record no price. The adapter does not log in, solve CAPTCHAs, rotate proxies or guess a price. It reads a single explicit INR offer or Amazon's main price-to-pay, and rejects ambiguous offers, wrong variants and invalid amounts. Tests use controlled contracts, while the local browser checks also exercise the owner's supplied real product URL.

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

Each pass claims up to ten due jobs, oldest first (`--max-jobs` accepts 1â€“100). Jobs and attempts survive process restarts. Recently requested listings become eligible every six hours; listings without demand for seven days use a 24-hour cadence. These are configurable polling intervals, not a promise to capture every price change. Keep the worker running to collect future history.

Workers use five-minute leases, conditional writes and PostgreSQL `SKIP LOCKED` claims. Expired workers cannot append prices; a later worker can reclaim their job. Failures retry with exponential delays from 15 minutes to 24 hours. Refusals/connection failures apply a persisted six-hour source cooldown; changed variants pause for review. The last valid quote remains intact. A paused identity needs operator review before requeuing; there is no public recovery control yet.

A shared database budget limits each source to 500 reserved fetch operations per UTC day by default. URL lookup and collector checks reserve one operation, name searches reserve one per store, and comparisons conservatively reserve three per store (search plus up to two candidate checks). Unused reservations are not refunded; redirects are bounded by the HTTP adapter separately. Cached prices do not consume budget or create observations. This source budget supplements the process-local client limiter.

History starts at the first recorded observation. Launch dates and earlier prices remain unavailable unless actual historical evidence is imported. Missing shipping is never treated as free. Offline prices require dated, exact-variant shop evidence; there is no universal offline-price feed or fabricated percentage estimate.

- `GET /api/v1/products?q=&category=&limit=24&offset=0`: stored real variants.
- `GET /api/v1/products/{id}`: offers, retailer links, online/offline channel, timestamps, price components and coverage.
- `GET /api/v1/products/{id}/history?days=90&basis=item`: source-labelled timeline; `basis=delivered` requires known shipping. Maximum range is 36,500 days.
- `GET /api/v1/products/{id}/assessment`: comparable delivered-total reference and coverage.
- `GET /api/v1/products/{id}/coverage`: aggregate evidence counts/India days, first/latest observations, collection state, last check and next eligible time.
- `GET /api/v1/categories`: stored categories.
- `GET /api/v1/health`: service health.
- `POST /api/v1/ingest`: administrative evidence import, disabled until `PRICELEDGER_ADMIN_TOKEN` is set; requires `X-Admin-Token`. Never expose this secret to the frontend.

The original `/ingest` contract requires a permitted source, mapped exact offer belonging to that source, matching attributes, INR integer paise, aware timestamp and unique source event ID. Identical repeated events are idempotent; conflicting reuse is rejected. Backfills preserve newer current prices. `evidence_kind` accepts `recorded` (default), `page_check` or `historical_import`; it labels evidence and does not validate a provider's claim. New observations retain their submitted variant attributes. Public-page checks also retain seller/condition/region/availability at that check; older records retain an unknown snapshot rather than inventing past identity. Independent archive-to-offer mappings use the reviewed historical import workflow below; automatic provider adapters remain pending.

Assessments use India calendar days for the median of daily delivered totals over 30 days ending seven days ago, with at least 20 observed days. Recorded lows cover a stated offer and period. A rise/drop pattern does not prove manipulation. Item-price charts and delivered-total assessments are labelled separately.

Lookup is limited to 12 requests/minute/client in one process. Dedicated-store HTTP access uses an exact host allowlist, public-address checks, validated redirects, bounded timeouts and a 3 MB decoded page limit. Lookup and automatic-comparison quotas are separate. Generic URLs use DNS-pinned HTTPS connections: every resolved address must be public, the connection goes to that checked IP, TLS verifies the original hostname, and each redirect is revalidated. Generic fetches reject credentials, private/reserved addresses, unusual ports and non-product pages, with a 35-second deadline and 3 MB body limit. Shared rate limiting and enforced egress policy remain necessary before running multiple public replicas.

## Verify

```sh
uv run pytest -q
uv run alembic check
```

GitHub CI checks unit contracts and PostgreSQL 17 migrations, lookup persistence, backfills and assessment roundtrips without external retailer dependencies.

Frontend: https://github.com/codeWith-Ashwani/priceledger-web. Only README documentation is published; research and sprints remain local. Both repositories remain private until the owner explicitly changes visibility.

## One-link cross-store comparison

`POST /api/v1/products/{id}/compare` searches the other connected retailers automatically and verifies up to two readable candidate listings per retailer. Comparison scans are cached for 15 minutes and serialized in this pilot process to avoid duplicate fan-out. A retailer with no readable match is reported explicitly. Adding more discovery retailers requires their search/feed adapter; accepting a source URL is separate from guaranteeing coverage of every other retailer.

Verified matches create durable comparison links while keeping each retailer's original product attributes, offer and observations intact. Catalog detail, best-price assessment and history then include all verified group members. Matching requires either a valid shared GTIN with consistent brand/condition and no conflicting attributes, or complete matching phone brand/model/RAM/storage/colour/condition. A changed known condition cannot enter a previous variant's history. Missing information requires review; different variants remain excluded. Their genuine prices and links may be displayed separately without entering the exact-product winner or combined graph.

History responses include a separate series for each actual offer, retailer, source and online/offline channel. The frontend additionally renders a paginated observation ledger with date/time, store, item price, delivered total and source. No current page can reconstruct launch-to-today prices. Licensed/public-permitted historical evidence can be backfilled through the administrative ingestion contract; a historical data provider is not yet connected.

## Reviewed historical imports

The separate owner UI lives at `/?admin=imports` in the React repository. Configure a server-only `PRICELEDGER_ADMIN_TOKEN`; import endpoints require `X-Admin-Token`, send `Cache-Control: no-store`, and are disabled without that configuration. The frontend accepts the token at runtime without persisting it. Never commit it or put it in frontend build variables. Operate over HTTPS beyond localhost.

- `GET /api/v1/admin/sources`: active approved historical/merchant sources. Public-page and demo sources are excluded.
- `POST /api/v1/admin/sources`: register a new historical source with `id`, `name`, `approval_reference`, explicit `rights_confirmed: true` and optional aware `expires_at`. Existing source IDs cannot be overwritten. Approval is an operator assertion based on verified access; it does not purchase or establish rights.
- `GET /api/v1/admin/offers?q=`: up to 50 stored real listings, with exact identifiers/attributes/context for preparing templates.
- `POST /api/v1/admin/imports/preview`: JSON envelope containing `source_id`, `file_name`, `format` (`csv` or `json`) and UTF-8 file `content`. Request body is capped at 1.5 MB; file content at 1 MB and 1,000 records. Preview stores a valid 24-hour draft and no observations/mappings; invalid rows block staging.
- `POST /api/v1/admin/imports/{id}/commit`: atomically commit a reviewed draft. Exact repeats are skipped; conflicting events or changed source/identity require another preview. Repeating a completed commit returns its saved result, while revoked source access still denies the request.
- `GET /api/v1/admin/imports`: the latest 20 preview/commit audit records, file hashes and committed results; raw payloads remain private.

CSV headers and JSON keys:

```text
offer_id,retailer_id,retailer_listing_id,external_event_id,observed_at,variant_attributes,seller,condition,region,item_paise,shipping_paise,mrp_paise,in_stock,currency,price_type,evidence_reference
```

`variant_attributes` is a JSON object, including when inside a quoted CSV cell. The owner UI can download a JSON template with current identity fields and blank evidence fields; no example prices or fictional products are included. `observed_at` requires an explicit timezone and must be in the past. Stock uses JSON booleans or CSV `true`/`false`. Available observations require an item price. Amounts are strict integer paise (not decimal rupees, exponent notation or formulas); optional amounts are null/empty. Currency defaults to INR. Price type defaults to `listing`; conditional/coupon/other types are rejected. Seller, condition, region, retailer/listing ID and the complete typed variant attributes must match the tracked offer. Different historical contexts need their own tracked/reviewed offer; this importer does not guess or merge them. The initial mapping supports retailer listing IDs, not arbitrary provider aliases.

Committed imports create independent source-to-offer mappings and observations with an import ID, evidence reference and immutable original identity/context. They always leave live current quotes intact, even when a historical record is newer than the last live check. Existing same-source administrative `/ingest` remains available; independent-source imports use the reviewed workflow. No file or import creates an untracked product.

History series are keyed by offer/source/price type, so overlapping archives remain individually attributable without creating extra retailer offers. Coverage includes all currently permitted historical sources, even when an independent live source is disabled. Revoking a source hides its observations. Assessments select one approved evidence source per India day: own records first, then deterministic source-ID order. They exclude incompatible imported seller/condition/region and non-listing price types. The source choice is a reproducible precedence rule, not a confidence estimate; graphs/ledgers retain all approved original observations. Advertised discounts use the current own-source observation, never a mismatched imported MRP.

SQLite migrations preserve existing data/checks and add a named import foreign key. PostgreSQL commits lock the source and product/offer identity rows before revalidation; SQLite's conditional draft write serializes writers. Bounded files, bulk lookups and commit batching avoid one lookup per row. Database constraints and transaction rollback prevent partial event/mapping writes under concurrency. Import code cannot verify a source's truthfulness: operators must audit original records, exact identities and retention/display permissions. No external historical feed or full launch archive is connected automatically.
