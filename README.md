# SVADEZI Live Pricing

Computes and pushes **live selling price + compare-at price** for every product
variant on the SVADEZI Shopify store, daily, using live metal rates and the
SVADEZI pricing formula. Runs on GitHub Actions (automatic + manual).

> Public repo — **no secrets in code.** Shopify token & metals API key are
> GitHub Secrets / local env only.

---

## The formula (single source of truth)

```
metal     = grams × metal_rate_per_g          # karat- or silver-specific
making    = grams × making_charge_per_g        # gold ₹1,500/g · silver ₹800/g
finishing = ₹800 (flat)
diamond   = total_ct × diamond_rate
              lab .................. ₹35,000/ct
              natural ≤ 0.15 ct .... ₹1,35,000/ct
              natural > 0.15 ct .... ₹1,85,000/ct
SELLING   = round_to_₹10( (metal + making + finishing + diamond) × 1.03 )
COMPARE   = round_to_₹10( SELLING × 1.50 )
```

Per-variant inputs come from product data:
- **grams** → variant metafield `custom.metal_weight_g`
- **total_ct** → product metafield `custom.diamond_weight_ct`
- **metal / karat / stone type** → parsed from the variant option values + title

All tunables live in [`config.json`](config.json) (safe for a public repo).

---

## How price-breakdown stays in sync

Each run writes a **`metal_rate` metaobject** (`handle: current`) holding the live
rates **and every formula constant** (making, finishing, diamond rates, GST,
compare multiplier, rounding step). This metaobject is the **single source of
truth**: the engine computes from it and the theme price-breakdown should read
the same metaobject, so the displayed breakdown always reproduces the exact
pushed price. Field list is in `setup_metaobject.py`.

---

## Setup

### 1. Shopify access token scopes
The custom app's Admin API token needs:
- `read_products`, `write_products` (prices/variants)
- `read_metaobjects`, `write_metaobjects` (rate snapshot)
- `read_metaobject_definitions`, `write_metaobject_definitions` (first-time, to create the definition)

### 2. Create the metaobject definition (once)
```bash
export SHOPIFY_STORE=... SHOPIFY_ACCESS_TOKEN=...
python setup_metaobject.py
```

### 3. GitHub Secrets (Settings → Secrets and variables → Actions)
| Secret | Value |
|---|---|
| `SHOPIFY_STORE` | `svadezi.myshopify.com` |
| `SHOPIFY_ACCESS_TOKEN` | `shpat_…` |
| `METALS_API_KEY` | `metals.dev` key (provider `metalsdev`) — **free tier 100 req/month**, fetched once per run |
| `METALS_API_KEY_2` | optional fallback key |

> **Multi-key fallback:** the engine tries `METALS_API_KEY` first; if it's
> quota-exceeded/fails it falls back to `METALS_API_KEY_2` (and `_3`…`_5`).
> `METALS_API_KEY` may also be a comma-separated list. **Keys are used
> server-side only — never in the theme/browser.**

---

## Running

### Automatic (daily)
[`.github/workflows/auto-update.yml`](.github/workflows/auto-update.yml) runs at
**00:00 IST** (`cron: "30 18 * * *"` UTC). Fetches live rates from the provider.
**Change the time:** edit that cron (IST = UTC + 5:30). Can also be triggered
from the Actions tab (with optional dry-run).

### Manual (set your own rate)
[`.github/workflows/manual-update.yml`](.github/workflows/manual-update.yml) —
Actions tab → "Manual Price Update" → enter `gold_24k`, `silver_999`, run.
Use when the API is down/over-quota or to pin a specific rate.

### Local
```bash
pip install -r requirements.txt
cp .env.example .env   # fill in, then: export $(grep -v '^#' .env | xargs)

python price_engine.py --dry-run                       # live rates, compute only
python price_engine.py --gold-24k 7200 --silver-999 95 # manual rates, push
python price_engine.py --handle svadezi-cc-er1080-p12  # one product (testing)
python price_engine.py --limit 5 --dry-run             # first 5 products
```

---

## Swapping the metals API later
1. Add `providers/<name>.py` exposing `fetch_rates(api_key, currency) -> {"gold_24k", "silver_999", "source"}`.
2. Register it in `providers/__init__.py` → `PROVIDERS`.
3. Set `"provider": "<name>"` in `config.json`.
No other code changes. The engine only needs pure 24k gold/g and pure silver/g.

---

## Notes
- **Bulk + parallel:** prices are written with `productVariantsBulkUpdate`
  (≤250 variants/call) across products concurrently (`max_workers`), so a full
  run is fast.
- **Rounding** is half-up to `rounding_step` (₹10) on both selling and compare-at.
- `zero_carat_behavior` (config) — `compute` prices 0-carat items normally;
  `skip` leaves them untouched.
- `only_active_products` skips archived products.
