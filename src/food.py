"""Food lookup helpers: Open Food Facts barcode + heuristic description estimator."""
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

    Returns dict: {name, kcal_100g, protein_100g, carbs_100g, fat_100g, serving_size_g, raw}
    Returns None on miss or fetch error.
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

    return {
        "name": name,
        "kcal_100g": _coerce_float(nutr.get("energy-kcal_100g")),
        "protein_100g": _coerce_float(nutr.get("proteins_100g")),
        "carbs_100g": _coerce_float(nutr.get("carbohydrates_100g")),
        "fat_100g": _coerce_float(nutr.get("fat_100g")),
        "serving_size_g": serving_g,
        "raw": product,
    }


# Rough kcal/macros per 100g for very common foods. Best-effort fallback only.
_FOOD_TABLE: dict[str, dict[str, float]] = {
    "egg": {"kcal": 155, "protein": 13, "carbs": 1.1, "fat": 11},
    "eggs": {"kcal": 155, "protein": 13, "carbs": 1.1, "fat": 11},
    "rice": {"kcal": 130, "protein": 2.7, "carbs": 28, "fat": 0.3},
    "white rice": {"kcal": 130, "protein": 2.7, "carbs": 28, "fat": 0.3},
    "brown rice": {"kcal": 112, "protein": 2.6, "carbs": 23, "fat": 0.9},
    "pasta": {"kcal": 158, "protein": 5.8, "carbs": 31, "fat": 0.9},
    "bread": {"kcal": 265, "protein": 9, "carbs": 49, "fat": 3.2},
    "chicken": {"kcal": 165, "protein": 31, "carbs": 0, "fat": 3.6},
    "chicken breast": {"kcal": 165, "protein": 31, "carbs": 0, "fat": 3.6},
    "beef": {"kcal": 250, "protein": 26, "carbs": 0, "fat": 17},
    "salmon": {"kcal": 208, "protein": 20, "carbs": 0, "fat": 13},
    "tuna": {"kcal": 132, "protein": 28, "carbs": 0, "fat": 1.3},
    "potato": {"kcal": 77, "protein": 2, "carbs": 17, "fat": 0.1},
    "banana": {"kcal": 89, "protein": 1.1, "carbs": 23, "fat": 0.3},
    "apple": {"kcal": 52, "protein": 0.3, "carbs": 14, "fat": 0.2},
    "yogurt": {"kcal": 59, "protein": 10, "carbs": 3.6, "fat": 0.4},
    "milk": {"kcal": 42, "protein": 3.4, "carbs": 5, "fat": 1},
    "cheese": {"kcal": 402, "protein": 25, "carbs": 1.3, "fat": 33},
    "oats": {"kcal": 389, "protein": 17, "carbs": 66, "fat": 7},
    "oatmeal": {"kcal": 71, "protein": 2.5, "carbs": 12, "fat": 1.5},
    "almonds": {"kcal": 579, "protein": 21, "carbs": 22, "fat": 50},
    "peanut butter": {"kcal": 588, "protein": 25, "carbs": 20, "fat": 50},
    "avocado": {"kcal": 160, "protein": 2, "carbs": 9, "fat": 15},
    "broccoli": {"kcal": 34, "protein": 2.8, "carbs": 7, "fat": 0.4},
    "salad": {"kcal": 20, "protein": 1.5, "carbs": 3.6, "fat": 0.2},
    "pizza": {"kcal": 266, "protein": 11, "carbs": 33, "fat": 10},
    "burger": {"kcal": 295, "protein": 17, "carbs": 24, "fat": 14},
    "sandwich": {"kcal": 250, "protein": 11, "carbs": 30, "fat": 9},
    "soup": {"kcal": 50, "protein": 2, "carbs": 8, "fat": 1.5},
    "coffee": {"kcal": 2, "protein": 0.3, "carbs": 0, "fat": 0},
    "beer": {"kcal": 43, "protein": 0.5, "carbs": 3.6, "fat": 0},
    "wine": {"kcal": 83, "protein": 0.1, "carbs": 2.6, "fat": 0},
}


def estimate_from_description(description: str) -> dict:
    """Best-effort kcal/macro estimate from a free-text food description.

    Matches the description against a small lookup table of common foods.
    Assumes a 150g default serving. Returns zeros + a `matched=False` flag if
    nothing matched — caller decides whether to store the row anyway.
    """
    desc_lc = (description or "").lower().strip()
    serving_g = 150.0

    matched_key = None
    for key in sorted(_FOOD_TABLE, key=len, reverse=True):
        if key in desc_lc:
            matched_key = key
            break

    if matched_key is None:
        return {
            "matched": False,
            "matched_key": None,
            "serving_size_g": serving_g,
            "kcal": None,
            "protein_g": None,
            "carbs_g": None,
            "fat_g": None,
        }

    macros = _FOOD_TABLE[matched_key]
    factor = serving_g / 100.0
    return {
        "matched": True,
        "matched_key": matched_key,
        "serving_size_g": serving_g,
        "kcal": round(macros["kcal"] * factor, 1),
        "protein_g": round(macros["protein"] * factor, 1),
        "carbs_g": round(macros["carbs"] * factor, 1),
        "fat_g": round(macros["fat"] * factor, 1),
    }
