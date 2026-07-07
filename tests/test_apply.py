import json
from pathlib import Path

from beanbeaver_matcher.apply import apply_match
from beanbeaver_matcher.ledger import load_transactions
from beanbeaver_matcher.model import Candidate
from beanbeaver_matcher.receipts import load_stage_document, read_receipt, stage_status
from beanbeaver_matcher.scoring import resolve_candidates

LEDGER = """
2024-01-01 open Liabilities:CreditCard:CardA
2024-01-01 open Expenses:Uncategorized
2024-01-01 open Expenses:FIXME

2024-01-05 * "COSTCO" "Groceries"
  Liabilities:CreditCard:CardA  -54.20 CAD
  Expenses:Uncategorized         54.20 CAD
"""


def _write_ledger(tmp_path: Path) -> Path:
    ledger_path = tmp_path / "main.beancount"
    ledger_path.write_text(LEDGER)
    return ledger_path


def _write_receipt(tmp_path, *, merchant="COSTCO", date_str="2024-01-05", total="54.20", stage="review_stage_1"):
    stages_dir = tmp_path / "receipts" / "2024-01-05_costco_54_20_aaaa" / "stages"
    stages_dir.mkdir(parents=True)
    document = {
        "meta": {
            "schema_version": "2",
            "receipt_id": "r-1",
            "stage": stage,
            "stage_index": 1,
            "image_filename": "costco.jpg",
        },
        "receipt": {"merchant": merchant, "date": date_str, "currency": "CAD", "total": total},
        "items": [{"id": "item-0001", "description": "COKE ZERO", "price": total, "quantity": 1}],
        "warnings": [],
        "tenders": [],
        "raw_text": "COSTCO",
    }
    stage_path = stages_dir / "010_review.receipt.json"
    stage_path.write_text(json.dumps(document))
    return stage_path


def test_apply_match_replaces_transaction_writes_enriched_and_archives_receipt(tmp_path):
    ledger_path = _write_ledger(tmp_path)
    stage_path = _write_receipt(tmp_path)

    snapshot = load_transactions(ledger_path)
    receipt = read_receipt(stage_path)
    resolved = resolve_candidates(receipt, snapshot.transactions)
    assert resolved.candidates, "expected at least one candidate"
    candidate = resolved.candidates[0]

    result = apply_match(stage_path, candidate, ledger_path=ledger_path)

    assert result.status == "applied"
    assert result.enriched_path is not None
    assert result.enriched_path.exists()
    enriched_text = result.enriched_path.read_text()
    assert "COKE ZERO" in enriched_text
    assert "Liabilities:CreditCard:CardA" in enriched_text

    statement_text = (tmp_path / "main.beancount").read_text()
    assert 'include "_enriched/' in statement_text
    assert '; 2024-01-05 * "COSTCO" "Groceries"' in statement_text

    # Ledger still parses clean after the rewrite.
    reloaded = load_transactions(ledger_path)
    assert reloaded.errors == []

    assert result.matched_receipt_path is not None
    matched_document = load_stage_document(result.matched_receipt_path)
    assert stage_status(matched_document) == "matched"


def test_apply_match_is_idempotent_on_second_call(tmp_path):
    ledger_path = _write_ledger(tmp_path)
    stage_path = _write_receipt(tmp_path)

    snapshot = load_transactions(ledger_path)
    receipt = read_receipt(stage_path)
    candidate = resolve_candidates(receipt, snapshot.transactions).candidates[0]

    first = apply_match(stage_path, candidate, ledger_path=ledger_path)
    assert first.status == "applied"

    # Re-apply against the now-archived (matched) stage path with the same candidate.
    second = apply_match(stage_path, candidate, ledger_path=ledger_path)
    assert second.status == "already_applied"


def test_apply_match_rejects_itemized_total_exceeding_transaction(tmp_path):
    ledger_path = _write_ledger(tmp_path)
    stage_path = _write_receipt(tmp_path, total="54.20")
    # Bump the item price so the itemized total blows past the $54.20 charge.
    document = load_stage_document(stage_path)
    document["items"][0]["price"] = "999.00"
    stage_path.write_text(json.dumps(document))

    snapshot = load_transactions(ledger_path)
    txn = snapshot.transactions[0]
    candidate = Candidate(transaction=txn, confidence=0.9, details="amount: exact match")

    result = apply_match(stage_path, candidate, ledger_path=ledger_path)

    assert result.status == "receipt_total_exceeds_transaction"
    assert not (tmp_path / "_enriched").exists()


def test_apply_match_reports_target_missing_when_file_deleted(tmp_path):
    ledger_path = _write_ledger(tmp_path)
    stage_path = _write_receipt(tmp_path)

    snapshot = load_transactions(ledger_path)
    receipt = read_receipt(stage_path)
    candidate = resolve_candidates(receipt, snapshot.transactions).candidates[0]

    ledger_path.unlink()

    result = apply_match(stage_path, candidate, ledger_path=ledger_path)

    assert result.status == "target_missing"
