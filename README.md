# PriceLedger API

Backend for comparing real product prices in India and maintaining a dated, source-attributed history. Users provide a product URL or brand/model query; the API verifies readable listings, discovers matching offers and stores actual observations.

**Stack:** Python 3.12+, FastAPI, SQLAlchemy, Alembic and PostgreSQL. SQLite supports local development. The [React frontend](https://github.com/codeWith-Ashwani/priceledger-web) lives in a separate private repository.

**Release 1.0.0:** feature scope is frozen. This release consolidates the existing lookup, matching, comparison, recorded history, targets, owner tools, issue review and documented local-shop evidence. Both repositories remain private. Coverage depends on readable, permitted sources; a release version does not imply universal store coverage or complete launch history.

## Quick start

Install Python 3.12+ and [uv](https://docs.astral.sh/uv/getting-started/installation/). From this repository:

```sh
uv sync --locked
uv run alembic upgrade head
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

A fresh installation starts with an empty catalog in `priceledger.db`; there is no sample seed. Apply migrations before starting the API or workers. Environment variables and an optional `.env` configure the service; `.env.example` lists operational settings. Restart processes after configuration changes. For PostgreSQL, set `PRICELEDGER_DATABASE_URL` to your `postgresql+psycopg://` connection string before migrating. Keep credentials out of Git.

| Local URL | Purpose |
| --- | --- |
| `http://127.0.0.1:8000/docs` | Interactive API reference |
| `http://127.0.0.1:8000/api/v1/health` | Liveness, independent of the database |
| `http://127.0.0.1:8000/api/v1/ready` | Database connectivity and migration-head readiness |

## Lookup and comparison

`POST /api/v1/lookup` accepts a JSON object with a `query` string containing a product URL or brand/model. URL lookup returns `kind: product` and a `product_id` when identity can be read. Name lookup returns `kind: search`, candidates and store diagnostics; selecting a candidate verifies its actual page. Search snippets are not verified prices. Readable identity without price can still start discovery, with the price unconfirmed.

`POST /api/v1/products/{id}/compare` checks other connected stores. The frontend calls it automatically. Same-model offers may differ in colour, storage, RAM or size and retain labelled differences. Only verified exact identities share history. Generations, accessories, protection bundles and incompatible conditions remain distinct.

Dedicated adapters cover Amazon India, Flipkart, Meesho, Croma, Reliance Digital and brand-gated iQOO, Sony Center and boAt. Generic readable public pages can expose an unambiguous INR Product/Offer. Verified Shopify submissions can teach new stores and observed brands; brand-scoped iPlanet/Imagine origins support Apple discovery, and Noise's official public catalog supports Noise discovery. Known brand/model searches include these origins without prior product registration. Adapter endpoints and security allowlists are explicit integration metadata. Product IDs, prices, ratings and stock come from source evidence.

Shopify ProductGroup pages require agreement between the selected variant's schema SKU/INR price and native product metadata. An unselected group can identify a model only when its product ID, vendor and page path agree; it supplies no variant price. Unpriced anchors can search their own store for verified variants. Candidate ordering preserves model qualifiers, including Elite, Neo, SE and Plus/+, and prefers variants of the submitted page when other evidence ties. Search metadata is bounded and ranked before truncation; Apple reseller exclusion queries are not applied to Noise's different search behavior. Ratings may inherit a group's explicitly published rating, while shipping and condition remain unknown unless evidenced.

All returned verified offers participate in comparison; there is no three/four-card cap or 100-candidate catalog cutoff. Discovery is bounded by candidate verification, source budgets and supported adapters. Missing cards can mean blocked access, cooldown, unreadable markup or unverified identity, rather than absent stock. Readable current pages do not provide complete historical archives.

Fire-Boltt's official `.com` store supports URL lookup and brand/model discovery, including the spellings Fire-Boltt, Fireboltt and Fire Boltt. Its theme uses Boltt Games as vendor and can append an invalid `itemReviewed` property to otherwise valid Product JSON. The scoped adapter accepts only that known suffix and requires canonical path, visible title, native product ID/vendor, selected variant, SKU, INR price and stock to agree. It reads literal JSON without executing scripts or making additional requests. Search prices remain untrusted; sold-out listings remain visible but cannot win current ranking. Word-only watch models preserve Pro/Plus/Max and other qualifiers. Shopify variant IDs are not treated as manufacturer part numbers. Shipping, condition and ratings stay unknown unless evidenced. No `.shop` mirror or unrelated host inherits this adapter or brand identity.

Candidate URLs must belong to the searched retailer before a listing is fetched. Duplicate canonical URLs are checked once per store scan. Rejected URLs remain diagnostics; failed page checks retain their validated target for audit recording. One rejected candidate cannot abort the other store results.

## Prices and history

Amounts use integer INR **paise**. Unknown values remain `null`.

| Field/calculation | Price basis |
| --- | --- |
| `item_paise`, item history | Item price |
| `total_paise`, `best_price_paise`, delivered history | Item + known shipping, excluding extras |
| `known_subtotal_paise` | Item + known shipping + explicitly observed listed fees |
| Recorded insights | Selected-period exact online listing prices on the requested basis |
| Deal assessment | Fresh available exact online item + shipping against daily historical medians |

Unknown shipping prevents a confirmed delivered total. Known subtotals may omit other checkout charges. Conditional bank/coupon/exchange promotions are excluded from unconditional prices. Ratings retain retailer provenance. Stale, unavailable and future-dated quotes cannot win current ranking.

History begins with genuine observations. Older evidence requires permitted archive records. Launch dates and past prices are not fabricated; offline ranges require dated shop evidence. Legacy demo records stay excluded.

History, insights and assessments share bounded evidence reads and source-permission checks. Query parameters are batched across exact-group products, with one observation ceiling across the entire result. Oversized ranges return 422 rather than truncated evidence. The fixed one-year assessment is also bounded; large histories require capacity tuning or a future aggregate service.

Insights show selected-period low/high with store/source/time and an equally weighted average of each observed India day's lowest available exact online price. Missing days are excluded. Assessments use the median of daily delivered prices over 30 days ending seven days ago, requiring 20 observed days. Neither statistic predicts prices or proves manipulation.

Calculations select own checks first per offer/India day; otherwise one approved archive is chosen by source ID. Imported seller/condition/region must remain compatible. Raw history keeps separate approved source series and original evidence. Source precedence is deterministic, not a confidence estimate.

## API reference

All paths below use `/api/v1`. URL-encode IDs in paths.

| Method and path | Purpose |
| --- | --- |
| `POST /lookup` | Resolve a URL or search brand/model |
| `POST /products/{id}/compare` | Scan connected stores and return offers/diagnostics |
| `POST /product-comparison` | Read independent exact-variant offers for 2–4 selected products |
| `GET /products?q=&category=&limit=24&offset=0` | Paginated real catalog |
| `GET /products/{id}` | Exact-variant offers and metadata |
| `GET /products/{id}/history?days=90&basis=item` | Source-separated dated timeline |
| `GET /products/{id}/insights?days=90&basis=item` | Recorded milestones, daily average and coverage |
| `GET /products/{id}/assessment` | Historical reference, savings and insufficient-history status |
| `GET /products/{id}/coverage` | Evidence counts, India days, source and collection state |
| `GET /products/{id}/offline-quotes?condition=new&days=90&limit=10&offset=0` | Reviewed city-specific shop evidence and current sampled range |
| `GET /categories` | Stored real-product categories |
| `GET /health`, `GET /ready` | Liveness and migration-aware readiness |
| `POST /ingest` | Authenticated same-source evidence ingestion |
| `POST /reports` | Submit an issue against a displayed offer and comparison context |

History/insights accept 1–36,500 days and `basis=item` or `basis=delivered`. Coverage aggregates evidence in SQL and fetches collection state in bulk. An indexed SQL query selects the latest permitted completed attempt per offer; running, future-started and revoked-source attempts are excluded.

## Side-by-side product comparison

`POST /api/v1/product-comparison` accepts `{"product_ids":["first-recorded-id","second-recorded-id"]}` with 2–4 distinct recorded product IDs. It returns columns in selection order, recorded product attributes, all permitted exact-variant offers per selection and a read timestamp. Each anchor requires its own approved non-demo offer. Missing/revoked selections return 404 for the whole request; selecting two members of the same exact-variant group returns 409. Remove that selection and retry. Duplicate IDs, extra fields and invalid sizes return 422.

This endpoint reads the database using the public read quota. It does not contact retailers, reserve collection budgets, create matches, save a comparison list or merge histories. Exact-group offer parameters are batched without a result/store cap. Store quotes retain their source, seller, condition, fees, rating and observation time; a read timestamp is not a new store check. Missing attributes remain missing. Stored variant details can originate in retailer metadata/title parsing and are not independently verified technical specifications. The frontend ranks fresh available online prices separately within each column, with no overall best-product claim.

## Collection and maintenance

The API does not automatically start a worker. Run one bounded pass, or keep a collector running alongside it:

```sh
uv run python -m app.worker --once --max-jobs 10
uv run python -m app.worker --interval-minutes 15 --max-jobs 10
```

Jobs, attempts, leases, budgets and retries persist. Default eligible polling is every six hours for recently requested listings and 24 hours for stable ones. Each pass processes due jobs; it cannot capture every intervening change. Claims use leases and conditional writes; PostgreSQL uses `SKIP LOCKED`. Expired workers cannot append results. Failed checks preserve the last valid quote and can activate source cooldowns; changed variants pause for review.

Discovery reserves one search plus its configured candidate reads before parallel work. iQOO also reserves its additional SKU request. Reservations are conservative and unused reservations are not refunded. Run quota housekeeping through your scheduler:

```sh
uv run python -m app.maintenance --max-rows 1000
```

Maintenance removes old expired quota windows, not price observations or import audits. Backups, restore testing and long-term retention infrastructure are deployment responsibilities.

## Configuration

Settings use the `PRICELEDGER_` prefix. Origins and retailer lists use JSON arrays. See `.env.example` for request-body, database-pool, timeout, freshness, collection-cadence and lease settings.

| Setting suffix | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite:///./priceledger.db` | Database connection |
| `CORS_ORIGINS` | Local React origins | Allowed frontend origins |
| `ADMIN_TOKEN` | Empty | Administration disabled until configured |
| `LOOKUP_CACHE_MINUTES` | 15 | Recorded-link cache lifetime |
| `COMPARISON_CACHE_MINUTES` | 15 | Store-scan cache lifetime |
| `DISCOVERY_WORKERS` | 3 | Parallel searches, range 1–8 |
| `DISCOVERY_CANDIDATES_PER_STORE` | 2 | Verified candidates per store, range 1–5 |
| `DISCOVERY_EXTRA_STORE_LIMIT` | -1 | All registered sources; 0 disables extra discovery; positive values set an operator limit |
| `DISCOVERY_REGISTRY_CAPACITY` | 64 | Maximum new learned-store registrations |
| `DISCOVERY_BRANDS_PER_STORE` | 32 | Maximum new observed-brand associations per store |
| `LOOKUP_REQUESTS_PER_MINUTE` | 12 | Separate lookup/comparison client quota buckets |
| `READ_REQUESTS_PER_MINUTE` | 120 | Public data-read quota |
| `ADMIN_REQUESTS_PER_MINUTE` | 60 | Authenticated administration quota |
| `REPORT_REQUESTS_PER_MINUTE` | 5 | Separate public report quota, range 1–60 |
| `OFFLINE_FRESHNESS_DAYS` | 7 | Maximum age of a current shop quote, range 1–30 |
| `MAX_HISTORY_OBSERVATIONS` | 10,000 | Global evidence ceiling per request/calculation |
| `COLLECTION_DAILY_BUDGET` | 500 | Reserved operations per source/UTC day |
| `COLLECTION_COOLDOWN_HOURS` | 6 | Source retry cooldown |

Learning capacities do not hide existing stores when lowered. Cache validity includes candidate policy, source permissions and the enabled retailer set. Parser/security rules, schema contracts and statistical policy remain explicit versioned code. Raising concurrency/candidate counts increases source traffic; preserve budgets and cooldowns.

Future-dated observations and scans cannot qualify as cache hits. Comparison anchors require an approved non-demo source even when reusing a cached result. Concurrent scans of one product retain the 409 lease guard and short `Retry-After` response; clients may retry within a bounded policy without bypassing source quotas.

## Administration and historical imports

Set a server-only `PRICELEDGER_ADMIN_TOKEN`. Administrative requests require `X-Admin-Token`; never put it in Vite variables, URLs or source files. The frontend owner workspace at `/?admin=imports` holds its token only in memory.

| Administrative path | Purpose |
| --- | --- |
| `GET/POST /admin/sources` | List/register permitted sources and approval references |
| `GET /admin/offers?q=` | Find tracked identities for templates |
| `POST /admin/imports/preview` | Validate CSV/JSON and stage a valid draft |
| `POST /admin/imports/{id}/commit` | Revalidate and atomically commit |
| `GET /admin/imports` | Recent draft/commit audit records |
| `GET /admin/discovery-stores` | Learned stores/source status |
| `POST /admin/discovery-stores/{id}/status` | Pause/resume searches without deleting evidence |
| `GET /admin/collection/overview` | Recorded collection counters and completed checks in the last 24 hours |
| `GET /admin/collection/offers?status=all&limit=25&offset=0` | Paginated tracked-offer health and scheduling constraints |
| `GET /admin/collection/sources?limit=25&offset=0` | Paginated source access, cooldowns, daily budgets and check outcomes |
| `GET /admin/reports?status=open&limit=25&offset=0` | Private issue queue, optionally filtered by review status |
| `GET /admin/reports/{id}?limit=25&offset=0` | Captured evidence, issue details and paginated review audit |
| `POST /admin/reports/{id}/review` | Update review status with an expected revision and private note |
| `GET/POST /admin/offline/shops` | Paginated documented shops / permission-backed shop registration |
| `GET/POST /admin/offline/quotes` | Paginated private queue / immutable exact-variant draft |
| `GET /admin/offline/quotes/{id}` | Private evidence, shop identity and paginated review audit |
| `POST /admin/offline/quotes/{id}/review` | Publish or withdraw with a UUID4 receipt and expected revision |

Collection health is read-only: it neither contacts retailers nor starts jobs. Offer filters include `stale`, `failed`, `gap`, `due`, `paused`, `unscheduled` and `inactive`; pages accept 1–100 records. Missing/future-dated or older-than-`FRESHNESS_HOURS` quotes count as stale. A gap means no approved, past listing-price observation within `HISTORY_GAP_HOURS`; an unpriced identity check cannot close it. Due counts follow worker scheduling, source permissions, UTC daily budgets and cooldowns; leased jobs do not prove a worker process is alive. Source diagnostics include search failures even when no offer was created. Indexed latest-record queries and SQL aggregates avoid loading observation history or issuing one query per displayed offer.

Imports accept UTF-8 CSV or JSON arrays, at most 1 MB and 1,000 records. Each row identifies an existing exact offer, retailer listing, typed variant attributes, seller/condition/region, unique source event, aware past timestamp, stock and evidence reference. Prices are integer paise; only INR listing prices are accepted. Unknown shipping remains empty/null. Download an identity template with blank evidence fields from the owner UI.

Invalid rows block staging. Drafts expire after 24 hours. Commit rechecks permissions, identity and event conflicts and writes atomically. Archive imports retain original context, mappings, file hashes and audit references; they never overwrite current live quotes. Source registration records operator approval, not independently verified rights. No historical provider is connected automatically.

## Offer issue reporting

Public reporting accepts `wrong_price`, `wrong_product`, `outdated_price`, `stock_status` and `broken_link`. Send a UUID4 `request_id`, `offer_id`, `comparison_product_id`, 5–1,000 characters of `details`, and the displayed `observed_at`, `item_paise` and `in_stock`. Prices use strict integer paise; nullable price/time fields must still be present. The offer and comparison anchor must have approved non-demo source evidence. A changed quote returns 409; refresh the product before reporting again.

The server captures its own offer, source, seller, variant and comparison snapshot. Reports preserve the quoted evidence even if the listing changes. Retrying the same identifier and normalized payload returns the original receipt (200); new reports return 201. Reusing an identifier with different details returns 409. Public responses contain only a receipt, with no public report lookup or private notes. Avoid personal/payment information in free text. Intake consumes its separate persisted client quota and never fetches retailers.

Owner review statuses are `open`, `in_review`, `resolved` and `dismissed`; queue and audit pages accept 1–100 records. Reviews require `expected_revision`, `status` and an optional private `note` of up to 1,000 characters. Resolving/dismissing requires an explanation of at least five characters. Concurrent edits return 409; reload before retrying. Each change appends an audit event atomically with its revision. Reviewing a report does not modify prices, observations, matches or collection jobs. Verify and correct underlying evidence separately. Anonymous reports have no outbound notifications or user accounts.

## Reviewed local shop quotes

Offline evidence is owner-entered and reviewed; the API does not scrape or approximate offline prices. In the frontend owner workspace, open **Record and review local shop quotes**, register a documented shop with its city/state/area and permission reference, find an existing exact tracked product, record a private draft, then review the shop, product, price, dates and evidence before publishing. Use unconditional tax-inclusive INR item prices. No receipt image upload, anonymous shop intake or independent verification of an owner's attestations is provided. Keep personal/payment information out of references and notes.

Shop registration requires `request_id` (UUID4), `name`, `city`, `state`, `area`, `approval_reference`, and explicit boolean `rights_confirmed: true`; `expires_at` optionally records an aware future permission deadline. Normalized shop/name/location uniqueness prevents duplicate registrations. Registration creates a separate merchant source. Permission metadata is private; administrative source operations can inspect it, and revoked/expired source permissions hide related public evidence.

Quote creation requires `request_id`, `product_id`, `shop_id`, the complete current `variant_attributes`, strict integer `item_paise` (0–2,000,000,000), explicit boolean `in_stock`, `condition` (`new`, `used`, `refurbished`), aware past `observed_at`, later `valid_until` within 30 days, `evidence_reference`, and three explicit true attestations: `exact_variant_confirmed`, `includes_tax`, `unconditional_price`. Each immutable draft captures product identity and typed variant attributes. Publication rechecks variant identity and active product/shop source permissions. Expired historical evidence can be published but cannot count as current.

Review requires a new UUID4 `request_id`, `expected_revision`, `action` (`publish` or `withdraw`), and a private 5–1,000 character `note`. Only drafts can be published; drafts or published quotes can be withdrawn, and withdrawal is final for that record. Record corrected evidence as a new draft. Every accepted transition atomically appends an audit event. Identical registration/draft/review retries return the original identity/receipt; conflicting reused identifiers or revisions return 409. Review retries return the original transition even if a later transition has occurred; reload detail for current status.

Public reads require approved non-demo online identity evidence for the product. They accept `condition`, `city_id` (32-character ID from `cities`), `days` (1–3,650), `limit` (1–100) and `offset`. The default history is 90 days and the default city is the first sorted city with published evidence. City spelling/whitespace are normalized within the stated state; this is not a geocoding/pincode service. Each city/condition/variant remains separate. Public results expose documented shop name/area, price, observation/validity/review time and source; evidence references and audit notes stay private.

The newest previously published observation per shop controls its current eligibility; a later withdrawal or out-of-stock observation cannot resurrect an older available quote. Withdrawn unpublished drafts do not supersede published evidence. A current quote must be in stock, unexpired, past-dated, variant-compatible and no older than `OFFLINE_FRESHNESS_DAYS`. At least two distinct documented shops are required for the sampled min/max range; fewer quotes produce null range bounds. SQL aggregates count eligible shops across the full city selection independently of history pagination. The range describes the documented sample, not the whole offline market, and is separate from online rankings, histories and deal assessments. Reads and reviews never contact retailers or modify online observations, matches or collection jobs. No offline quotes are seeded.

## Safeguards

- Public HTTP reads validate HTTPS, DNS and redirects, pin connections to checked public addresses and verify TLS for the original host. Private networks, credentials, unsupported responses and excessive content are rejected. No environment proxy, login, CAPTCHA solving or refusal bypass is used.
- Body/deadline limits, strict integer amounts, typed identities, idempotent events and database constraints protect ingestion and backfills. Conditional updates prevent older quotes overwriting newer ones.
- Persisted quotas, source budgets, cooldowns and fenced leases coordinate API processes/workers. Client quotas use connection-address digests; forwarded headers cannot bypass them. Configure trusted proxy handling explicitly when deploying.
- Responses carry request IDs, `no-store` and `nosniff`. Application logs omit product URLs, payloads, tokens and client addresses. `--no-access-log` avoids separate raw-URL Uvicorn logging; configure proxy logs consistently.
- PostgreSQL uses bounded connection pools and query/lock/idle-transaction timeouts. SQLite enables foreign keys and busy timeouts. Readiness checks database availability and migration heads.

Public operation needs environment-specific HTTPS, measured load capacity, egress controls, monitoring and real-data restore drills. Verify source access/display/retention permissions before public operation.

## Reproducible release setup

Install Docker with Compose v2. Keep the two repositories as sibling directories named `priceledger-api` and `priceledger-web`, and check out `v1.0.0` in each. From this API directory, generate a local credentials file once; the command refuses to overwrite an existing file:

```sh
uv run python -c "import secrets; f=open('.env.compose','x'); f.write('POSTGRES_PASSWORD='+secrets.token_hex(32)+'\nPRICELEDGER_ADMIN_TOKEN='+secrets.token_hex(32)+'\n'); f.close()"
docker compose --env-file .env.compose --profile web up --build -d --wait
uv run python scripts/release_smoke.py
```

Open `http://127.0.0.1:8080`. This serves compiled React assets through Nginx, proxies `/api/v1` to the API, and runs PostgreSQL 17 plus one collector. The migration job must finish before API startup; API readiness gates the web and worker. Empty catalogs remain empty until a real lookup. Read the generated admin token locally when using the owner workspace; never paste it into browser configuration, command-line arguments or Git. Omitting the token disables owner operations.

API and web ports bind to loopback; PostgreSQL has no published port. Application containers run as non-root with read-only filesystems, temporary scratch space and dropped capabilities. Docker contexts exclude credentials, databases, dependencies and QA artifacts. The API health check verifies database connectivity and the migration head; worker startup alone does not prove successful collections. Source budgets and freshness settings retain their normal defaults.

If ports 8000/8080 are occupied, add `API_PORT=8001` and/or `WEB_PORT=8081` to `.env.compose`; pass the corresponding API origin to `release_smoke.py`. The web uses its same-origin proxy, so its build needs no local port change.

```sh
docker compose --env-file .env.compose ps
docker compose --env-file .env.compose stop
docker compose --env-file .env.compose --profile web up -d --wait
```

The named database volume survives stopping/recreating containers. Do not use `down --volumes` on retained data. The packaged proxy intentionally does not trust browser-supplied forwarded addresses; browser traffic shares its connection-address quota bucket. Before a public multi-user rollout, configure an explicitly trusted edge and appropriate rate limiting, TLS and monitoring for that environment. Never enable wildcard forwarded-header trust. Access logs are disabled to avoid storing product queries and credentials; the API emits request IDs and bounded route/status/timing logs.

### Backup, restore and upgrades

Use PostgreSQL's consistent custom-format dump; these Compose commands work without binary shell redirection, including in Windows PowerShell. Create a private `../backups` directory first:

```sh
docker compose --env-file .env.compose exec -T db pg_dump -U priceledger -Fc -f /tmp/priceledger-backup.dump priceledger
docker compose --env-file .env.compose cp db:/tmp/priceledger-backup.dump ../backups/priceledger-backup.dump
```

Give each retained backup a unique dated filename. Store encrypted copies outside the host and restrict access: the database includes private source references and review notes. Container `/tmp` is temporary and is not a backup destination.

Restore into a new database first, leaving the current database intact:

```sh
docker compose --env-file .env.compose exec -T db createdb -U priceledger priceledger_restore
docker compose --env-file .env.compose cp ../backups/priceledger-backup.dump db:/tmp/priceledger-restore.dump
docker compose --env-file .env.compose exec -T db pg_restore -U priceledger --exit-on-error -d priceledger_restore /tmp/priceledger-restore.dump
```

Verify the restored migration revision, product/observation counts, permissions, private audit records and a selection of genuine histories in an isolated API deployment before switching the database connection. CI exercises fresh-schema dump/restore and migration consistency; it cannot establish recoverability of your actual retained evidence. Protect and back up `.env.compose` separately.

For upgrades, back up first, stop web/API/worker, check out the selected matching tags, then rebuild with `docker compose --env-file .env.compose --profile web up --build --force-recreate -d --wait`. Recreating web refreshes its upstream connection. Check readiness, run the release smoke and inspect collection health. If rollback is required after a schema-changing upgrade, restore the pre-upgrade backup to a separate database and run the matching application tag; do not blindly downgrade a populated database. Release 1.0.0 keeps migration head `9c0ff0799756` unchanged from the previous phase.

## Development and verification

```sh
uv run ruff check app tests scripts
uv run ruff format --check app tests scripts
uv run pytest -q
uv run alembic check
```

Unit tests use isolated databases and controlled source responses. Fixtures never seed the application. CI also verifies PostgreSQL migrations, concurrency, leases, backfills, streamed matching, report reviews and shop registration/quote publication races. For local PostgreSQL checks, configure a **disposable** PostgreSQL database, migrate it and run `uv run pytest -q tests/test_postgres.py tests/test_report_concurrency.py tests/test_offline_concurrency.py`. Do not use the application database: integration tests create test records.

Frontend browser checks combine intercepted contracts with genuine previously looked-up listings. Large suites use a separate database and temporary quota overrides, not reset application budgets/cooldowns.

| Modules | Responsibility |
| --- | --- |
| `retail`, `public_http`, `iqoo` | Safe fetching and source adapters |
| `lookup`, `comparison`, `discovery`, `identity` | Discovery and verified product matching |
| `evidence`, `catalog` | Bounded reads, source policy and public history/coverage |
| `insights`, `analysis` | Recorded statistics and deal assessment |
| `collection`, `coordination`, `worker` | Durable budgets, leases and refresh jobs |
| `ingestion`, `history_imports` | Validated observations and reviewed archives |
| `operations` | Authenticated read-only collection health and paginated diagnostics |
| `reports` | Public issue receipts, immutable evidence snapshots and private review audits |
| `product_comparison` | Bounded selection reads with independent exact-variant offers and source permissions |
| `offline` | Documented shops, immutable draft quotes, publication/withdrawal receipts and city-specific sampled ranges |
| `http_safety`, `limits`, `auth` | Request safeguards and administrative access |

Both repositories remain private. README is the only repository documentation; research, sprint notes and QA artifacts remain local. Databases, credentials, environments and generated output are ignored by Git.
