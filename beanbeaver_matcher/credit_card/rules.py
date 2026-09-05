"""TOML-backed merchant categorization for statement transactions."""

from __future__ import annotations

import tomllib
from pathlib import Path


def _load(path: Path) -> list[tuple[tuple[str, ...], str]]:
    if not path.is_file():
        return []
    with path.open("rb") as handle:
        document = tomllib.load(handle)
    rules = []
    for raw in document.get("rules", []):
        category = str(raw.get("category", "")).strip()
        keywords = tuple(str(keyword).upper() for keyword in raw.get("keywords", []) if str(keyword).strip())
        if category and keywords:
            rules.append((keywords, category))
    return rules


def _load_chequing(path: Path) -> list[tuple[str, str]]:
    if not path.is_file():
        return []
    with path.open("rb") as handle:
        document = tomllib.load(handle)
    rules = []
    for raw in document.get("rules", []):
        pattern = str(raw.get("pattern", "")).strip().upper()
        account = str(raw.get("account", "")).strip()
        if pattern and account:
            rules.append((pattern, account))
    return rules


class MerchantRules:
    def __init__(self, project_rules: Path | None = None) -> None:
        default_path = Path(__file__).with_name("default_merchant_rules.toml")
        self.rules = [*(_load(project_rules) if project_rules else []), *_load(default_path)]

    def categorize(self, payee: str, amount) -> str:
        upper = payee.upper()
        for keywords, category in self.rules:
            if any(keyword in upper for keyword in keywords):
                return category
        if amount >= 0 and amount < 5:
            return "Expenses:Shopping:NotAssigned"
        return "Expenses:Uncategorized"


class ChequingRules:
    def __init__(self, project_rules: Path | None = None) -> None:
        self.rules = _load_chequing(project_rules) if project_rules else []

    def categorize(self, payee: str, amount) -> str:
        upper = payee.upper()
        for pattern, account in self.rules:
            if pattern in upper:
                return account
        return "Expenses:Uncategorized"
