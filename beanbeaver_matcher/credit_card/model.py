"""Plain-data models shared by the card-import core and Fava adapter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal

CardImporterId = Literal["cibc", "bmo", "scotia", "rogers", "mbna", "pcf", "ctfs", "amex"]


@dataclass(frozen=True)
class ParsedCardRow:
    row_id: str
    date: date
    payee: str
    amount: Decimal
    currency: str = "CAD"


@dataclass(frozen=True)
class PlannedCardTransaction:
    row_id: str
    date: date
    payee: str
    amount: Decimal
    currency: str
    category: str
    duplicate: bool = False


@dataclass(frozen=True)
class CreditCardPlan:
    source_path: Path
    source_sha256: str
    importer_id: CardImporterId
    account: str
    transactions: tuple[PlannedCardTransaction, ...]
    candidate_categories: tuple[str, ...]

    @property
    def start_date(self) -> date:
        return min(transaction.date for transaction in self.transactions)

    @property
    def end_date(self) -> date:
        return max(transaction.date for transaction in self.transactions)


@dataclass(frozen=True)
class TransactionEdit:
    row_id: str
    category: str
    new_amount: Decimal | None = None
    deleted: bool = False


@dataclass(frozen=True)
class ApplyImportResult:
    status: Literal["applied", "already_applied"]
    output_path: Path
    transaction_count: int
    message: str
