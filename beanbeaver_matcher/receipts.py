"""Read staged receipt JSON (schema version "2", the shape beanbeaver writes under each
receipt chain's `stages/` directory) into effective `Receipt` objects.

Reads the JSON directly rather than depending on beanbeaver-core's Rust resolver — see the
README's "Staged-JSON contract" section. Item categories are **not** resolved from category
rules in v1 (that needs beanbeaver's project-local rule config); itemized postings default to
`Expenses:FIXME` in `enrich.py`. This is a known, documented v1 gap.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast

from beanbeaver_matcher.model import Receipt, ReceiptItem, ReceiptWarning, Tender, TenderKind

_VALID_TENDER_KINDS = {"card", "gift_card", "cash", "store_credit"}
_STAGES_DIRNAME = "stages"


def placeholder_date() -> date:
    """A stand-in date for receipts with no resolved date (first of the current month)."""
    return date.today().replace(day=1)


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return Decimal(stripped)
        except InvalidOperation:
            return None
    return None


def _to_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return date.fromisoformat(stripped)
        except ValueError:
            return None
    return None


def _effective_receipt(document: dict[str, Any], key: str) -> Any:
    """Review-stage overlay wins over the originally parsed `receipt` value."""
    review = document.get("review")
    if isinstance(review, dict) and review.get(key) is not None:
        return review[key]
    receipt = document.get("receipt")
    return receipt.get(key) if isinstance(receipt, dict) else None


def _effective_entry(entry: dict[str, Any], key: str) -> Any:
    """Review-stage overlay wins over an item's/tender's own value."""
    review = entry.get("review")
    if isinstance(review, dict) and review.get(key) is not None:
        return review[key]
    return entry.get(key)


def _is_removed(entry: dict[str, Any]) -> bool:
    review = entry.get("review")
    return bool(isinstance(review, dict) and review.get("removed"))


def _parse_items(raw_items: list[Any]) -> list[ReceiptItem]:
    items = []
    for raw in raw_items:
        if not isinstance(raw, dict) or _is_removed(raw):
            continue
        description = _effective_entry(raw, "description")
        description = str(description).strip() if description else ""
        price = _to_decimal(_effective_entry(raw, "price")) or Decimal("0")
        quantity_raw = _effective_entry(raw, "quantity")
        quantity = int(quantity_raw) if isinstance(quantity_raw, (int, float)) else 1
        items.append(
            ReceiptItem(
                description=description or "UNKNOWN_ITEM",
                price=price,
                quantity=quantity,
                # Rule-based category resolution is deferred (see module docstring);
                # enrich.py falls back to Expenses:FIXME for uncategorized items.
                category=None,
            )
        )
    return items


def _parse_warnings(document: dict[str, Any]) -> list[ReceiptWarning]:
    warnings: list[ReceiptWarning] = []
    active_index = -1
    for raw in document.get("items") or []:
        if not isinstance(raw, dict) or _is_removed(raw):
            continue
        active_index += 1
        for warning in raw.get("warnings") or []:
            message = warning.get("message") if isinstance(warning, dict) else None
            if message:
                warnings.append(ReceiptWarning(message=str(message), after_item_index=active_index))
    for warning in document.get("warnings") or []:
        message = warning.get("message") if isinstance(warning, dict) else None
        if message:
            warnings.append(ReceiptWarning(message=str(message), after_item_index=None))
    return warnings


def _parse_tenders(raw_tenders: list[Any]) -> list[Tender]:
    tenders = []
    for raw in raw_tenders:
        if not isinstance(raw, dict) or _is_removed(raw):
            continue
        amount = _to_decimal(_effective_entry(raw, "amount"))
        if amount is None:
            continue
        account = _effective_entry(raw, "account")
        account = str(account).strip() or None if account else None
        kind_value = _effective_entry(raw, "kind")
        kind: TenderKind = cast(TenderKind, kind_value) if kind_value in _VALID_TENDER_KINDS else "card"
        raw_label = raw.get("raw_label") or ""
        tenders.append(Tender(amount=amount, account=account, kind=kind, raw_label=str(raw_label)))
    return tenders


def receipt_from_stage_document(document: dict[str, Any]) -> Receipt:
    """Resolve one staged JSON document (schema version "2") into an effective Receipt."""
    merchant = _effective_receipt(document, "merchant")
    merchant = str(merchant).strip() if merchant else ""

    resolved_date = _to_date(_effective_receipt(document, "date"))
    date_is_placeholder = resolved_date is None

    total = _to_decimal(_effective_receipt(document, "total")) or Decimal("0")
    tax = _to_decimal(_effective_receipt(document, "tax"))
    subtotal = _to_decimal(_effective_receipt(document, "subtotal"))

    raw_text = document.get("raw_text") or ""
    meta_value = document.get("meta")
    meta = meta_value if isinstance(meta_value, dict) else {}
    image_filename = meta.get("image_filename") or ""

    return Receipt(
        merchant=merchant or "UNKNOWN_MERCHANT",
        date=resolved_date or placeholder_date(),
        total=total,
        date_is_placeholder=date_is_placeholder,
        items=_parse_items(document.get("items") or []),
        tax=tax,
        subtotal=subtotal,
        raw_text=str(raw_text),
        image_filename=str(image_filename),
        warnings=_parse_warnings(document),
        tenders=_parse_tenders(document.get("tenders") or []),
    )


def load_stage_document(path: Path) -> dict[str, Any]:
    """Load one staged receipt JSON document from disk."""
    return json.loads(path.read_text())


def read_receipt(stage_path: Path) -> Receipt:
    """Read one staged receipt JSON file into an effective Receipt."""
    return receipt_from_stage_document(load_stage_document(stage_path))


def stage_status(document: dict[str, Any]) -> str:
    """One of "scanned", "approved" (awaiting match), or "matched"."""
    meta_value = document.get("meta")
    meta = meta_value if isinstance(meta_value, dict) else {}
    stage = str(meta.get("stage") or "").lower()
    if "matched" in stage:
        return "matched"
    if "review" in stage:
        return "approved"
    return "scanned"


def stage_index(document: dict[str, Any]) -> int:
    meta_value = document.get("meta")
    meta = meta_value if isinstance(meta_value, dict) else {}
    try:
        return int(meta.get("stage_index", 0))
    except (TypeError, ValueError):
        return 0


def receipt_chain_dir(stage_path: Path) -> Path:
    """The receipt-chain directory enclosing one stage file (parent of `stages/`)."""
    return stage_path.parent.parent if stage_path.parent.name == _STAGES_DIRNAME else stage_path.parent


def receipt_chain_name(stage_path: Path) -> str:
    """A stable, human-readable name for one receipt chain (its directory name)."""
    return receipt_chain_dir(stage_path).name


def latest_stage_file(chain_dir: Path) -> Path | None:
    """The stage file with the highest `stage_index` in one receipt-chain directory."""
    stages_dir = chain_dir / _STAGES_DIRNAME
    if not stages_dir.is_dir():
        return None
    stage_files = sorted(stages_dir.glob("*.receipt.json"))
    if not stage_files:
        return None
    return max(stage_files, key=lambda path: stage_index(load_stage_document(path)))


@dataclass(frozen=True)
class ApprovedReceipt:
    stage_path: Path
    receipt: Receipt


def list_approved_receipts(receipts_root: Path) -> list[ApprovedReceipt]:
    """The latest-stage receipt for every chain directory under `receipts_root` whose
    latest stage is "approved" (reviewed, awaiting a ledger match)."""
    results: list[ApprovedReceipt] = []
    if not receipts_root.is_dir():
        return results
    for chain_dir in sorted(receipts_root.iterdir()):
        if not chain_dir.is_dir():
            continue
        stage_path = latest_stage_file(chain_dir)
        if stage_path is None:
            continue
        document = load_stage_document(stage_path)
        if stage_status(document) != "approved":
            continue
        results.append(ApprovedReceipt(stage_path=stage_path, receipt=receipt_from_stage_document(document)))
    return results
