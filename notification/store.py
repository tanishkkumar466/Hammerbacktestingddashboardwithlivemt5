"""Persist notification bot configs under APP_DIR/notification/."""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Dict, List, Optional

from .manager import MODE_BOTH, MODE_DRY_RUN, MODE_LIVE, NotificationBot


def default_bots_path(app_dir: str) -> str:
    return os.path.join(app_dir, "notification", "bots.json")


def _normalize_mode(raw: Any) -> str:
    m = str(raw or MODE_BOTH).strip().lower().replace("-", "_").replace(" ", "_")
    if m in ("live", "live_only", "real"):
        return MODE_LIVE
    if m in ("dry", "dry_run", "dryrun", "sim", "paper"):
        return MODE_DRY_RUN
    return MODE_BOTH


def bot_from_dict(row: Dict[str, Any]) -> NotificationBot:
    accounts = row.get("account_ids") or row.get("accounts") or []
    timeframes = row.get("timeframes") or []
    return NotificationBot(
        id=str(row.get("id") or uuid.uuid4().hex[:10]),
        name=str(row.get("name") or "Telegram bot").strip() or "Telegram bot",
        bot_token=str(row.get("bot_token") or row.get("token") or "").strip(),
        chat_id=str(row.get("chat_id") or "").strip(),
        enabled=bool(row.get("enabled", True)),
        mode=_normalize_mode(row.get("mode")),
        account_ids=[str(a).strip() for a in accounts if str(a).strip()],
        timeframes=[str(t).strip() for t in timeframes if str(t).strip()],
        notify_start_hhmm=str(row.get("notify_start_hhmm") or "").strip(),
        notify_end_hhmm=str(row.get("notify_end_hhmm") or "").strip(),
    )


def bot_to_dict(bot: NotificationBot) -> Dict[str, Any]:
    return {
        "id": bot.id,
        "name": bot.name,
        "bot_token": bot.bot_token,
        "chat_id": bot.chat_id,
        "enabled": bool(bot.enabled),
        "mode": bot.mode,
        "account_ids": list(bot.account_ids),
        "timeframes": list(bot.timeframes),
        "notify_start_hhmm": str(bot.notify_start_hhmm or "").strip(),
        "notify_end_hhmm": str(bot.notify_end_hhmm or "").strip(),
    }


def load_bots(path: str) -> List[NotificationBot]:
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    rows = raw.get("bots") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return []
    out: List[NotificationBot] = []
    for row in rows:
        if isinstance(row, dict):
            out.append(bot_from_dict(row))
    return out


def save_bots(path: str, bots: List[NotificationBot]) -> None:
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    payload = {
        "version": 1,
        "bots": [bot_to_dict(b) for b in bots],
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def migrate_legacy_single_bot(
    *,
    enabled: bool,
    bot_token: str,
    chat_id: str,
    existing: Optional[List[NotificationBot]] = None,
) -> List[NotificationBot]:
    """
    If bots list is empty and legacy QSettings telegram fields exist,
    seed one 'Live + Dry' bot (or two preferred: Live / Dry with same token).
    """
    bots = list(existing or [])
    token = (bot_token or "").strip()
    cid = (chat_id or "").strip()
    if not token or not cid:
        return bots
    if bots:
        return bots
    # Prefer two channels when client later edits chat ids separately.
    bots.append(
        NotificationBot(
            id=uuid.uuid4().hex[:10],
            name="Live trades",
            bot_token=token,
            chat_id=cid,
            enabled=bool(enabled),
            mode=MODE_LIVE,
        )
    )
    bots.append(
        NotificationBot(
            id=uuid.uuid4().hex[:10],
            name="Dry-run / paper",
            bot_token=token,
            chat_id=cid,
            enabled=bool(enabled),
            mode=MODE_DRY_RUN,
        )
    )
    return bots
