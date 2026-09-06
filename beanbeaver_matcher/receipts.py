"""Read staged receipt JSON (schema version "2", the shape beanbeaver writes under each
receipt chain's `stages/` directory) into effective `Receipt` objects.

Reads the JSON directly rather than depending on beanbeaver-core's Rust resolver — see the
README's "Staged-JSON contract" section. Item categories are **not** resolved from category
rules in v1 (that needs beanbeaver's project-local rule config); itemized postings default to
`Expenses:FIXME` in `enrich.py`. This is a known, documented v1 gap.
"""

from __future__ import annotations

import copy
import json
import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from beanbeaver_matcher.model import Receipt, ReceiptItem, ReceiptWarning, Tender, TenderKind

_VALID_TENDER_KINDS = {"card", "gift_card", "cash", "store_credit"}
_STAGES_DIRNAME = "stages"
_ACCOUNT_PREFIXES = ("Assets:", "Liabilities:", "Equity:", "Expenses:", "Income:")
_LEGACY_POSTING_RE = re.compile(
    r"^\s+([A-Z][A-Za-z0-9:_-]+)\s+[-+]?(?:\d+(?:\.\d*)?|\.\d+)\s+[A-Z][A-Z0-9._-]*(?:\s+;\s*(.*))?$"
)


class ReceiptEditError(ValueError):
    """A receipt edit is invalid or conflicts with a newer on-disk version."""


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
                category=_effective_entry(raw, "account") or None,
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


def receipt_from_legacy_document(
    document: dict[str, Any], *, image_filename: str = "", draft_item_accounts: tuple[str | None, ...] = ()
) -> Receipt:
    """Read the flat receipt JSON emitted by older BeanBeaver versions."""
    merchant = str(document.get("merchant") or "").strip()
    resolved_date = _to_date(document.get("date"))
    date_is_placeholder = bool(document.get("dateIsPlaceholder")) or resolved_date is None
    items = []
    for item_index, raw in enumerate(document.get("items") or []):
        if not isinstance(raw, dict):
            continue
        description = str(raw.get("description") or "").strip() or "UNKNOWN_ITEM"
        price = _to_decimal(raw.get("price")) or Decimal("0")
        quantity_raw = raw.get("quantity")
        try:
            quantity = int(quantity_raw) if quantity_raw is not None else 1
        except (TypeError, ValueError):
            quantity = 1
        raw_category = raw.get("account") or raw.get("category")
        category_value = str(raw_category).strip() if raw_category else ""
        category = category_value if category_value.startswith(_ACCOUNT_PREFIXES) else None
        if category is None and item_index < len(draft_item_accounts):
            category = draft_item_accounts[item_index]
        items.append(ReceiptItem(description=description, price=price, quantity=quantity, category=category))

    warnings = []
    for raw_warning in document.get("warnings") or []:
        if isinstance(raw_warning, dict):
            message = raw_warning.get("message")
        else:
            message = raw_warning
        if message:
            warnings.append(ReceiptWarning(message=str(message), after_item_index=None))

    return Receipt(
        merchant=merchant or "UNKNOWN_MERCHANT",
        date=resolved_date or placeholder_date(),
        total=_to_decimal(document.get("total")) or Decimal("0"),
        date_is_placeholder=date_is_placeholder,
        items=items,
        tax=_to_decimal(document.get("tax")),
        subtotal=_to_decimal(document.get("subtotal")),
        image_filename=image_filename,
        warnings=warnings,
        tenders=_parse_tenders(document.get("tenders") or []),
    )


def _legacy_draft_item_accounts(receipt_path: Path, document: dict[str, Any]) -> tuple[str | None, ...]:
    """Recover resolved item accounts from the legacy generated Beancount draft."""
    draft_path = receipt_path.with_suffix(".beancount")
    if not draft_path.is_file():
        return ()
    accounts_by_description: dict[str, list[str]] = {}
    for line in draft_path.read_text().splitlines():
        match = _LEGACY_POSTING_RE.match(line)
        if match is None or match.group(2) is None:
            continue
        account, description = match.groups()
        accounts_by_description.setdefault(description.strip(), []).append(account)

    accounts = []
    for raw in document.get("items") or []:
        description = str(raw.get("description") or "").strip() if isinstance(raw, dict) else ""
        candidates = accounts_by_description.get(description, [])
        accounts.append(candidates.pop(0) if candidates else None)
    return tuple(accounts)


def load_stage_document(path: Path) -> dict[str, Any]:
    """Load one staged receipt JSON document from disk."""
    return json.loads(path.read_text())


def read_receipt(stage_path: Path) -> Receipt:
    """Read one staged receipt JSON file into an effective Receipt."""
    document = load_stage_document(stage_path)
    if is_legacy_receipt_path(stage_path):
        image_filename = ""
        for suffix in (".jpg", ".jpeg", ".png"):
            image_path = stage_path.with_suffix(suffix)
            if image_path.is_file():
                image_filename = image_path.name
                break
        return receipt_from_legacy_document(
            document,
            image_filename=image_filename,
            draft_item_accounts=_legacy_draft_item_accounts(stage_path, document),
        )
    return receipt_from_stage_document(document)


