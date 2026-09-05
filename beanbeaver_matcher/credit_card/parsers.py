"""Institution routing and CSV parsing for Canadian credit-card exports."""

from __future__ import annotations

import csv
import re
from collections.abc import Callable, Iterable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from beanbeaver_matcher.credit_card.model import CardImporterId, ParsedCardRow

_TRANSACTIONS_DOWNLOAD_RE = re.compile(r"^transactions(?: \(\d+\))?\.csv$", re.IGNORECASE)
_MBNA_MONTHLY_RE = re.compile(r"^[A-Za-z]+20\d{2}_\d{4}\.csv$")
_WEALTHSIMPLE_EXPORT_RE = re.compile(r"^activities-export-\d{4}-\d{2}-\d{2}(?: \(\d+\))?\.csv$", re.IGNORECASE)


class CardParseError(ValueError):
    """A statement cannot be routed or parsed safely."""


def _header(path: Path, *, skip_rows: int = 0, encoding: str = "utf-8-sig") -> set[str]:
    try:
        with path.open(encoding=encoding, newline="") as handle:
            reader = csv.reader(handle)
            for _ in range(skip_rows):
                next(reader, None)
            return {column.strip().lower() for column in next(reader, [])}
    except (OSError, UnicodeError, csv.Error):
        return set()


def route_credit_card(path: Path) -> CardImporterId:
    """Identify a supported card export using content signatures plus filename hints."""
    name = path.name
    lower = name.lower()
    header0 = _header(path)
    header2 = _header(path, skip_rows=2)
    header3 = _header(path, skip_rows=3)

    wealthsimple_date_columns = {"effective_date", "transaction_date"}
    if (
        _WEALTHSIMPLE_EXPORT_RE.fullmatch(name)
        and wealthsimple_date_columns.intersection(header0)
        and {"account_type", "activity_type", "description", "net_cash_amount"}.issubset(header0)
    ):
        return "wealthsimple_chequing"

    if {"date", "merchant name", "amount"}.issubset(header0):
        return "rogers"
    if {"transaction date", "amount", "description", "type"}.issubset(header3):
        return "ctfs"
    if {"transaction date", "transaction amount", "description"}.issubset(header2):
        return "bmo"
    if {"posted date", "payee", "address", "amount"}.issubset(header0):
        return "mbna"
    if {"date", "description", "amount"}.issubset(header0) and (
        "amex" in lower or lower in {"activity.csv", "plat.csv"}
    ):
        return "amex"

    if lower == "cibc.csv" or ("simplii" in lower and lower.endswith(".csv")):
        return "cibc"
    if lower in {"statement.csv", "porter.csv"}:
        return "bmo"
    if lower == "report.csv":
        return "pcf"
    if _TRANSACTIONS_DOWNLOAD_RE.fullmatch(name):
        raise CardParseError("Transactions.csv does not match the Rogers or CTFS header signature")
    if lower.startswith("transaction history_") and lower.endswith(".csv"):
        return "rogers"
    if "scotiabank" in lower and lower.endswith(".csv"):
        return "scotia"
    if ("mbna" in lower and lower.endswith(".csv")) or _MBNA_MONTHLY_RE.fullmatch(name):
        return "mbna"
    if lower in {"activity.csv", "plat.csv"} or ("amex" in lower and lower.endswith(".csv")):
        return "amex"
    raise CardParseError(f"Unsupported credit-card CSV: {name}")


def _decimal(raw: str) -> Decimal:
    value = raw.strip().replace(",", "").replace("$", "")
    negative = value.startswith("(") and value.endswith(")")
    if negative:
        value = value[1:-1]
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise CardParseError(f"Invalid amount: {raw!r}") from exc
    return -amount if negative else amount


def _date(raw: str, *formats: str):
    value = raw.strip()
    for date_format in formats:
        try:
            return datetime.strptime(value, date_format).date()
        except ValueError:
            continue
    raise CardParseError(f"Invalid date: {raw!r}")


def _dict_rows(path: Path, *, skip_rows: int = 0, encoding: str = "utf-8-sig") -> Iterable[tuple[int, dict[str, str]]]:
    with path.open(encoding=encoding, newline="") as handle:
        for _ in range(skip_rows):
            next(handle, None)
        for index, row in enumerate(csv.DictReader(handle), start=1):
            yield index, {str(key).strip().lower(): value or "" for key, value in row.items() if key is not None}


def _parsed(index: int, txn_date, payee: str, amount: Decimal) -> ParsedCardRow:
    return ParsedCardRow(str(index), txn_date, payee.strip(), amount)


def _parse_cibc(path: Path) -> list[ParsedCardRow]:
    rows = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        next(reader, None)
        for index, row in enumerate(reader, start=1):
            if len(row) < 3 or not row[0].strip() or not row[2].strip():
                continue
            rows.append(_parsed(index, _date(row[0], "%Y-%m-%d", "%m/%d/%Y"), row[1], _decimal(row[2])))
    return rows


