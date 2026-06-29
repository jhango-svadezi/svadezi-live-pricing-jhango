#!/usr/bin/env python3
"""
One-time setup: create the `metal_rate` metaobject DEFINITION so the engine can
upsert the live rate + pricing-constants snapshot, and the theme price-breakdown
can read the exact same values.

Run once after granting the app these scopes:
    read_metaobjects, write_metaobjects, read_metaobject_definitions, write_metaobject_definitions

    export SHOPIFY_STORE=... SHOPIFY_ACCESS_TOKEN=...
    python setup_metaobject.py
"""
import os, sys, json
import requests

API = os.environ.get("SHOPIFY_API_VERSION", "2024-10")
store = os.environ.get("SHOPIFY_STORE", "").replace("https://", "").rstrip("/")
if store and not store.endswith(".myshopify.com"):
    store += ".myshopify.com"
token = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")
if not store or not token:
    print("Missing SHOPIFY_STORE / SHOPIFY_ACCESS_TOKEN"); sys.exit(1)

URL = f"https://{store}/admin/api/{API}/graphql.json"
H = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}

# Every field is decimal where numeric so the theme can read & compute exactly.
FIELDS = [
    ("gold_24k", "Gold 24K INR/g", "number_decimal"),
    ("silver_999", "Silver 999 INR/g", "number_decimal"),
    ("making_gold", "Making gold INR/g", "number_decimal"),
    ("making_silver", "Making silver INR/g", "number_decimal"),
    ("finishing", "Finishing flat INR", "number_decimal"),
    ("diamond_lab", "Lab diamond INR/ct", "number_decimal"),
    ("diamond_natural_small", "Natural <=thr INR/ct", "number_decimal"),
    ("diamond_natural_large", "Natural >thr INR/ct", "number_decimal"),
    ("natural_threshold_ct", "Natural threshold ct", "number_decimal"),
    ("gst_multiplier", "GST multiplier", "number_decimal"),
    ("compare_at_multiplier", "Compare-at multiplier", "number_decimal"),
    ("rounding_step", "Rounding step", "number_decimal"),
    ("silver_purity", "Silver purity", "number_decimal"),
    ("karat_purity", "Karat purity JSON", "json"),
    ("source", "Rate source", "single_line_text_field"),
    ("updated_at", "Updated at", "single_line_text_field"),
]

MUT = """
mutation($def: MetaobjectDefinitionCreateInput!) {
  metaobjectDefinitionCreate(definition: $def) {
    metaobjectDefinition { id type }
    userErrors { field message code }
  }
}"""

definition = {
    "name": "Metal Rate",
    "type": "metal_rate",
    "access": {"storefront": "PUBLIC_READ"},   # so the theme can read it
    "fieldDefinitions": [{"key": k, "name": n, "type": t} for k, n, t in FIELDS],
}

r = requests.post(URL, json={"query": MUT, "variables": {"def": definition}}, headers=H, timeout=60).json()
res = (r.get("data") or {}).get("metaobjectDefinitionCreate") or {}
if r.get("errors"):
    print("GraphQL errors:", json.dumps(r["errors"], indent=1)); sys.exit(1)
if res.get("userErrors"):
    print("userErrors:", res["userErrors"])
    print("(If it says 'taken', the definition already exists — that's fine.)")
else:
    print("Created metaobject definition:", res["metaobjectDefinition"])