def is_legacy_receipt_path(path: Path) -> bool:
    """Whether a receipt JSON is from the pre-stage, flat per-receipt layout."""
    return path.parent.name != _STAGES_DIRNAME and path.suffix.lower() == ".json"


def legacy_matched_path(path: Path) -> Path:
    return path.with_suffix(".matched")


def receipt_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _edit_decimal(value: object, label: str, *, required: bool = False) -> Decimal | None:
    parsed = _to_decimal(value)
    if parsed is None:
        if value is not None and str(value).strip():
            raise ReceiptEditError(f"{label} must be a number")
        if required:
            raise ReceiptEditError(f"{label} is required")
        return None
    if not parsed.is_finite():
        raise ReceiptEditError(f"{label} must be finite")
    return parsed


def _validate_receipt_edit(
    document: dict[str, Any], payload: dict[str, Any], allowed_accounts: set[str]
) -> dict[str, Any]:
    merchant = str(payload.get("merchant") or "").strip()
    if not merchant:
        raise ReceiptEditError("Merchant is required")
    raw_date = str(payload.get("date") or "").strip()
    parsed_date = _to_date(raw_date)
    if raw_date and parsed_date is None:
        raise ReceiptEditError("Date must be in YYYY-MM-DD format")
    total = _edit_decimal(payload.get("total"), "Total", required=True)
    if total is None or total <= 0:
        raise ReceiptEditError("Total must be greater than zero")
    subtotal = _edit_decimal(payload.get("subtotal"), "Subtotal")
    tax = _edit_decimal(payload.get("tax"), "Tax")

    raw_items = payload.get("items")
    existing_items = document.get("items") or []
    if not isinstance(raw_items, list):
        raise ReceiptEditError("Items must be a list")
    edited_items = []
    used_indices: set[int] = set()
    for index, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict):
            raise ReceiptEditError(f"Item {index} is invalid")
        source_index = raw.get("source_index")
        existing = {}
        if source_index is not None:
            if (
                type(source_index) is not int
                or not 0 <= source_index < len(existing_items)
                or source_index in used_indices
                or not isinstance(existing_items[source_index], dict)
            ):
                raise ReceiptEditError(f"Item {index} has an invalid source index; reload the receipt")
            used_indices.add(source_index)
            existing = existing_items[source_index]
        description = str(raw.get("description") or "").strip()
        if not description:
            raise ReceiptEditError(f"Item {index} description is required")
        price = _edit_decimal(raw.get("price"), f"Item {index} price", required=True)
        try:
            quantity = int(raw.get("quantity", 1))
        except (TypeError, ValueError) as exc:
            raise ReceiptEditError(f"Item {index} quantity must be a whole number") from exc
        if str(quantity) != str(raw.get("quantity", 1)).strip():
            raise ReceiptEditError(f"Item {index} quantity must be a whole number")
        if quantity < 1:
            raise ReceiptEditError(f"Item {index} quantity must be at least one")
        account = str(raw.get("category") or "").strip()
        if account and account not in allowed_accounts:
            raise ReceiptEditError(f"Item {index} account is not open on the receipt date: {account}")
        edited = dict(existing)
        edited.update({"description": description, "price": str(price), "quantity": quantity})
        edited["account"] = account or "Expenses:FIXME"
        edited_items.append(edited)

    raw_tenders = payload.get("tenders") or []
    if not isinstance(raw_tenders, list):
        raise ReceiptEditError("Tenders must be a list")
    edited_tenders = []
    tender_total = Decimal("0")
    card_total = Decimal("0")
    for index, raw in enumerate(raw_tenders, start=1):
        if not isinstance(raw, dict):
            raise ReceiptEditError(f"Tender {index} is invalid")
        kind = str(raw.get("kind") or "")
        if kind not in _VALID_TENDER_KINDS:
            raise ReceiptEditError(f"Tender {index} has an invalid kind")
        amount = _edit_decimal(raw.get("amount"), f"Tender {index} amount", required=True)
        if amount is None or amount <= 0:
            raise ReceiptEditError(f"Tender {index} amount must be greater than zero")
        account = str(raw.get("account") or "").strip()
        if kind != "card" and not account:
            raise ReceiptEditError(f"Tender {index} requires an account")
        if account and account not in allowed_accounts:
            raise ReceiptEditError(f"Tender {index} account is not open on the receipt date: {account}")
        tender_total += amount
        if kind == "card":
            card_total += amount
        edited_tenders.append(
            {
                "kind": kind,
                "amount": str(amount),
                "account": account or None,
                "raw_label": str(raw.get("raw_label") or ""),
            }
        )
    if edited_tenders:
        if abs(tender_total - total) > Decimal("0.01"):
            raise ReceiptEditError(f"Tender total ({tender_total:.2f}) must equal receipt total ({total:.2f})")
        if card_total <= 0:
            raise ReceiptEditError("At least one card tender is required for credit-card matching")

    document.update(
        {
            "merchant": merchant,
            "date": parsed_date.isoformat() if parsed_date else None,
            "dateIsPlaceholder": parsed_date is None,
            "total": str(total),
            "subtotal": str(subtotal) if subtotal is not None else None,
            "tax": str(tax) if tax is not None else None,
            "items": edited_items,
            "tenders": edited_tenders,
        }
    )
    return document


