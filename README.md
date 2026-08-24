# E-commerce Scraper

API-first service that accepts a public e-commerce URL, discovers its category hierarchy, and returns a reproducible random sample of up to 10 normalized products per category.

> This is an MVP discovery engine, not a promise that every website can be scraped without an adapter. It only reads publicly accessible pages and APIs. It does not bypass logins, CAPTCHAs, paywalls, or access controls.

## What works now

- Shopify capability probing instead of theme-signature assumptions
- Shopify collection/product JSON extraction with sitemap/HTML fallback
- Headless Shopify detection when public catalogue APIs are exposed
- Magento/Adobe Commerce GraphQL category and product extraction
- Headless Magento PWA/Venia detection with public GraphQL capability probing
- Storefront-visible, sitemap-only and API-only collection classification
- WooCommerce detection and public Store API extraction
- Generic navigation and mega-menu hierarchy discovery
- Recursive `robots.txt`, sitemap index, sitemap and gzip sitemap processing
- Product and pagination link discovery on category pages
- Static-asset and sitemap image filtering before product extraction
- Schema.org `Product`, `Offer`, `BreadcrumbList` and `AggregateRating` extraction
- DOM fallback with minimum product-evidence checks for incomplete structured data
- Variant, price, currency, stock, image, rating and specification normalization
- Seeded random sampling of up to 10 products per category
- URL canonicalization, redirect validation, DNS checks and SSRF protection
- Per-domain concurrency limits, request spacing, jitter and adaptive backoff
- `robots.txt` allow/disallow, crawl-delay and request-rate enforcement
- `Retry-After` and common rate-limit header handling with bounded retries
- Cloudflare, Akamai, DataDome, PerimeterX and Imperva challenge detection
- Access-event reporting and a circuit breaker for repeated target blocks
- Asynchronous background jobs with status and result endpoints
- Docker and GitHub Actions configuration

## Architecture

```text
POST /api/v1/jobs
        |
        v
URL validation + safe HTTP client
        |
        v
Platform + capability probe
   |              |              |              |
Shopify JSON  Shopify fallback  Magento API   Generic
   |              |              |              |
Public APIs   Sitemap + HTML   GraphQL API   Navigation + sitemaps
   |              |              |              |
   +--------------+--------------+--------------+
              v
     Category/product mapping
              |
              v
  Deterministic random sampling
              |
              v
   JSON-LD -> DOM extraction
              |
              v
     Normalized JSON result
```

## Quick start

### Docker

```bash
cp .env.example .env
docker compose up --build
```

Open [http://localhost:8000/docs](http://localhost:8000/docs).

### Local Python

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
uvicorn ecommerce_scraper.main:app --reload
```

Python 3.11 or newer is required.

## API usage

Create a job:

```bash
curl -X POST http://localhost:8000/api/v1/jobs \
  -H 'Content-Type: application/json' \
  -d '{
    "url": "https://example-store.com",
    "products_per_category": 10,
    "seed": "catalog-audit-2026"
  }'
