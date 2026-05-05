"""Telegram inbound listener for the meal-logging bot.

Single-user, raw `requests`-based long-polling. Mirrors the outbound style
in `alerts.py` so we don't pull in a heavyweight Telegram library.

Dispatch tree
─────────────
  /help, /start                           → static help text
  /today, /balance                        → today's calorie balance
  numeric ^\\d{8,14}$                       → barcode → Open Food Facts → auto-insert
  any other text                          → Claude text extractor → auto-insert
  photo                                   → Claude vision; if a barcode is in
                                            frame we route through OFF, otherwise
                                            we use the visual estimate. Either
                                            way we surface a Confirm/Cancel
                                            inline keyboard before persisting.
  callback_query (confirm:UUID/cancel:UUID) → flush or drop pending entry

Photo bytes are passed to Claude in-memory and **discarded** — no SHA-256
cache, no blob columns, nothing that would let raw inputs leak into SQLite.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from datetime import date
from typing import Any

import requests

from . import alerts, db, food, llm
from .config import Config

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

TELEGRAM_API = "https://api.telegram.org/bot{token}"
TELEGRAM_FILE = "https://api.telegram.org/file/bot{token}/{path}"
LONG_POLL_TIMEOUT = 30  # seconds; Telegram allows up to 50
PENDING_TTL_SECONDS = 3600
INITIAL_BACKOFF_S = 2.0
MAX_BACKOFF_S = 60.0
BARCODE_PATTERN = re.compile(r"^\d{8,14}$")

HELP_TEXT = (
    "*garmin-monitor bot*\n\n"
    "Send any of:\n"
    "• A meal description (e.g. _\"oat porridge with banana, ~350 kcal\"_)\n"
    "• A *photo* of your food or a packaged product (Confirm/Cancel before logging)\n"
    "• A product *barcode* (8–14 digits)\n\n"
    "Commands:\n"
    "/help — this message\n"
    "/today — today's calorie balance\n"
    "/balance — alias for /today"
)


# ──────────────────────────────────────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────────────────────────────────────

def run(cfg: Config) -> None:
    """Long-poll Telegram for messages until interrupted.

    `pending` is a per-process dict mapping a short UUID to a proposed meal
    awaiting Confirm/Cancel. Photos and barcode-from-photo proposals stash
    here; text and explicit-barcode messages auto-insert without queuing.
    """
    if not cfg.telegram_bot_token or not cfg.telegram_chat_id:
        raise RuntimeError(
            "Telegram bot needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID set in .env"
        )
    log.info("Bot starting (long-poll)…")
    offset: int | None = None
    backoff = INITIAL_BACKOFF_S
    pending: dict[str, dict[str, Any]] = {}
    while True:
        try:
            updates = _get_updates(cfg, offset)
            for u in updates:
                offset = u["update_id"] + 1
                try:
                    dispatch(cfg, u, pending)
                except Exception:  # noqa: BLE001 — never let one update kill the loop
                    log.exception("dispatch error")
            backoff = INITIAL_BACKOFF_S
        except KeyboardInterrupt:
            log.info("Bot stopped by user")
            return
        except Exception as e:  # noqa: BLE001 — outer net catches transient failures
            log.warning("Bot loop error: %s — backing off %.1fs", e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2.0, MAX_BACKOFF_S)


def _get_updates(cfg: Config, offset: int | None) -> list[dict]:
    params: dict[str, Any] = {
        "timeout": LONG_POLL_TIMEOUT,
        "allowed_updates": '["message","callback_query"]',
    }
    if offset is not None:
        params["offset"] = offset
    url = TELEGRAM_API.format(token=cfg.telegram_bot_token) + "/getUpdates"
    r = requests.get(url, params=params, timeout=LONG_POLL_TIMEOUT + 10)
    r.raise_for_status()
    body = r.json() or {}
    if not body.get("ok"):
        raise RuntimeError(f"Telegram getUpdates returned ok=false: {body}")
    return body.get("result") or []


# ──────────────────────────────────────────────────────────────────────────────
# Dispatch
# ──────────────────────────────────────────────────────────────────────────────


def dispatch(cfg: Config, update: dict, pending: dict[str, dict[str, Any]]) -> None:
    """Top-level update handler. Routes based on update kind and chat-id whitelist."""
    if "callback_query" in update:
        q = update["callback_query"]
        cb_chat = (q.get("message") or {}).get("chat", {}).get("id")
        if not _is_whitelisted(cfg, cb_chat):
            log.warning("Ignoring callback from non-whitelisted chat %s", cb_chat)
            return
        _handle_callback(cfg, q, pending)
        return

    msg = update.get("message")
    if not msg:
        return
    chat_id = msg.get("chat", {}).get("id")
    if not _is_whitelisted(cfg, chat_id):
        log.warning("Ignoring message from non-whitelisted chat %s", chat_id)
        return
    if "photo" in msg:
        _handle_photo(cfg, msg, pending)
    elif "text" in msg:
        _handle_text(cfg, msg, pending)


def _is_whitelisted(cfg: Config, chat_id: Any) -> bool:
    if chat_id is None:
        return False
    return str(chat_id) == str(cfg.telegram_chat_id)


# ──────────────────────────────────────────────────────────────────────────────
# Text flow
# ──────────────────────────────────────────────────────────────────────────────


def _handle_text(cfg: Config, msg: dict, _pending: dict[str, dict[str, Any]]) -> None:
    text = (msg.get("text") or "").strip()
    if not text:
        return

    if text in ("/help", "/start"):
        alerts.send_telegram(cfg, HELP_TEXT)
        return
    if text in ("/today", "/balance"):
        bal = db.calorie_balance_for_date(cfg.db_path, date.today().isoformat(), cfg=cfg)
        alerts.send_telegram(cfg, _format_balance(bal))
        return
    if BARCODE_PATTERN.match(text):
        _log_barcode_auto(cfg, text)
        return

    meal = llm.extract_meal_from_text(cfg, text)
    if meal is None:
        _send_plain(cfg, "Sorry — I couldn't extract a meal from that. Try /help.")
        return
    meal.setdefault("source", "text")
    mid = db.insert_meal(cfg.db_path, meal)
    log.info("Inserted meal #%s from text", mid)
    _send_plain(cfg, f"Logged: {_format_meal_summary(meal)}")


def _log_barcode_auto(cfg: Config, barcode: str) -> None:
    info = food.lookup_barcode(barcode)
    if info is None:
        _send_plain(cfg, f"Barcode {barcode} not found in Open Food Facts.")
        return
    meal = food.meal_from_barcode_info(info, barcode)
    db.insert_meal(cfg.db_path, meal)
    _send_plain(cfg, f"Logged barcode {barcode} — {_format_meal_summary(meal)}")


# ──────────────────────────────────────────────────────────────────────────────
# Photo flow
# ──────────────────────────────────────────────────────────────────────────────


def _handle_photo(cfg: Config, msg: dict, pending: dict[str, dict[str, Any]]) -> None:
    photos = msg.get("photo") or []
    if not photos:
        return
    largest = max(photos, key=lambda p: p.get("file_size") or 0)
    image_bytes, mime_type = _download_telegram_file(cfg, largest["file_id"])
    if image_bytes is None:
        _send_plain(cfg, "Couldn't download that photo from Telegram.")
        return

    result = llm.extract_meal_from_photo(cfg, image_bytes, mime_type)
    # Drop the buffer reference now — no persistence, no caching.
    del image_bytes

    if result is None:
        _send_plain(
            cfg,
            "Sorry — I couldn't read that meal. Try a clearer angle, send a barcode, "
            "or describe it in text.",
        )
        return

    if result.get("kind") == "barcode":
        info = food.lookup_barcode(result["barcode"])
        if info is None:
            _send_plain(cfg, f"Spotted barcode {result['barcode']} but it's not in Open Food Facts.")
            return
        proposed = food.meal_from_barcode_info(info, result["barcode"])
    else:
        proposed = dict(result.get("meal") or {})
        proposed.setdefault("source", "photo")

    _offer_confirmation(cfg, proposed, pending)


def _offer_confirmation(
    cfg: Config, meal: dict, pending: dict[str, dict[str, Any]]
) -> None:
    pid = uuid.uuid4().hex[:10]
    _sweep_pending(pending)
    pending[pid] = {"meal": meal, "ts": time.time()}
    keyboard = [[
        {"text": "✅ Log it", "callback_data": f"confirm:{pid}"},
        {"text": "❌ Cancel", "callback_data": f"cancel:{pid}"},
    ]]
    _send_with_keyboard(cfg, f"Proposed: {_format_meal_summary(meal)}\n\nLog it?", keyboard)


def _sweep_pending(pending: dict[str, dict[str, Any]], ttl: float = PENDING_TTL_SECONDS) -> None:
    now = time.time()
    stale = [k for k, v in pending.items() if now - v.get("ts", 0) > ttl]
    for k in stale:
        del pending[k]
    if stale:
        log.debug("Swept %d stale pending entries", len(stale))


# ──────────────────────────────────────────────────────────────────────────────
# Callback flow
# ──────────────────────────────────────────────────────────────────────────────


def _handle_callback(
    cfg: Config, q: dict, pending: dict[str, dict[str, Any]]
) -> None:
    data = (q.get("data") or "")
    callback_id = q.get("id")
    if not data or ":" not in data:
        _answer_callback(cfg, callback_id, "Bad callback")
        return
    action, pid = data.split(":", 1)
    entry = pending.pop(pid, None)
    msg = q.get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    msg_id = msg.get("message_id")

    if entry is None:
        _answer_callback(cfg, callback_id, "Expired")
        if chat_id and msg_id:
            _edit_message_remove_keyboard(cfg, chat_id, msg_id, "Confirmation expired.")
        return

    if action == "confirm":
        meal = entry["meal"]
        db.insert_meal(cfg.db_path, meal)
        _answer_callback(cfg, callback_id, "Logged")
        if chat_id and msg_id:
            _edit_message_remove_keyboard(
                cfg, chat_id, msg_id, f"✅ Logged: {_format_meal_summary(meal)}"
            )
    elif action == "cancel":
        _answer_callback(cfg, callback_id, "Cancelled")
        if chat_id and msg_id:
            _edit_message_remove_keyboard(cfg, chat_id, msg_id, "❌ Cancelled.")
    else:
        _answer_callback(cfg, callback_id, "Unknown action")


# ──────────────────────────────────────────────────────────────────────────────
# Telegram HTTP helpers
# ──────────────────────────────────────────────────────────────────────────────


def _download_telegram_file(cfg: Config, file_id: str) -> tuple[bytes | None, str]:
    """Resolve a file_id to bytes via getFile + a content GET. No persistence."""
    api = TELEGRAM_API.format(token=cfg.telegram_bot_token)
    try:
        r = requests.get(f"{api}/getFile", params={"file_id": file_id}, timeout=15)
        r.raise_for_status()
        result = (r.json() or {}).get("result") or {}
        path = result.get("file_path")
        if not path:
            return None, ""
        url = TELEGRAM_FILE.format(token=cfg.telegram_bot_token, path=path)
        rr = requests.get(url, timeout=30)
        rr.raise_for_status()
        ext = (path.rsplit(".", 1)[-1] or "jpg").lower()
        mime = {
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "png": "image/png",
            "webp": "image/webp",
        }.get(ext, "image/jpeg")
        return rr.content, mime
    except Exception as e:  # noqa: BLE001
        log.error("Failed to download file %s: %s", file_id, e)
        return None, ""


def _send_plain(cfg: Config, text: str) -> bool:
    """Send a Markdown-free message to the whitelisted chat. Used for dynamic
    output where free-text descriptions might collide with Markdown syntax."""
    api = TELEGRAM_API.format(token=cfg.telegram_bot_token)
    try:
        r = requests.post(
            f"{api}/sendMessage",
            json={"chat_id": cfg.telegram_chat_id, "text": text},
            timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception as e:  # noqa: BLE001
        log.error("send_plain failed: %s", e)
        return False


def _send_with_keyboard(cfg: Config, text: str, keyboard: list[list[dict]]) -> bool:
    api = TELEGRAM_API.format(token=cfg.telegram_bot_token)
    try:
        r = requests.post(
            f"{api}/sendMessage",
            json={
                "chat_id": cfg.telegram_chat_id,
                "text": text,
                "reply_markup": {"inline_keyboard": keyboard},
            },
            timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception as e:  # noqa: BLE001
        log.error("send_with_keyboard failed: %s", e)
        return False


def _answer_callback(cfg: Config, callback_id: str | None, text: str) -> None:
    if not callback_id:
        return
    api = TELEGRAM_API.format(token=cfg.telegram_bot_token)
    try:
        requests.post(
            f"{api}/answerCallbackQuery",
            json={"callback_query_id": callback_id, "text": text},
            timeout=10,
        )
    except Exception as e:  # noqa: BLE001
        log.error("answerCallbackQuery failed: %s", e)


def _edit_message_remove_keyboard(
    cfg: Config, chat_id: Any, msg_id: Any, new_text: str
) -> None:
    api = TELEGRAM_API.format(token=cfg.telegram_bot_token)
    try:
        requests.post(
            f"{api}/editMessageText",
            json={"chat_id": chat_id, "message_id": msg_id, "text": new_text},
            timeout=10,
        )
    except Exception as e:  # noqa: BLE001
        log.error("editMessageText failed: %s", e)


# ──────────────────────────────────────────────────────────────────────────────
# Formatting helpers
# ──────────────────────────────────────────────────────────────────────────────


def _format_balance(bal: dict) -> str:
    sign = "surplus" if bal["balance_kcal"] >= 0 else "deficit"
    burned = (
        f"BMR {bal['bmr_kcal']} + steps {bal['steps_burned_kcal']}"
        + (f" + activities {bal['burned_kcal']}" if bal['burned_kcal'] > 0 else "")
    )
    return (
        f"*Today {bal['date']}*\n"
        f"Eaten: {bal['eaten_kcal']:.0f} kcal · {bal['meal_count']} meal(s)\n"
        f"Burned: {bal['total_burned_kcal']} kcal ({burned})\n"
        f"*Balance: {bal['balance_kcal']:+.0f} kcal {sign}*\n"
        f"Macros: P{bal['protein_g']:.0f}g / C{bal['carbs_g']:.0f}g / F{bal['fat_g']:.0f}g"
    )


def _format_meal_summary(meal: dict) -> str:
    desc = meal.get("description") or "—"
    parts = [desc]
    kcal = meal.get("kcal")
    if kcal is not None:
        parts.append(f"{kcal:.0f} kcal")
    macros = []
    for label, key in (("P", "protein_g"), ("C", "carbs_g"), ("F", "fat_g")):
        v = meal.get(key)
        if v is not None:
            macros.append(f"{v:.0f}g {label}")
    if macros:
        parts.append(" / ".join(macros))
    return " · ".join(parts)
