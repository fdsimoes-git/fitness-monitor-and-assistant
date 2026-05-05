"""Food lookup helpers: Open Food Facts barcode lookup."""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

OFF_URL = "https://world.openfoodfacts.org/api/v0/product/{barcode}.json"
USER_AGENT = "garmin-monitor/0.1 (personal Pi project)"


def _http_get_json(url: str, timeout: float = 8.0) -> dict | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        log.warning("HTTP error fetching %s: %s", url, e)
        return None
    except (ValueError, json.JSONDecodeError) as e:
        log.warning("Bad JSON from %s: %s", url, e)
        return None


def _coerce_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def lookup_barcode(barcode: str) -> dict | None:
    """Query Open Food Facts (free, no API key) for a barcode.

    Returns dict: {name, kcal_100g, protein_100g, carbs_100g, fat_100g,
    fiber_100g, sugars_100g, saturated_fat_100g, sodium_mg_100g,
    food_category, serving_size_g, raw}. Returns None on miss/fetch error.
    """
    if not barcode or not barcode.strip():
        return None
    payload = _http_get_json(OFF_URL.format(barcode=barcode.strip()))
    if not payload or payload.get("status") != 1:
        return None
    product = payload.get("product") or {}
    nutr = product.get("nutriments") or {}

    name = (
        product.get("product_name")
        or product.get("generic_name")
        or product.get("brands")
        or "Unknown product"
    )

    serving_g = _coerce_float(product.get("serving_quantity"))
    if serving_g is None:
        serving_g = _coerce_float(product.get("product_quantity"))

    # OFF reports salt in grams; sodium ≈ salt × 0.4 (× 1000 → mg).
    salt_100g = _coerce_float(nutr.get("salt_100g"))
    sodium_mg = round(salt_100g * 400.0, 1) if salt_100g is not None else None

    return {
        "name": name,
        "kcal_100g": _coerce_float(nutr.get("energy-kcal_100g")),
        "protein_100g": _coerce_float(nutr.get("proteins_100g")),
        "carbs_100g": _coerce_float(nutr.get("carbohydrates_100g")),
        "fat_100g": _coerce_float(nutr.get("fat_100g")),
        "fiber_100g": _coerce_float(nutr.get("fiber_100g")),
        "sugars_100g": _coerce_float(nutr.get("sugars_100g")),
        "saturated_fat_100g": _coerce_float(nutr.get("saturated-fat_100g")),
        "sodium_mg_100g": sodium_mg,
        "food_category": product.get("pnns_groups_2") or product.get("pnns_groups_1"),
        "serving_size_g": serving_g,
        "raw": product,
    }