```

Response:

```json
{
  "id": "dfd88f07c82b4d2f909b744b41d5d329",
  "state": "queued",
  "status_url": "/api/v1/jobs/dfd88f07c82b4d2f909b744b41d5d329"
}
```

Check status and obtain the result:

```bash
curl http://localhost:8000/api/v1/jobs/JOB_ID
curl http://localhost:8000/api/v1/jobs/JOB_ID/result
```

## Request options

| Field | Default | Description |
|---|---:|---|
| `url` | required | Store homepage or storefront URL |
| `products_per_category` | `10` | Random sample size, from 1 to 50 |
| `seed` | generated from URL | Optional stable seed for reproducible samples |
| `max_categories` | `500` | Safety limit for category processing |
| `max_sitemap_urls` | `50000` | Safety limit for discovered sitemap URLs |
| `max_pages_per_category` | `25` | Pagination limit per generic category |
| `include_api_only_collections` | `false` | Include public Shopify collections absent from navigation and collection sitemaps |

## Output fields

Every product can contain:

- Name and canonical URL
- Full category path
- Clean and HTML descriptions
- Brand, SKU, MPN and GTIN
- Price, compare-at price and currency
- Availability
- Images and videos
- Rating and review count
- Product specifications
- Variants and option values
- Extraction sources and confidence score

The result reports the selected `extraction_strategy` (`shopify_json`,
`shopify_sitemap`, `woocommerce_api`, `magento_graphql` or `generic_html`). Each
category also has a `visibility` value: `navigation`, `sitemap`, `api_only`,
`platform_api` or `synthetic`.

Every completed result also includes an `access_report` with request and retry totals,
rate-limit and block counts, the effective request interval, circuit state and up to 100
recent target-access events. Failed jobs caused by a target rate limit, challenge, access
denial or `robots.txt` rule expose the same report on the job resource.

## Target access controls

The defaults are intentionally conservative and can be tuned through environment variables:

| Setting | Default | Behavior |
|---|---:|---|
| `TARGET_REQUESTS_PER_SECOND` | `1.0` | Baseline request-start rate for each origin |
| `TARGET_MAX_CONCURRENCY_PER_HOST` | `2` | Maximum simultaneous requests to one origin |
| `TARGET_JITTER_SECONDS` | `0.25` | Random extra delay between request starts |
| `TARGET_MAX_RETRIES` | `3` | Retry limit for `429`, `502`, `503` and `504` |
| `TARGET_BACKOFF_BASE_SECONDS` | `1` | Initial exponential-backoff delay |
| `TARGET_BACKOFF_CAP_SECONDS` | `30` | Maximum local backoff and adaptive interval |
| `TARGET_MAX_RETRY_AFTER_SECONDS` | `120` | Fail instead of waiting beyond this server delay |
| `TARGET_BLOCK_THRESHOLD` | `3` | Repeated block signals needed to open the circuit |
| `TARGET_CIRCUIT_BREAK_SECONDS` | `300` | Circuit cool-down period |
| `ROBOTS_OBEY` | `true` | Enforce `robots.txt` rules and pacing directives |
| `HTTP_VALIDATE_DNS` | `true` | Reject hostnames resolving to private/reserved IPs; disable only behind a trusted egress proxy that performs equivalent SSRF filtering |

A response identified as a bot challenge or access denial is not retried. The job stops
and reports the detected provider and URL. The scraper does not solve CAPTCHAs, spoof
browser fingerprints, rotate proxies, or otherwise evade the target's access controls.
Direct private IPs and local hostnames remain invalid regardless of `HTTP_VALIDATE_DNS`.

## Project layout

```text
src/ecommerce_scraper/
├── adapters/
│   ├── generic.py
│   ├── magento.py  # GraphQL probe + categories, products and variants
│   ├── shopify.py  # probe + JSON router + sitemap/HTML fallback
│   └── woocommerce.py
├── api.py
├── config.py
├── discovery.py
├── extractors.py
├── http_client.py
├── jobs.py
├── models.py
├── platforms.py
├── sampling.py
├── security.py
└── service.py
```

## Accuracy boundaries

- “All categories” means categories discoverable through public navigation, sitemaps or supported public platform APIs.
- Generic category-to-product mapping is based on category pages and pagination. Sites that only load products through private APIs need a dedicated adapter.
- Magento/Adobe Commerce stores are supported when their public storefront GraphQL exposes `storeConfig`, `categoryList` and `products`. Schema customizations can still require a store-specific query profile.
- Shopify collections are flat in the public JSON API; navigation paths are used when available to recover hierarchy.
- API-only Shopify collections are excluded by default when navigation or collection-sitemap visibility evidence exists. Set `include_api_only_collections=true` for catalogue audits.
- If Shopify JSON is disabled, the worker discovers collections through navigation and sitemaps, extracts public product links from collection HTML, then normalizes JSON-LD/DOM product data.
- A JavaScript-only collection with no public catalogue JSON or server-rendered product links is reported as requiring a store-specific renderer; the service does not disguise browser automation or bypass a challenge.
- An in-memory job manager is used in this MVP. Replace it with Redis/PostgreSQL before running multiple API replicas.
- Browser-rendered pages are the next adapter layer; the current MVP deliberately starts with cheaper HTTP and structured-data extraction.

## Next milestones

1. Rate-controlled browser renderer for authorized JavaScript-only category pages
2. Per-domain adapter registry for non-standard/headless storefront APIs
3. BigCommerce adapter and store-specific Magento query profiles
4. Redis-backed durable job queue and resumable collection checkpoints
5. PostgreSQL run history and CSV/XLSX export
6. Next.js dashboard with progress and download controls

## Responsible use

Run the service only on websites you are authorized to access. Respect terms of service, `robots.txt`, copyright, privacy obligations, crawl delays and applicable laws. Do not use it to bypass authentication or technical access controls.
