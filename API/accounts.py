"""Persist API / headless MT5 accounts (table rows for the Accounts Manager dock)."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


KIND_HEADLESS = "headless_mt5"  # local terminal, hidden from UI
KIND_CLOUD = "cloud_api"  # future vendor REST when docs arrive


def default_accounts_path(app_dir: str) -> str:
    return os.path.join(app_dir, "API", "accounts.json")


@dataclass
class ApiAccount:
    id: str
    name: str
    kind: str = KIND_HEADLESS
    login: str = ""
    server: str = ""
    terminal_path: str = ""
    # Future cloud fields
    api_base_url: str = ""
    api_account_id: str = ""
    enabled: bool = True
    notes: str = ""
    # In-memory only — never written to disk
    password: str = ""
    api_key: str = ""

    @staticmethod
    def new(name: str = "API account", kind: str = KIND_HEADLESS) -> "ApiAccount":
        return ApiAccount(
            id=uuid.uuid4().hex[:10],
            name=(name or "API account").strip() or "API account",
            kind=kind or KIND_HEADLESS,
        )

    def kind_label(self) -> str:
        if self.kind == KIND_CLOUD:
            return "Cloud API"
        return "Broker API"

    def display_target(self) -> str:
        if self.kind == KIND_CLOUD:
            host = (self.api_base_url or "").strip() or "—"
            aid = (self.api_account_id or "").strip()
            return f"{host}" + (f" · {aid}" if aid else "")
        login = (self.login or "").strip() or "—"
        server = (self.server or "").strip() or "—"
        return f"{login} @ {server}"


@dataclass
class ApiAccountStore:
    accounts: List[ApiAccount] = field(default_factory=list)

    def by_id(self, account_id: str) -> Optional[ApiAccount]:
        for a in self.accounts:
            if a.id == account_id:
                return a
        return None

    def upsert(self, account: ApiAccount) -> None:
        for i, existing in enumerate(self.accounts):
            if existing.id == account.id:
                # Keep in-memory secrets if new row left them blank
                if not account.password and existing.password:
                    account.password = existing.password
                if not account.api_key and existing.api_key:
                    account.api_key = existing.api_key
                self.accounts[i] = account
                return
        self.accounts.append(account)

    def remove(self, account_id: str) -> bool:
        before = len(self.accounts)
        self.accounts = [a for a in self.accounts if a.id != account_id]
        return len(self.accounts) < before


def account_to_dict(acc: ApiAccount) -> Dict[str, Any]:
    return {
        "id": acc.id,
        "name": acc.name,
        "kind": acc.kind,
        "login": acc.login,
        "server": acc.server,
        "terminal_path": acc.terminal_path,
        "api_base_url": acc.api_base_url,
        "api_account_id": acc.api_account_id,
        "enabled": bool(acc.enabled),
        "notes": acc.notes,
        # password / api_key intentionally omitted
    }


def account_from_dict(row: Dict[str, Any]) -> ApiAccount:
    kind = str(row.get("kind") or KIND_HEADLESS).strip() or KIND_HEADLESS
    if kind not in (KIND_HEADLESS, KIND_CLOUD):
        kind = KIND_HEADLESS
    return ApiAccount(
        id=str(row.get("id") or uuid.uuid4().hex[:10]),
        name=str(row.get("name") or "API account").strip() or "API account",
        kind=kind,
        login=str(row.get("login") or "").strip(),
        server=str(row.get("server") or "").strip(),
        terminal_path=str(row.get("terminal_path") or "").strip(),
        api_base_url=str(row.get("api_base_url") or "").strip(),
        api_account_id=str(row.get("api_account_id") or "").strip(),
        enabled=bool(row.get("enabled", True)),
        notes=str(row.get("notes") or "").strip(),
    )


def load_accounts(path: str) -> ApiAccountStore:
    store = ApiAccountStore()
    if not path or not os.path.isfile(path):
        return store
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        return store
    rows = raw.get("accounts") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return store
    for row in rows:
        if isinstance(row, dict):
            store.accounts.append(account_from_dict(row))
    return store


def save_accounts(path: str, store: ApiAccountStore) -> None:
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    payload = {
        "version": 1,
        "accounts": [account_to_dict(a) for a in store.accounts],
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
