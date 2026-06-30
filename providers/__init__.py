"""
Metal-rate provider registry.

Each provider exposes `fetch_rates(api_key, currency) -> dict` returning at least:
    { "gold_24k": <INR per gram>, "silver_999": <INR per gram>, "source": "<name>", "raw": {...} }

To add a new provider later: drop a module in this package with a `fetch_rates`
function and register it in PROVIDERS below. Then set "provider" in config.json.
"""

from . import goldapi
from . import metalsdev

PROVIDERS = {
    "goldapi": goldapi.fetch_rates,
    "metalsdev": metalsdev.fetch_rates,
}


def get_provider(name):
    if name not in PROVIDERS:
        raise ValueError(f"Unknown provider '{name}'. Available: {list(PROVIDERS)}")
    return PROVIDERS[name]
