"""Ported from beanbeaver's src/matcher.rs `#[cfg(test)] mod tests`, plus fallback-tiering cases."""

from datetime import date
from decimal import Decimal

from beanbeaver_matcher.model import LedgerPosting, LedgerTransaction, MerchantFamily, Receipt
from beanbeaver_matcher.scoring import (
    match_receipt_to_transactions,
    merchant_similarity,
    resolve_candidates,
)


def _receipt(
    merchant: str,
    total: str,
    *,
    date_value: date = date(2026, 3, 1),
    date_is_placeholder: bool = False,
) -> Receipt:
    return Receipt(merchant=merchant, date=date_value, total=Decimal(total), date_is_placeholder=date_is_placeholder)


def _txn(
    *,
    date_value: date,
    payee: str | None,
    amount: str,
    file_path: str = "/tmp/ledger.beancount",
    line_number: int = 1,
) -> LedgerTransaction:
    return LedgerTransaction(
        date=date_value,
        payee=payee,
        narration="",
        postings=(LedgerPosting(account="Liabilities:CreditCard", number=Decimal(amount), currency="CAD"),),
        file_path=file_path,
        line_number=line_number,
    )


def _real_canadian_families() -> list[MerchantFamily]:
    return [MerchantFamily(canonical="REAL CANADIAN SUPERSTORE", aliases=("REAL CANADIAN", "RCSS"))]


def test_merchant_similarity_handles_common_substrings():
    assert merchant_similarity("T&T", "T&T SUPERMARKET") > 0.8


def test_merchant_similarity_handles_family_aliases():
    assert merchant_similarity("REAL CANADIAN", "RCSS", _real_canadian_families()) > 0.8


def test_receipt_transaction_matching_returns_none_for_positive_amounts():
    receipt = _receipt("T&T", "100.00", date_value=date(2024, 1, 1))
    txn = _txn(date_value=date(2024, 1, 1), payee="T&T SUPERMARKET", amount="100.00")  # positive, not a charge

    matches = match_receipt_to_transactions(receipt, [txn])

    assert matches == []


def test_transaction_receipt_matching_reports_family_match_details():
    receipt = _receipt("REAL CANADIAN", "73.63", date_value=date(2024, 1, 1))
    txn = _txn(date_value=date(2024, 1, 4), payee="RCSS", amount="-73.63")

    matches = match_receipt_to_transactions(receipt, [txn], merchant_families=_real_canadian_families())

    assert len(matches) == 1
    assert "merchant: family match (REAL CANADIAN SUPERSTORE)" in matches[0].details


def test_public_match_receipt_to_transactions_sorts_by_confidence_then_index():
    receipt = _receipt("T&T", "100.00", date_value=date(2024, 1, 1))
    transactions = [
        _txn(date_value=date(2024, 1, 1), payee="T&T SUPERMARKET", amount="-100.00", line_number=1),
        _txn(date_value=date(2024, 1, 1), payee="T&T SUPERMARKET", amount="-100.00", line_number=2),
    ]

    matches = match_receipt_to_transactions(receipt, transactions)

    assert len(matches) == 2
    assert matches[0].transaction.line_number == 1
    assert matches[1].transaction.line_number == 2
    assert matches[0].confidence == matches[1].confidence


def test_public_match_transaction_to_receipts_preserves_unknown_date_details():
    receipt = _receipt("T&T", "100.00", date_value=date(2024, 1, 1), date_is_placeholder=True)
    txn = _txn(date_value=date(2024, 1, 2), payee="T&T SUPERMARKET", amount="-100.00")

    matches = match_receipt_to_transactions(receipt, [txn])

    assert len(matches) == 1
    assert "date: unknown" in matches[0].details


def test_resolve_candidates_falls_back_to_relaxed_when_strict_finds_nothing():
    # Amount is $3 off: outside strict's tolerance (max(0.10, 1% of 50.00) = 0.50) but
    # within relaxed's (max(2.00, 8% of 50.00) = 4.00). Merchant matches well either way.
    receipt = _receipt("T&T", "50.00", date_value=date(2024, 1, 1))
    txn = _txn(date_value=date(2024, 1, 1), payee="T&T SUPERMARKET", amount="-53.00")

    resolved = resolve_candidates(receipt, [txn])

    assert resolved.used_relaxed_threshold is True
    assert resolved.candidates
    assert resolved.candidates[0].strength == "relaxed"


def test_resolve_candidates_uses_amount_date_fallback_when_merchant_is_unrelated():
    receipt = _receipt("FRESH", "91.22", date_value=date(2026, 3, 3))
    txn = _txn(date_value=date(2026, 3, 4), payee="FOODY MART", amount="-91.22")

    resolved = resolve_candidates(receipt, [txn])

    assert resolved.used_relaxed_threshold is True
    assert resolved.candidates
    assert resolved.candidates[0].strength == "fallback"
    assert resolved.candidates[0].transaction.date == date(2026, 3, 4)


def test_resolve_candidates_reports_no_candidates_when_amount_is_out_of_range():
    receipt = _receipt("FRESH", "10.00", date_value=date(2026, 3, 3))
    txn = _txn(date_value=date(2026, 3, 4), payee="FOODY MART", amount="-999.99")

    resolved = resolve_candidates(receipt, [txn])

    assert resolved.candidates == []
    assert resolved.warning == "No reliable matches found, and no weaker fallback candidates were found."
