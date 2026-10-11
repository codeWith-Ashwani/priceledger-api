# PriceLedger API

Private FastAPI backend for real India product lookup and durable, sourced price observations. Python 3.12+, SQLAlchemy/Alembic and PostgreSQL; SQLite runs locally without additional services.

## Run locally

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```sh
uv sync --locked
uv run alembic upgrade head
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

Start with an empty catalog. No sample seed is included. Demo records from older development versions are excluded from every public product/history endpoint.

Optional settings are in `.env.example`. Copy it to `.env` to configure your local environment. For PostgreSQL, use `PRICELEDGER_DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST:5432/priceledger`, then run migrations. Never commit credentials. API reference: http://127.0.0.1:8000/docs.

## Real product lookup

`POST /api/v1/lookup` accepts `{"query":"PRODUCT_URL_OR_BRAND_MODEL_VARIANT"}`.

Amazon India, Flipkart and Meesho have dedicated URL rules. Other public ecommerce URLs use a generic reader when the page exposes one unambiguous INR Product/Offer in structured data. Full Flipkart links retain `pid` to identify the actual variant and discard tracking parameters. Recognized short links are resolved only through allowlisted retailer hosts. Readable product pages supply actual INR item prices, explicit delivery charges, availability, identity and source. A successful lookup creates a real product and timestamped observation and returns its `product_id`. Recent observations are cached for 15 minutes by default. Link requests never substitute catalog samples.

A brand/model query searches retailer pages concurrently and returns candidate listings and per-retailer status. The user selects the exact colour, size, storage and pack before a price is recorded. Search snippets are not historical price evidence. After selecting or pasting a listing, the frontend automatically starts a cross-store comparison. Similar names alone are not enough to merge prices.

Public page access varies. Some pages need JavaScript, refuse automated requests or change markup; blocked or unreadable pages return a specific error and record no price. The adapter does not log in, solve CAPTCHAs, rotate proxies or guess a price. It reads a single explicit INR offer or Amazon's main price-to-pay, and rejects ambiguous offers, wrong variants and invalid amounts. Tests use controlled contracts, while the local browser checks also exercise the owner's supplied real product URL.

These are public-page observations in a private development prototype, not licensed historical feeds. Before public operation, review retailer/source permissions and obtain appropriate data access. Sources can be disabled by revoking their display/history flags or expiry; ingestion, lookup caching, display and collection enforce those settings. Source activation flags are technical configuration, not a representation that a retailer granted a license.

## Learned store discovery and listed fees

Successful readable Shopify product lookups can register additional stores automatically. Literal same-product Shopify metadata must identify the selected SKU; only genuinely observed brands are learned. Cross-store scans check all enabled learned stores for the requested brand alongside configured adapters by default. Name searches include all enabled learned stores. `PRICELEDGER_DISCOVERY_EXTRA_STORE_LIMIT=-1` is the new default and removes the extra-store count limit; zero disables additional searches, and a positive value is an optional operator-imposed request limit. Update older `.env` files that explicitly set this to 4 or 8 to use -1. The registry retains at most 64 stores and 32 brands per store as an independent catalog capacity control. Only fixed public `/search?q=...&type=product` pages are searched, and candidate prices require separate product-page verification. Existing source permission, request budget, cooldown, redirect and public-network checks apply.

The owner workspace exposes learned stores through authenticated `GET /api/v1/admin/discovery-stores` and `POST /api/v1/admin/discovery-stores/{retailer_id}/status` with `{"enabled":false}` or `true`. Paused stores stay paused after later lookups. Resuming discovery never restores revoked source permissions. Pausing searches leaves existing permitted price evidence available.

Apple comparisons also start with the public iPlanet and Imagine Shopify search origins, without requiring a prior reseller product submission. These origins share the additional-store limit with learned stores, retain existing generic retailer IDs and respect persisted source restrictions and operator pauses. Only search origins are configured; models, product URLs, prices and stock come from fetched pages. Brand-scoped phrase searches exclude unrequested model qualifiers and accessories. Literal catalog vendors support titles that omit their brand; each candidate still requires independent product-page verification.

Closest readable variants are checked first within the two-product-per-store budget. Explicit AppleCare/Protect+ and other bundles are excluded from plain-product comparisons, including when they share a phone barcode. Lavender, Sage and Mist Blue are retained as variant colours. A single Shopify offer can supply its selected variant URL; an omitted SKU is recovered only from matching literal variant metadata and agreeing INR prices. Conflicting variants, identifiers and prices are rejected. Previously empty identifiers may be enriched without changing known identities. Out-of-stock listings remain labelled and cannot win the available-price ranking. Scan version changes invalidate earlier cached discovery results; exact-variant history remains separate.

Comparisons return every verified matching catalog offer without the former 100-candidate cutoff. Database reads and parameter lists are batched without truncating result coverage. The frontend renders the full returned offer set and ranks across all eligible available quotes; there is no three- or four-card maximum. Discovery cache version 11 invalidates old truncated scans. Network discovery still checks up to two selected candidates per store, with three concurrent store workers and normal collection limits. This shows all verified connected-store results, not every listing or retailer on the internet. Live page checks use unique internal event IDs so two checks within one system-clock tick do not overwrite or reject each other's price evidence. Imported provider event IDs retain their existing deduplication semantics.

Shopify `ProductGroup` variants can be resolved using an explicit variant URL or an unambiguous selected purchase form. Variant SKU and INR price must agree with literal Shopify metadata before overriding stale parent-level schema prices. Other products, conflicting variant prices and ambiguous selections are rejected. No script is executed.

Offer responses include nullable `listed_fees` and `known_subtotal_paise`. Current automatic extra-fee extraction recognizes Flipkart's explicit product-ID-bound Protect Promise Fee; other checkout charges remain unconfirmed. Each fee retains its label, integer paise amount and evidence kind. Known subtotal adds item price, known shipping and observed fees; unknown shipping keeps the subtotal null. A missing fee list does not assert fee-free checkout. Fresh missing evidence clears old current fees; observation history retains dated fee evidence. Conditional bank/coupon offers are excluded.

Existing `total_paise`, `best_price_paise`, delivered history and assessments retain their **item + shipping** meaning and exclude listed extras, preserving historical comparability. The React cards and exact-variant headline use known subtotal where requested. Apply migration `79f240a1ce38` with `uv run alembic upgrade head` before starting this version. No provider credentials or new dependencies are required. Learning readable stores does not establish universal discovery, full checkout prices or historical data access.

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

A shared database budget limits each source to 500 reserved fetch operations per UTC day by default. URL lookup and collector checks reserve one operation, name searches reserve one per store, and comparisons conservatively reserve three per store (search plus up to two candidate checks). Unused reservations are not refunded; redirects are bounded by the HTTP adapter separately. Cached prices do not consume source budget or create observations. Client quotas are also persisted and shared across API processes.

History starts at the first recorded observation. Launch dates and earlier prices remain unavailable unless actual historical evidence is imported. Missing shipping is never treated as free. Offline prices require dated, exact-variant shop evidence; there is no universal offline-price feed or fabricated percentage estimate.

- `GET /api/v1/products?q=&category=&limit=24&offset=0`: stored real variants.
- `GET /api/v1/products/{id}`: offers, retailer links, online/offline channel, timestamps, price components and coverage.
- `GET /api/v1/products/{id}/history?days=90&basis=item`: source-labelled timeline; `basis=delivered` requires known shipping. Maximum range is 36,500 days.
- `GET /api/v1/products/{id}/assessment`: comparable delivered-total reference and coverage.
- `GET /api/v1/products/{id}/coverage`: aggregate evidence counts/India days, first/latest observations, collection state, last check and next eligible time.
- `GET /api/v1/categories`: stored categories.
- `GET /api/v1/health`: service health.
- `GET /api/v1/ready`: verifies database connectivity and the exact current migration heads; returns 503 when unavailable or outdated. Liveness remains independent of database availability.
- `POST /api/v1/ingest`: administrative evidence import, disabled until `PRICELEDGER_ADMIN_TOKEN` is set; requires `X-Admin-Token`. Never expose this secret to the frontend.

The original `/ingest` contract requires a permitted source, mapped exact offer belonging to that source, matching attributes, INR integer paise, aware timestamp and unique source event ID. Identical repeated events are idempotent; conflicting reuse is rejected. Backfills preserve newer current prices. `evidence_kind` accepts `recorded` (default), `page_check` or `historical_import`; it labels evidence and does not validate a provider's claim. New observations retain their submitted variant attributes. Public-page checks also retain seller/condition/region/availability at that check; older records retain an unknown snapshot rather than inventing past identity. Independent archive-to-offer mappings use the reviewed historical import workflow below; automatic provider adapters remain pending.

Assessments use India calendar days for the median of daily delivered totals over 30 days ending seven days ago, with at least 20 observed days. Recorded lows cover a stated offer and period. A rise/drop pattern does not prove manipulation. Item-price charts and delivered-total assessments are labelled separately.

Client quotas use atomic database upserts across processes, with separate lookup (12/minute), comparison (12/minute), catalog/assessment reads (120/minute) and authenticated administration (60/minute) buckets. Each fixed 60-second window starts at its first accepted request; bursts across window boundaries remain possible. Cached lookups still consume client quota. Denials return 429 and the remaining `Retry-After`; database failures fail closed with 503. Only a SHA-256 digest of the connection's client address is persisted, not raw addresses or credentials; this is pseudonymization, not anonymity. Forwarded headers are ignored by the application. Behind a proxy, configure Uvicorn's trusted proxy IPs explicitly, never a blanket trust of arbitrary clients. IP quotas are not per-account quotas and users behind one NAT share a bucket.

All retailer and generic URL reads use DNS-pinned HTTPS connections: every resolved address must be public, the socket connects to that checked IP, TLS verifies the original hostname, and each redirect is revalidated against the retailer. IPv4 is preferred where local IPv6 routing is unavailable; connection failures can try up to three validated addresses within the original deadline. TLS failures and HTTP refusals never trigger address fallback. Credentials, private/reserved addresses, unusual ports and unsupported response types/encodings are rejected. Responses request identity encoding and cap HTML at 8 MB, with socket timeouts and a 35-second deadline checked between fetch steps/chunks. OS DNS resolution and response-header reading also depend on OS/socket behavior; enforce network egress and upstream deadlines when deploying. No environment proxy is used. This transport does not bypass retailer refusals.

## Verify

```sh
uv run pytest -q
uv run alembic check
```

GitHub CI checks unit contracts and PostgreSQL 17 migrations, lookup persistence, backfills and assessment roundtrips without external retailer dependencies.

React's full browser suite runs many scenarios through one localhost client identity. For its temporary integration API servers, set `PRICELEDGER_READ_REQUESTS_PER_MINUTE=1000` and `PRICELEDGER_LOOKUP_REQUESTS_PER_MINUTE=120` so UI contracts do not exhaust one shared test bucket. The backend suite separately verifies the default quota behavior and concurrent enforcement. These overrides are test-run settings, not changes to the application defaults.

Frontend: https://github.com/codeWith-Ashwani/priceledger-web. Only README documentation is published; research and sprints remain local. Both repositories remain private until the owner explicitly changes visibility.

## One-link cross-store comparison

iQOO official-store discovery is available for iQOO products when `"iqoo"` is included in `PRICELEDGER_DISCOVERY_RETAILERS` (now included in the default). It reads the public catalog and one matching model's SKU selector, then verifies at most two selected product pages. This reserves four source fetch operations per comparison, or two for name discovery. The bounded Nuxt metadata reader resolves literal SKU identifiers without executing website JavaScript. Selected `skuId` stays in the URL and must agree with the page's structured Product SKU; a redirected/default colour cannot silently replace the requested one.

Phone titles with explicit RAM/storage can be identified for known phone brands even without the word “phone”; accessories, laptops and tablets are excluded from that heuristic. Multiword colours such as Solar Flame are retained. Search queries use brand/model rather than marketing descriptions, and candidate ranking uses visible card RAM/storage/colour before the two-page verification limit. Charging/connectivity terms are omitted only from discovery queries, not exact identity checks. A model-name difference in a charging label requires review unless a shared manufacturer identifier is available. Amazon condition metadata is accepted only for the selected ASIN with one explicit buying-option type; otherwise condition stays unknown. Existing price observations and historical variant snapshots are not rewritten by metadata enrichment.

Discovery results are versioned so previous failed marketing-title searches are not reused after this fix. Access refusals, cooldowns and unreadable searches retain their actual reasons; a failed search is never evidence that a retailer does not stock a product.

Discovery is configured with `PRICELEDGER_DISCOVERY_RETAILERS` (a JSON array; default `["amazon","flipkart","meesho","croma","reliance","iqoo","sony","boat"]`). Sony Center and boAt discovery run only for their respective brands; iQOO remains brand gated. Known fashion, grocery and beauty categories skip electronics stores. Shopify search metadata supplies candidate URLs only; prices must come from checked product offers. Selected variant IDs, SKU, stock and images stay tied to the requested option. A sold-out selected cart option overrides a conflicting generic stock claim. The registry describes implemented adapters, not guaranteed retailer access. JavaScript-only search, refusals, incomplete identifiers and variant differences can leave fewer than three stores.

Previously tracked readable products from other public retailer domains can also join through exact identity matching, even when that domain has no discovery adapter. Candidate catalog matching checks at most 100 recent same-brand listings. Discovering an untracked retailer still requires a search/feed adapter; the API does not search the entire Internet or generate missing offers.

The default frontend view compares **same brand and model**, allowing labelled colour, memory, size and part-number differences. The scan returns these as `model_offers` with `match_scope: "model"` and known `differences`. Model generations, Pro/Lite suffixes, connectivity, accessories and used/refurbished conditions remain distinct. Optional charging-wattage wording can differ in model scope. Model alternatives never create `ProductMatch` links or join exact-variant history, assessments or catalog best prices. **Exact variant** hides model alternatives and retains the stricter identity rules below. Model candidates are recomputed against current source permissions even when a scan is cached.

Complementary Product schemas can be consolidated only when shared identifiers and nonconflicting fields establish one product; conflicting offers remain rejected. A named product without a readable offer can start cross-store discovery with a null price and unknown availability. No zero-price substitute is recorded. Legacy headphone misclassification and omitted generation qualifiers are corrected for matching without rewriting historical observation snapshots. Duplicate search aliases resolving to one listing produce one scan item and observation.

An October 2026 exploratory audit submitted 12 real links from 12 retailer domains. Four input pages exposed readable prices, two exposed identity only, and six hit access/cooldown or missing-page limits. Sony WH-CH520 expanded to four stores; original boAt Airdopes 141 expanded to two. These are bounded observed results, not a universal store/price guarantee. Search-index prices can be stale and are never used as current or historical evidence. Detailed audit data and screenshots remain local.

Item prices exclude conditional bank/coupon discounts. Item plus known shipping does not establish every checkout/protection fee or pincode-specific final total; confirm those at the retailer. No offline market price is generated without dated shop evidence.

Offer detail includes the original listing's title, image and nullable product rating (`value`, reported `best` scale, optional feedback `count`, and check timestamp). Ratings come from that exact selected product's structured metadata or an identified Amazon product rating element. Missing ratings remain null, and a fresh check without one clears the previous value. Ratings are current metadata, not an invented rating history. Apply the latest migration before starting the API; it adds the nullable rating column without changing existing price evidence.

`POST /api/v1/products/{id}/compare` searches the other connected retailers automatically and verifies up to two readable candidate listings per retailer. Comparison scans are cached for 15 minutes. Database leases prevent concurrent requests for one product from repeating retailer fan-out across API processes; different products can scan independently. A busy comparison returns 409 with `Retry-After`. Leases expire after ten minutes by default and are released on completion/failure; an expired/reclaimed owner cannot publish a comparison cache or release the newer owner's lease. Genuine listing observations already saved during discovery remain evidence even if the final comparison lease expires. A retailer with no readable match is reported explicitly. Adding more discovery retailers requires their search/feed adapter; accepting a source URL is separate from guaranteeing coverage of every other retailer.

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

## Backend safeguards and operations

Mutating requests are buffered within a configurable 1.5 MB ceiling and 15-second body deadline before parsing. Streaming/chunked requests obey the same limit. Compressed bodies, inconsistent lengths, non-JSON content, duplicate JSON fields, non-finite numbers and invalid Unicode are rejected. Administrative credentials are checked before reading files, then authenticated requests consume the shared admin quota. The reviewed import file/row limits remain separately enforced. `/ingest` also requires strict integer paise and booleans, bounded finite attributes and known fields; exact typed variant matching prevents booleans from matching numeric variant IDs. Current quotes update conditionally against persisted timestamps, preventing stale writers from overwriting a newer quote.

History responses cap both observations and failed checks at 10,000 each by default. An oversized range returns 422 with a request to choose a shorter period; evidence is never silently truncated. Aggregate/paginated large-history APIs remain future work. PostgreSQL connections use bounded pools (five connections plus five overflow per process, ten-second pool wait), five-second connect/lock timeouts, 30-second statement timeouts and a 60-second idle-transaction timeout. SQLite uses a five-second busy timeout and foreign-key enforcement. Tune these settings with measured workloads rather than treating them as national-scale capacity guarantees.

Every application response carries a fresh server-generated `X-Request-ID`, `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`. Application request logs contain request ID, method, route template, status and elapsed time; they omit query strings, product IDs, client addresses, tokens, file contents and exception values. Unexpected/database failures return sanitized messages (500/503); integrity conflicts return 409. Validation errors omit submitted values. CORS exposes request IDs and retry delays to approved origins. `--no-access-log` avoids Uvicorn's separate raw-URL access logs; configure any proxy logging with the same privacy requirements. Infrastructure preflight/error responses outside the application may have separate logging/header policies.

Run bounded quota housekeeping periodically using your service/job infrastructure:

```sh
uv run python -m app.maintenance --max-rows 1000
```

Each pass removes at most the requested number of quota windows expired more than one day ago, rechecking expiry before deletion. It leaves active quotas, observations and import audit records intact. This command is not scheduled automatically. Run `uv run alembic upgrade head` before starting the updated API; use `/ready` for readiness and `/health` for liveness. CI verifies lint, model/migration consistency, unit contracts and PostgreSQL concurrency, lease fencing, stale quote protection and configured timeouts. Load measurements, deployment-wide egress controls, backups/restore and account-aware quotas remain production work.
