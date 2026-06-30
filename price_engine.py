#!/usr/bin/env python3
"""
SVADEZI Live Price Engine
=========================
Computes and pushes live selling price + compare-at price for every product
variant on the Shopify store, using live metal rates (goldapi.io by default)
and the SVADEZI pricing formula.

Formula (per variant):
    metal     = grams * metal_rate_per_g            # karat/silver specific
    making    = grams * making_charge_per_g[gold|silver]
    finishing = finishing_charge (flat)
    diamond   = total_ct * diamond_rate(stone_type, carat)
    price     = round_to_step( (metal+making+finishing+diamond) * gst_mult )
    compare   = round_to_step( price * compare_at_mult )

Data sources (per product/variant):
    grams      -> variant metafield custom.metal_weight_g
    total_ct   -> product metafield custom.diamond_weight_ct
    metal/karat, stone type -> parsed from the variant's option values + title

Modes:
    Automatic : fetch live rates from the provider (config "provider").
    Manual    : pass --gold-24k / --silver-999 to override (no API call).
    Dry-run   : --dry-run computes + logs, pushes nothing.

Secrets (env / GitHub Secrets):
    SHOPIFY_STORE, SHOPIFY_ACCESS_TOKEN, METALS_API_KEY
"""

import argparse
import json
import logging
import math
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    print("ERROR: pip install -r requirements.txt"); sys.exit(1)

from providers import get_provider

ROOT = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("price-engine")


def load_config(path):
    with open(path) as f:
        return json.load(f)


def collect_api_keys():
    """Gather metals API keys for fallback, in order:
    METALS_API_KEY (may be comma-separated), then METALS_API_KEY_2..5."""
    keys = []
    primary = os.environ.get("METALS_API_KEY", "")
    keys += [k.strip() for k in primary.split(",")]
    for n in range(2, 6):
        v = os.environ.get(f"METALS_API_KEY_{n}", "")
        if v:
            keys.append(v.strip())
    seen, out = set(), []
    for k in keys:
        if k and k not in seen:
            seen.add(k); out.append(k)
    return out


def round_to_step(value, step):
    """Round half-up to the nearest `step` (e.g. nearest 10)."""
    if step <= 0:
        return round(value, 2)
    return int(math.floor(value / step + 0.5)) * step


# ---------------------------------------------------------------------------
# Metal / stone parsing from option values + title
# ---------------------------------------------------------------------------
def detect_metal(text):
    """Return ('silver'|'gold', purity_label) from a lowercased blob."""
    t = text.lower()
    if "silver" in t or "925" in t or "sterling" in t:
        return "silver", "925"
    # karat: handle '18k', '18kt', '18 k', '18 kt', 'gold 18k'
    for k in ("22", "18", "14", "9"):
        if re.search(rf"\b{k}\s*k(t)?\b", t):
            return "gold", f"{k}K"
    if "gold" in t:
        return "gold", "18K"  # default karat if gold but unspecified
    return "gold", "18K"      # safe default


def detect_stone_type(text, default):
    t = text.lower()
    if "lab" in t or "grown" in t or "moissanite" in t:
        return "lab"
    if "natural" in t:
        return "natural"
    return default


def diamond_rate_for(stone_type, carat, cfg):
    d = cfg["diamond_rate"]
    if stone_type == "natural":
        thr = d["natural_threshold_ct"]
        # basis "total" uses total carat; "per_stone" would need a stone-count field (not present yet)
        return d["natural_large"] if carat > thr else d["natural_small"]
    return d["lab"]


# ---------------------------------------------------------------------------
# Price computation
# ---------------------------------------------------------------------------
def compute_price(grams, carat, metal, purity, stone_type, rates, cfg):
    if metal == "silver":
        rate = rates["silver_999"] * cfg["silver_purity"]
        making_per_g = cfg["making_charge_per_g"]["silver"]
    else:
        rate = rates["gold_24k"] * cfg["karat_purity"].get(purity, 0.75)
        making_per_g = cfg["making_charge_per_g"]["gold"]
    rate *= cfg.get("metal_rate_multiplier", 1.0)

    metal_value = grams * rate
    making = grams * making_per_g
    finishing = cfg["finishing_charge"]
    diamond = carat * diamond_rate_for(stone_type, carat, cfg)

    subtotal = metal_value + making + finishing + diamond
    price = round_to_step(subtotal * cfg["gst_multiplier"], cfg["rounding_step"])
    compare = round_to_step(price * cfg["compare_at_multiplier"], cfg["rounding_step"])
    return price, compare


