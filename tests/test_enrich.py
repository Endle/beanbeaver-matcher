from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from beanbeaver_matcher.enrich import format_enriched_transaction
from beanbeaver_matcher.model import Candidate, LedgerPosting, LedgerTransaction, Receipt, ReceiptItem, Tender


def _receipt(**overrides: Any) -> Receipt:
    defaults: dict[str, Any] = dict(
        merchant="COSTCO",
        date=date(2026, 2, 18),
        total=Decimal("20.00"),
        image_filename="costco.jpg",
    )
    defaults.update(overrides)
    return Receipt(**defaults)


def _txn(**overrides: Any) -> LedgerTransaction:
    defaults: dict[str, Any] = dict(
        date=date(2026, 2, 20),
        payee="COSTCO WHOLESALE",
        narration="Purchase",
        postings=(
            LedgerPosting(account="Liabilities:CreditCard:Visa", number=Decimal("-20.00"), currency="CAD"),
            LedgerPosting(account="Expenses:Uncategorized", number=Decimal("20.00"), currency="CAD"),
        ),
        file_path="ledger.beancount",
        line_number=42,
    )
    defaults.update(overrides)
    return LedgerTransaction(**defaults)


def test_enriched_transaction_reuses_match_and_itemizes():
    receipt = _receipt(
        tax=Decimal("1.00"),
        items=[ReceiptItem(description="COKE ZERO", price=Decimal("17.19"), quantity=1)],
    )
    candidate = Candidate(transaction=_txn(), confidence=0.9, details="amount+date")

    out = format_enriched_transaction(receipt, candidate)

    assert "; === ENRICHED TRANSACTION - REVIEW NEEDED ===" in out
    assert "; Receipt: costco.jpg" in out
    assert "; Matched: ledger.beancount:42" in out
    assert "; Confidence: 90% (amount+date)" in out
    assert '2026-02-20 * "COSTCO WHOLESALE" "Purchase"' in out
    assert "Liabilities:CreditCard:Visa" in out
    assert "-20.00 CAD" in out
    assert "Expenses:Food:Grocery:Drink:CocaCola" not in out  # no category resolved in v1
    assert "; remaining/unitemized" in out
    assert "; --- Original Transaction (to be replaced) ---" in out


def test_enriched_transaction_falls_back_to_fixme_expense_for_uncategorized_items():
    receipt = _receipt(items=[ReceiptItem(description="COKE ZERO", price=Decimal("20.00"), quantity=1)])
    candidate = Candidate(transaction=_txn(), confidence=0.9, details="amount+date")

    out = format_enriched_transaction(receipt, candidate)

    assert "Expenses:Uncategorized" in out  # reused from the matched transaction's original posting
    assert "20.00 CAD" in out


def test_enriched_transaction_renders_multiple_tenders():
    receipt = _receipt(
        raw_text="**** 9999",
        total=Decimal("20.00"),
        tenders=[
            Tender(amount=Decimal("15.00"), kind="card"),
            Tender(amount=Decimal("5.00"), account="Assets:GiftCards:Costco", kind="gift_card"),
        ],
        items=[ReceiptItem(description="ITEM", price=Decimal("20.00"), quantity=1)],
    )
    candidate = Candidate(transaction=_txn(), confidence=0.9, details="amount+date")

    out = format_enriched_transaction(receipt, candidate)

    assert "Liabilities:CreditCard:Visa" in out
    assert "-15.00 CAD" in out
    assert "; card ****9999" in out
    assert "Assets:GiftCards:Costco" in out
    assert "-5.00 CAD" in out
    assert "; gift card" in out


def test_enriched_transaction_falls_back_to_fixme_cc_account_when_no_negative_posting():
    receipt = _receipt(items=[ReceiptItem(description="ITEM", price=Decimal("20.00"), quantity=1)])
    txn = _txn(postings=(LedgerPosting(account="Expenses:Uncategorized", number=Decimal("20.00"), currency="CAD"),))
    candidate = Candidate(transaction=txn, confidence=0.9, details="amount")

    out = format_enriched_transaction(receipt, candidate)

    assert "Liabilities:CreditCard:FIXME" in out
    assert "-20.00 CAD" in out


def test_enriched_transaction_warns_when_items_exceed_transaction():
    receipt = _receipt(
        total=Decimal("10.00"),
        items=[ReceiptItem(description="ITEM", price=Decimal("50.00"), quantity=1)],
    )
    txn = _txn(
        postings=(
            LedgerPosting(account="Liabilities:CreditCard:Visa", number=Decimal("-10.00"), currency="CAD"),
            LedgerPosting(account="Expenses:Uncategorized", number=Decimal("10.00"), currency="CAD"),
        )
    )
    candidate = Candidate(transaction=txn, confidence=0.9, details="amount")

    out = format_enriched_transaction(receipt, candidate)

    assert "WARNING: items total (50.00) exceeds transaction (10.00)" in out


@pytest.mark.parametrize("discount_first", [True, False])
@pytest.mark.parametrize("split", [True, False])
def test_negative_discount_never_replaces_card_posting(discount_first, split):
    discount = LedgerPosting("Expenses:FIXME", Decimal("-25.00"), "CAD")
    postings = list(_txn().postings)
    postings.insert(0 if discount_first else len(postings), discount)
    txn = _txn(postings=tuple(postings))
    assert txn.charge_account == "Liabilities:CreditCard:Visa"
    assert txn.charge_amount == Decimal("20.00")
    receipt = _receipt(
        total=Decimal("25.00") if split else Decimal("20.00"),
        tenders=[Tender(Decimal("20.00")), Tender(Decimal("5.00"), "Assets:GiftCards:Costco", "gift_card")]
        if split
        else [],
    )
    out = format_enriched_transaction(receipt, Candidate(txn, 0.98, "exact"))
    active = out.split("; --- Original Transaction")[0]
    card_line = next(line for line in active.splitlines() if line.startswith("  Liabilities:"))
    assert "Liabilities:CreditCard:Visa" in card_line
    assert "-20.00 CAD" in card_line


def test_expense_refund_is_not_a_card_charge():
    txn = _txn(
        postings=(
            LedgerPosting("Expenses:FIXME", Decimal("-20.00"), "CAD"),
            LedgerPosting("Liabilities:CreditCard:Visa", Decimal("20.00"), "CAD"),
        )
    )
    assert txn.charge_amount is None