def _parse_bmo(path: Path) -> list[ParsedCardRow]:
    return [
        _parsed(
            index,
            _date(row["transaction date"], "%Y%m%d"),
            row["description"],
            _decimal(row["transaction amount"]),
        )
        for index, row in _dict_rows(path, skip_rows=2)
    ]


def _parse_scotia(path: Path) -> list[ParsedCardRow]:
    rows = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        next(reader, None)
        for index, row in enumerate(reader, start=1):
            if len(row) < 7 or row[5].strip().lower() != "debit":
                continue
            rows.append(_parsed(index, _date(row[1], "%Y-%m-%d"), row[2], _decimal(row[6])))
    return rows


def _parse_rogers(path: Path) -> list[ParsedCardRow]:
    rows = []
    for index, row in _dict_rows(path):
        raw_amount = row.get("amount", "").strip()
        if not raw_amount or raw_amount.startswith("-"):
            continue
        rows.append(_parsed(index, _date(row["date"], "%Y-%m-%d"), row["merchant name"], _decimal(raw_amount)))
    return rows


def _parse_mbna(path: Path) -> list[ParsedCardRow]:
    rows: list[ParsedCardRow] = []
    with path.open(encoding="iso-8859-1", newline="") as handle:
        raw_rows = list(csv.reader(handle))
    if not raw_rows:
        return rows
    has_header = raw_rows[0] and raw_rows[0][0].strip().lower() in {"posted date", "date"}
    for index, row in enumerate(raw_rows[1:] if has_header else raw_rows, start=1):
        if len(row) < 4 or not row[0].strip():
            continue
        rows.append(_parsed(index, _date(row[0], "%m/%d/%Y"), row[1], -_decimal(row[3])))
    return rows


def _parse_pcf(path: Path) -> list[ParsedCardRow]:
    rows = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        next(reader, None)
        for index, row in enumerate(reader, start=1):
            if len(row) < 6 or row[1].strip().upper() == "PAYMENT":
                continue
            rows.append(_parsed(index, _date(row[3], "%m/%d/%Y"), row[0], abs(_decimal(row[5]))))
    return rows


def _parse_ctfs(path: Path) -> list[ParsedCardRow]:
    rows = []
    for index, row in _dict_rows(path, skip_rows=3, encoding="utf-8"):
        description = row["description"]
        if row["type"].strip().upper() == "PAYMENT" and any(
            token in description.upper() for token in ("PAYMENT", "PMT")
        ):
            continue
        rows.append(_parsed(index, _date(row["transaction date"], "%Y-%m-%d"), description, _decimal(row["amount"])))
    return rows


def _parse_amex(path: Path) -> list[ParsedCardRow]:
    return [
        _parsed(index, _date(row["date"], "%d %b %Y"), row["description"], _decimal(row["amount"]))
        for index, row in _dict_rows(path)
    ]


def _parse_wealthsimple_chequing(path: Path) -> list[ParsedCardRow]:
    rows = []
    for index, row in _dict_rows(path):
        if row.get("account_type", "").strip().lower() != "chequing":
            continue
        raw_amount = row.get("net_cash_amount", "").strip()
        raw_date = (row.get("effective_date") or row.get("transaction_date") or "").strip()
        if not raw_amount or not raw_date:
            continue
        payee = re.sub(r"\s*\(executed at \d{4}-\d{2}-\d{2}\)\s*$", "", row.get("description", "")).strip()
        currency = row.get("currency", "").strip() or "CAD"
        parsed = _parsed(index, _date(raw_date, "%Y-%m-%d"), payee, _decimal(raw_amount))
        rows.append(ParsedCardRow(parsed.row_id, parsed.date, parsed.payee, parsed.amount, currency))
    return rows


_PARSERS: dict[CardImporterId, Callable[[Path], list[ParsedCardRow]]] = {
    "cibc": _parse_cibc,
    "bmo": _parse_bmo,
    "scotia": _parse_scotia,
    "rogers": _parse_rogers,
    "mbna": _parse_mbna,
    "pcf": _parse_pcf,
    "ctfs": _parse_ctfs,
    "amex": _parse_amex,
    "wealthsimple_chequing": _parse_wealthsimple_chequing,
}


def parse_credit_card(path: Path, importer_id: CardImporterId) -> list[ParsedCardRow]:
    try:
        rows = _PARSERS[importer_id](path)
    except (KeyError, IndexError, OSError, UnicodeError, csv.Error) as exc:
        raise CardParseError(f"Could not parse {path.name} as {importer_id}: {exc}") from exc
    rows = [row for row in rows if not _should_skip_shared(row, importer_id)]
    if not rows:
        raise CardParseError(f"The {importer_id} importer produced no transactions")
    return rows


def _should_skip_shared(row: ParsedCardRow, importer_id: CardImporterId) -> bool:
    payee = row.payee.strip().upper()
    if "TRSF FROM/DE ACCT/CPT" in payee or "PAYMENT RECEIVED" in payee or payee == "PAYMENT":
        return True
    if "INSTALLMENT PLAN FOR" in payee:
        return True
    return importer_id == "amex" and row.amount < 0 and "PRESTO FARE" in payee