# ---------------------------------------------------------------------------
# Shopify GraphQL
# ---------------------------------------------------------------------------
class Shopify:
    def __init__(self, store, token, api):
        store = store.replace("https://", "").replace("http://", "").rstrip("/")
        if not store.endswith(".myshopify.com"):
            store += ".myshopify.com"
        self.url = f"https://{store}/admin/api/{api}/graphql.json"
        self.s = requests.Session()
        self.s.headers.update({"X-Shopify-Access-Token": token, "Content-Type": "application/json"})

    def execute(self, query, variables=None):
        for attempt in range(1, 7):
            r = self.s.post(self.url, json={"query": query, "variables": variables or {}}, timeout=90)
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", 2))); continue
            r.raise_for_status()
            data = r.json()
            if data.get("errors"):
                if any("throttl" in str(e).lower() for e in data["errors"]):
                    time.sleep(2 ** attempt); continue
                raise RuntimeError(f"GraphQL errors: {data['errors']}")
            return data["data"]
        raise RuntimeError("GraphQL retries exhausted")


VARIANTS_QUERY = """
query($cursor: String, $mwNs: String!, $mwKey: String!, $dcNs: String!, $dcKey: String!) {
  productVariants(first: 200, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id sku title
      selectedOptions { name value }
      metalWeight: metafield(namespace: $mwNs, key: $mwKey) { value }
      product {
        id title status handle
        diamondCt: metafield(namespace: $dcNs, key: $dcKey) { value }
      }
    }
  }
}
"""

BULK_UPDATE = """
mutation($pid: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $pid, variants: $variants) {
    userErrors { field message }
  }
}
"""


def fetch_variants(shop, cfg):
    mw = cfg["metafields"]["metal_weight_g"]; dc = cfg["metafields"]["diamond_weight_ct"]
    cursor = None
    out = []
    pages = 0
    while True:
        data = shop.execute(VARIANTS_QUERY, {
            "cursor": cursor, "mwNs": mw["namespace"], "mwKey": mw["key"],
            "dcNs": dc["namespace"], "dcKey": dc["key"],
        })["productVariants"]
        out.extend(data["nodes"])
        pages += 1
        if pages % 10 == 0:
            log.info(f"  fetched {len(out)} variants...")
        if data["pageInfo"]["hasNextPage"]:
            cursor = data["pageInfo"]["endCursor"]
        else:
            break
    log.info(f"Fetched {len(out)} variants total")
    return out


# ---------------------------------------------------------------------------
# Rate metaobject (single source of truth)
# ---------------------------------------------------------------------------
# Full pricing snapshot stored on the metaobject = single source of truth for
# BOTH this engine and the theme price-breakdown, so the displayed breakdown
# always reproduces the exact pushed price.
RATE_FIELDS = [
    "gold_24k", "silver_999",
    "making_gold", "making_silver", "finishing",
    "diamond_lab", "diamond_natural_small", "diamond_natural_large", "natural_threshold_ct",
    "gst_multiplier", "compare_at_multiplier", "rounding_step",
    "silver_purity", "karat_purity", "source", "updated_at",
]


