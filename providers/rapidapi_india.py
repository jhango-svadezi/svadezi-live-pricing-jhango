"""
RapidAPI — "Gold Silver Rates India" (soralapps) provider.

Endpoint (ONE call returns both metals, gold in 22k/24k):
    GET https://gold-silver-rates-india.p.rapidapi.com/api/Fetch-Gold-Silver/?city=<city>
    headers: x-rapidapi-host, x-rapidapi-key

Response (relevant part):
    { "success": true, "data": {
        "gold":   { "22k": {"1gram": 12895, ...}, "24k": {"1gram": 13540, ...} },
        "silver": { "1gram": 240, "1kg": 240000, ...lots of garbled history fields... } } }

Unlike global spot APIs (metals.dev / goldapi), these are REAL Indian retail
rates (pre-GST), so `24k.1gram` slots straight into the SVADEZI formula whose
`x1.03` GST then reproduces the on-the-street Indian price. We read ONLY the
clean `gold.24k.1gram` and `silver.1gram` fields and ignore the noisy silver
history block.
"""

import os
import requests

HOST = "gold-silver-rates-india.p.rapidapi.com"
URL = f"https://{HOST}/api/Fetch-Gold-Silver/"


def fetch_rates(api_keys, currency="INR", city=None, **_):
    if isinstance(api_keys, str):
        api_keys = api_keys.split(",")
    keys = [k.strip() for k in (api_keys or []) if k and k.strip()]
    if not keys:
        raise RuntimeError("No RapidAPI key provided (RAPIDAPI_KEY)")

    city = (city or os.environ.get("RAPIDAPI_CITY") or "mumbai").strip().lower()
    headers_base = {"x-rapidapi-host": HOST, "Content-Type": "application/json"}

    errors = []
    for i, key in enumerate(keys, 1):
        try:
            r = requests.get(URL, params={"city": city},
                             headers={**headers_base, "x-rapidapi-key": key},
                             timeout=30)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:160]}")
            j = r.json()
            if not j.get("success"):
                raise RuntimeError(f"success!=true {str(j)[:160]}")
            data = j.get("data", {}) or {}
            gold = ((data.get("gold", {}) or {}).get("24k", {}) or {}).get("1gram")
            silver = (data.get("silver", {}) or {}).get("1gram")
            if not gold or not silver:
                raise RuntimeError(f"missing gold/silver: gold={gold} silver={silver}")
            return {"gold_24k": float(gold), "silver_999": float(silver),
                    "source": f"rapidapi_india:{city}#{i}",
                    "raw": {"gold": data.get("gold"), "silver_1g": silver}}
        except Exception as e:
            errors.append(f"key#{i} ({key[-6:]}): {e}")
            continue
    raise RuntimeError("All RapidAPI India keys failed -> " + " | ".join(errors))
