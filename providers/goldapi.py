"""
goldapi.io provider — with multi-key fallback.

Endpoints (confirmed from the SVADEZI theme proxy):
    GET https://www.goldapi.io/api/XAU/<CUR>   header: x-access-token: <key>
    GET https://www.goldapi.io/api/XAG/<CUR>   header: x-access-token: <key>

Pass one OR several keys. If a key is quota-exceeded / fails, the next key is
tried automatically. Returns pure 24k gold/gram and pure silver/gram; karat
rates are derived in the engine so the math stays provider-agnostic.
"""

import requests

BASE = "https://www.goldapi.io/api"
TROY_OZ_G = 31.1034768


def _get(symbol, currency, api_key):
    headers = {"x-access-token": api_key, "Content-Type": "application/json"}
    r = requests.get(f"{BASE}/{symbol}/{currency}", headers=headers, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"{symbol}/{currency} HTTP {r.status_code}: {r.text[:160]}")
    return r.json()


def _fetch_one(api_key, currency):
    gold = _get("XAU", currency, api_key)
    silver = _get("XAG", currency, api_key)
    gold_24k = gold.get("price_gram_24k")
    silver_999 = silver.get("price_gram_24k")
    if not silver_999 and silver.get("price"):
        silver_999 = silver["price"] / TROY_OZ_G
    if not gold_24k or not silver_999:
        raise RuntimeError(f"unusable rates gold_24k={gold_24k} silver_999={silver_999}")
    return {"gold_24k": float(gold_24k), "silver_999": float(silver_999),
            "raw": {"XAU": gold, "XAG": silver}}


def fetch_rates(api_keys, currency="INR"):
    """api_keys: a list (or comma-separated string) of goldapi keys, tried in order."""
    if isinstance(api_keys, str):
        api_keys = api_keys.split(",")
    keys = [k.strip() for k in (api_keys or []) if k and k.strip()]
    if not keys:
        raise RuntimeError("No goldapi keys provided (METALS_API_KEY / METALS_API_KEY_2)")

    errors = []
    for i, key in enumerate(keys, 1):
        try:
            res = _fetch_one(key, currency)
            res["source"] = f"goldapi.io#{i}"
            return res
        except Exception as e:
            errors.append(f"key#{i} ({key[-6:]}): {e}")
            continue
    raise RuntimeError("All goldapi keys failed -> " + " | ".join(errors))