def upsert_rate_metaobject(shop, cfg, rates):
    mo = cfg.get("rate_metaobject", {})
    if not mo.get("enabled"):
        return
    handle = {"type": mo["type"], "handle": mo["handle"]}
    d = cfg["diamond_rate"]
    fields = [
        {"key": "gold_24k", "value": str(round(rates["gold_24k"], 4))},
        {"key": "silver_999", "value": str(round(rates["silver_999"], 4))},
        {"key": "making_gold", "value": str(cfg["making_charge_per_g"]["gold"])},
        {"key": "making_silver", "value": str(cfg["making_charge_per_g"]["silver"])},
        {"key": "finishing", "value": str(cfg["finishing_charge"])},
        {"key": "diamond_lab", "value": str(d["lab"])},
        {"key": "diamond_natural_small", "value": str(d["natural_small"])},
        {"key": "diamond_natural_large", "value": str(d["natural_large"])},
        {"key": "natural_threshold_ct", "value": str(d["natural_threshold_ct"])},
        {"key": "gst_multiplier", "value": str(cfg["gst_multiplier"])},
        {"key": "compare_at_multiplier", "value": str(cfg["compare_at_multiplier"])},
        {"key": "rounding_step", "value": str(cfg["rounding_step"])},
        {"key": "silver_purity", "value": str(cfg["silver_purity"])},
        {"key": "karat_purity", "value": json.dumps(cfg["karat_purity"])},
        {"key": "source", "value": rates.get("source", "manual")},
        {"key": "updated_at", "value": datetime.now(timezone.utc).isoformat()},
    ]
    mut = """
    mutation($handle: MetaobjectHandleInput!, $mo: MetaobjectUpsertInput!) {
      metaobjectUpsert(handle: $handle, metaobject: $mo) {
        metaobject { id handle }
        userErrors { field message code }
      }
    }"""
    try:
        res = shop.execute(mut, {"handle": handle, "mo": {"fields": fields}})["metaobjectUpsert"]
        if res["userErrors"]:
            log.warning(f"Rate metaobject upsert userErrors: {res['userErrors']} "
                        f"(define metaobject type '{mo['type']}' with fields {RATE_FIELDS}, "
                        f"and grant write_metaobjects)")
        else:
            log.info(f"Rate metaobject updated: {res['metaobject']['handle']}")
    except Exception as e:
        log.warning(f"Could not upsert rate metaobject ({e}). Skipping (prices still pushed).")


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------
def run(args):
    cfg = load_config(args.config)
    store = os.environ.get("SHOPIFY_STORE", "")
    token = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")
    if not store or not token:
        log.error("Missing SHOPIFY_STORE / SHOPIFY_ACCESS_TOKEN"); sys.exit(1)

    # ---- resolve rates ----
    if args.gold_24k is not None or args.silver_999 is not None:
        if args.gold_24k is None or args.silver_999 is None:
            log.error("Manual mode needs BOTH --gold-24k and --silver-999"); sys.exit(1)
        rates = {"gold_24k": args.gold_24k, "silver_999": args.silver_999, "source": "manual"}
        log.info(f"MANUAL rates: gold_24k={rates['gold_24k']} silver_999={rates['silver_999']}")
    else:
        provider = get_provider(cfg["provider"])
        keys = collect_api_keys()
        if not keys:
            log.error("No metals API keys (set METALS_API_KEY, optionally METALS_API_KEY_2…)"); sys.exit(1)
        log.info(f"Using {len(keys)} metals API key(s) with fallback")
        rates = provider(keys, cfg.get("currency", "INR"))
        log.info(f"LIVE rates [{rates['source']}]: gold_24k={rates['gold_24k']:.2f} "
                 f"silver_999={rates['silver_999']:.2f}")

    shop = Shopify(store, token, cfg["shopify_api_version"])

    # ---- store rate as single source of truth (best-effort) ----
    if not args.dry_run:
        upsert_rate_metaobject(shop, cfg, rates)

    # ---- fetch + compute ----
    variants = fetch_variants(shop, cfg)
    by_product = {}
    pricing_map = {}   # productId -> { "opt1|opt2|opt3": [variantId, price, compare, grams] }
    skipped_no_grams = skipped_inactive = zero_carat = 0
    only_active = cfg.get("only_active_products", True)

    for v in variants:
        prod = v["product"]
        if only_active and prod.get("status") != "ACTIVE":
            skipped_inactive += 1; continue
        if args.handle and prod["handle"] != args.handle:
            continue

        grams = float((v.get("metalWeight") or {}).get("value") or 0)
        carat = float((prod.get("diamondCt") or {}).get("value") or 0)
        if grams <= 0:
            skipped_no_grams += 1; continue
        if carat == 0:
            zero_carat += 1
            if cfg.get("zero_carat_behavior") == "skip":
                continue

        blob = " ".join([o["value"] for o in v.get("selectedOptions", [])] + [v.get("title", ""), prod.get("title", "")])
        metal, purity = detect_metal(blob)
        stone = detect_stone_type(blob, cfg.get("default_stone_type", "lab"))
        price, compare = compute_price(grams, carat, metal, purity, stone, rates, cfg)

        by_product.setdefault(prod["id"], []).append(
            {"id": v["id"], "price": f"{price}.00", "compareAtPrice": f"{compare}.00"})
        # Full per-variant map for the theme (one metafield bypasses the 250 Liquid/AJAX cap)
        opt_key = "|".join((o.get("value") or "").strip() for o in v.get("selectedOptions", []))
        pricing_map.setdefault(prod["id"], {})[opt_key] = [
            int(v["id"].split("/")[-1]), price, compare, round(grams, 4)]

    total_variants = sum(len(x) for x in by_product.values())
    log.info(f"Computed prices for {total_variants} variants across {len(by_product)} products "
             f"(skipped: {skipped_no_grams} no-grams, {skipped_inactive} inactive; {zero_carat} zero-carat)")

    if args.limit:
        keep = dict(list(by_product.items())[:args.limit])
        by_product = keep
        log.info(f"--limit {args.limit}: restricting to {len(by_product)} products")

    if args.dry_run:
        sample = next(iter(by_product.values()), [])[:3]
        log.info(f"[DRY-RUN] no writes. Sample variant updates: {json.dumps(sample)}")
        return

    # ---- push in parallel, chunked ----
    chunk = cfg.get("bulk_chunk_size", 250)
    ok = err = 0
    lock = threading.Lock()

    def push(pid, variants):
        nonlocal ok, err
        try:
            for i in range(0, len(variants), chunk):
                res = shop.execute(BULK_UPDATE, {"pid": pid, "variants": variants[i:i + chunk]})["productVariantsBulkUpdate"]
                if res["userErrors"]:
                    with lock: err += 1
                    log.error(f"  {pid}: {res['userErrors']}")
                    return
            with lock: ok += 1
        except Exception as e:
            with lock: err += 1
            log.error(f"  {pid}: {e}")

    with ThreadPoolExecutor(max_workers=cfg.get("max_workers", 8)) as ex:
        futs = [ex.submit(push, pid, vs) for pid, vs in by_product.items()]
        done = 0
        for _ in as_completed(futs):
            done += 1
            if done % 25 == 0 or done == len(futs):
                log.info(f"  pushed {done}/{len(futs)} products (ok={ok} err={err})")

    log.info(f"DONE: {ok} products updated, {err} errors, {total_variants} variants priced")

    # ---- write the full variant map to a product metafield (handles >250 variants) ----
    write_variant_pricing_metafields(shop, {pid: pricing_map[pid] for pid in by_product if pid in pricing_map})