def update_legacy_receipt(
    path: Path,
    payload: dict[str, Any],
    *,
    expected_sha256: str,
    allowed_accounts: set[str],
) -> Receipt:
    """Validate and atomically persist edits to one legacy receipt JSON."""
    if not is_legacy_receipt_path(path):
        raise ReceiptEditError("Only legacy flat receipts can be edited in Matcher")
    if receipt_sha256(path) != expected_sha256:
        raise ReceiptEditError("Receipt changed after it was opened; reload it before saving")
    document = load_stage_document(path)

    document = _validate_receipt_edit(document, payload, allowed_accounts)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=path.parent, encoding="utf-8") as handle:
        temporary = Path(handle.name)
        json.dump(document, handle, indent=2)
        handle.write("\n")
    os.replace(temporary, path)
    return read_receipt(path)


def update_receipt(path: Path, payload: dict[str, Any], *, expected_sha256: str, allowed_accounts: set[str]) -> Path:
    """Save a legacy edit or append a review stage, preserving parsed source data."""
    if is_legacy_receipt_path(path):
        update_legacy_receipt(path, payload, expected_sha256=expected_sha256, allowed_accounts=allowed_accounts)
        return path
    if latest_stage_file(receipt_chain_dir(path)) != path or stage_status(load_stage_document(path)) == "matched":
        raise ReceiptEditError("Only the latest unmatched receipt stage can be reviewed")
    if receipt_sha256(path) != expected_sha256:
        raise ReceiptEditError("Receipt changed after it was opened; reload it before saving")
    document = load_stage_document(path)
    validated = _validate_receipt_edit(copy.deepcopy(document), payload, allowed_accounts)
    reviewed = copy.deepcopy(document)
    review = reviewed["review"] = dict(reviewed.get("review") or {})
    for key in ("merchant", "date", "subtotal", "tax", "total"):
        # Empty strings explicitly clear optional fields through the review overlay.
        review[key] = validated[key] if validated[key] is not None else ""
    items = reviewed["items"] = reviewed.get("items") or []
    for item in items:
        if isinstance(item, dict):
            item["review"] = {**(item.get("review") or {}), "removed": True}
    for raw, edited in zip(payload["items"], validated["items"], strict=True):
        source_index = raw.get("source_index")
        if source_index is None:
            item = {"id": f"item-{uuid4().hex}"}
            items.append(item)
        else:
            item = items[source_index]
        item.setdefault("review", {}).update(
            {key: edited[key] for key in ("description", "price", "quantity", "account")}
        )
        item["review"]["removed"] = False
    reviewed["tenders"] = validated["tenders"]
    meta = reviewed.setdefault("meta", {})
    index = stage_index(document) + 1
    meta.update(
        {
            "stage": f"review_stage_{index}",
            "stage_index": index,
            "parent_file": path.name,
            "created_by": "beanbeaver_matcher",
            "created_at": datetime.now(UTC).isoformat(),
            "pass_name": "receipt_review",
        }
    )
    destination = path.parent / f"{index:03d}_review_{uuid4().hex}.receipt.json"
    with tempfile.NamedTemporaryFile("w", delete=False, dir=path.parent, encoding="utf-8") as handle:
        temporary = Path(handle.name)
        json.dump(reviewed, handle, indent=2)
        handle.write("\n")
    try:
        if latest_stage_file(receipt_chain_dir(path)) != path or receipt_sha256(path) != expected_sha256:
            raise ReceiptEditError("Receipt changed after it was opened; reload it before saving")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


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


def list_approved_receipts(receipts_root: Path, *, include_scanned: bool = False) -> list[ApprovedReceipt]:
    """The latest-stage receipt for every chain directory under `receipts_root` whose
    latest stage is "approved" (reviewed, awaiting a ledger match).
    Set include_scanned to also include receipts awaiting their first review."""
    results: list[ApprovedReceipt] = []
    if not receipts_root.is_dir():
        return results
    for chain_dir in sorted(receipts_root.iterdir()):
        if chain_dir.name == ".trash" or not chain_dir.is_dir():
            continue
        stage_path = latest_stage_file(chain_dir)
        if stage_path is not None:
            document = load_stage_document(stage_path)
            if stage_status(document) == "approved" or (include_scanned and stage_status(document) == "scanned"):
                results.append(ApprovedReceipt(stage_path=stage_path, receipt=receipt_from_stage_document(document)))
            continue

        # Legacy chains have one flat JSON plus a generated Beancount draft. An
        # empty sibling `.matched` file is their archive marker.
        for legacy_path in sorted(chain_dir.glob("*.json")):
            if legacy_matched_path(legacy_path).exists() or not legacy_path.with_suffix(".beancount").is_file():
                continue
            results.append(ApprovedReceipt(stage_path=legacy_path, receipt=read_receipt(legacy_path)))
            break
    return results
