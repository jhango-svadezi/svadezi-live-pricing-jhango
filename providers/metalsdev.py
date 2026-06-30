"""
metals.dev provider.

Endpoint:
    GET https://api.metals.dev/v1/latest?api_key=<key>&currency=<CUR>&unit=g

Response (relevant part):
    { "status": "success", "currency": "INR", "unit": "g",
      "metals": { "gold": 12344.35, "silver": 183.08, ... } }

`metals.gold` / `metals.silver` are already per-gram in the requested currency,
so they map directly to pure 24k gold/g and pure silver/g.
"""

import requests

BASE = "https://api.metals.dev/v1/latest"


def fetch_rates(api_keys, currency="INR"):
    if isinstance(api_keys, str):
        api_keys = api_keys.split(",")
    keys = [k.strip() for k in (api_keys or []) if k and k.strip()]
    if not keys:
        raise RuntimeError("No metals.dev API key provided (METALS_API_KEY)")

    errors = []
    for i, key in enumerate(keys, 1):
        try:
            r = requests.get(BASE, params={"api_key": key, "currency": currency, "unit": "g"}, timeout=30)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:160]}")
            j = r.json()
            if j.get("status") != "success":
                raise RuntimeError(f"status={j.get('status')} {str(j)[:160]}")
            m = j.get("metals", {})
            gold = m.get("gold")
            silver = m.get("silver")
            if not gold or not silver:
                raise RuntimeError(f"missing gold/silver: {m}")
            return {"gold_24k": float(gold), "silver_999": float(silver),
                    "source": f"metals.dev#{i}", "raw": j}
        except Exception as e:
            errors.append(f"key#{i} ({key[-6:]}): {e}")
            continue
    raise RuntimeError("All metals.dev keys failed -> " + " | ".join(errors))
