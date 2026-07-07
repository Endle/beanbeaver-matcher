"""Pure DTOs for receipts, tenders, ledger transactions, and match candidates."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal

TenderKind = Literal["card", "gift_card", "cash", "store_credit"]


@dataclass
class ReceiptItem:
    """A single line item on a receipt."""

    description: str
    price: Decimal
    quantity: int = 1
    category: str | None = None


@dataclass
class ReceiptWarning:
    """Parser/review warning attached to a nearby item position."""

    message: str
    after_item_index: int | None = None


@dataclass
class Tender:
    """One payment tender on a receipt (e.g., $25 on a gift card)."""

    amount: Decimal
    account: str | None = None
    kind: TenderKind = "card"
    raw_label: str = ""


@dataclass
class Receipt:
    """Effective (review-resolved) receipt data read from staged JSON."""

    merchant: str
    date: date
    total: Decimal
    date_is_placeholder: bool = False
    items: list[ReceiptItem] = field(default_factory=list)
    tax: Decimal | None = None
    subtotal: Decimal | None = None
    raw_text: str = ""
    image_filename: str = ""
    warnings: list[ReceiptWarning] = field(default_factory=list)
    tenders: list[Tender] = field(default_factory=list)

    @property
    def card_amount(self) -> Decimal | None:
        """Sum of card-typed tenders, or None when there are no tenders at all."""
        if not self.tenders:
            return None
        card_total = sum((t.amount for t in self.tenders if t.kind == "card"), Decimal("0"))
        return card_total if card_total > 0 else None

    @property
    def non_card_amount(self) -> Decimal:
        """Sum of non-card tenders (0 when there are none)."""
        return sum((t.amount for t in self.tenders if t.kind != "card"), Decimal("0"))

    @property
    def match_amount(self) -> Decimal:
        """Amount the matcher compares against ledger transactions: card tender if present, else total."""
        card_amount = self.card_amount
        return card_amount if card_amount is not None else self.total

    @property
    def itemized_total(self) -> Decimal:
        """Sum of item prices plus tax."""
        total = sum((item.price for item in self.items), Decimal("0"))
        if self.tax is not None:
            total += self.tax
        return total


@dataclass(frozen=True)
class MerchantFamily:
    """Canonical merchant identity plus aliases."""

    canonical: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class LedgerPosting:
    account: str
    number: Decimal | None
    currency: str | None


@dataclass(frozen=True)
class LedgerTransaction:
    """One Transaction entry loaded from the beancount ledger."""

    date: date
    payee: str | None
    narration: str | None
    postings: tuple[LedgerPosting, ...]
    file_path: str
    line_number: int

    @property
    def charge_amount(self) -> Decimal | None:
        """Absolute value of the first negative posting amount (the card charge)."""
        for posting in self.postings:
            if posting.number is not None and posting.number < 0:
                return abs(posting.number)
        return None

    @property
    def charge_account(self) -> str | None:
        for posting in self.postings:
            if posting.number is not None and posting.number < 0:
                return posting.account
        return None


@dataclass(frozen=True)
class Candidate:
    """One candidate ledger transaction match for a receipt."""

    transaction: LedgerTransaction
    confidence: float
    details: str
    strength: Literal["strict", "relaxed", "fallback"] = "strict"