METAFIELDS_SET = """
mutation($mf: [MetafieldsSetInput!]!) {
  metafieldsSet(metafields: $mf) { userErrors { field message } }
}
"""


def write_variant_pricing_metafields(shop, pricing_map):
    """Store each product's full {optionKey: [variantId, price, compare, grams]} map in
    custom.variant_pricing (JSON). One metafield per product = no 250-variant cap; the
    theme reads it in Liquid (no Storefront token needed)."""
    items = list(pricing_map.items())
    if not items:
        return
    ok = err = 0
    BATCH = 25
    for i in range(0, len(items), BATCH):
        mfs = [{
            "ownerId": pid,
            "namespace": "custom",
            "key": "variant_pricing",
            "type": "json",
            "value": json.dumps(m, separators=(",", ":")),
        } for pid, m in items[i:i + BATCH]]
        try:
            res = shop.execute(METAFIELDS_SET, {"mf": mfs})["metafieldsSet"]
            if res["userErrors"]:
                err += len(mfs); log.error(f"  variant_pricing userErrors: {res['userErrors'][:2]}")
            else:
                ok += len(mfs)
        except Exception as e:
            err += len(mfs); log.error(f"  variant_pricing batch failed: {e}")
    log.info(f"variant_pricing metafield written: {ok} products ({err} errors)")


def main():
    ap = argparse.ArgumentParser(description="SVADEZI live price engine")
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--dry-run", action="store_true", help="Compute + log, push nothing")
    ap.add_argument("--gold-24k", type=float, help="Manual gold 24k INR/g (manual mode)")
    ap.add_argument("--silver-999", type=float, help="Manual silver 999 INR/g (manual mode)")
    ap.add_argument("--handle", help="Only this product handle (testing)")
    ap.add_argument("--limit", type=int, help="Only first N products (testing)")
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
