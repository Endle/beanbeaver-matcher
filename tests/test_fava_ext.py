"""Smoke test driving the Fava extension end-to-end through a real Flask test client."""

import json

from fava.application import create_app


def _write_receipt(tmp_path, *, total="54.20"):
    stages_dir = tmp_path / "receipts" / "2024-01-05_costco_54_20_aaaa" / "stages"
    stages_dir.mkdir(parents=True)
    document = {
        "meta": {"schema_version": "2", "receipt_id": "r-1", "stage": "review_stage_1", "stage_index": 1},
        "receipt": {"merchant": "COSTCO", "date": "2024-01-05", "currency": "CAD", "total": total},
        "items": [{"id": "item-0001", "description": "COKE ZERO", "price": total, "quantity": 1}],
        "warnings": [],
        "tenders": [],
        "raw_text": "COSTCO",
    }
    stage_path = stages_dir / "010_review.receipt.json"
    stage_path.write_text(json.dumps(document))
    return stage_path


def _write_ledger(tmp_path, receipts_dir):
    ledger_path = tmp_path / "main.beancount"
    ledger_path.write_text(
        f"""
option "title" "Test"

2000-01-01 custom "fava-extension" "beanbeaver_matcher.fava_ext" "{{'receipts': '{receipts_dir}'}}"

2024-01-01 open Liabilities:CreditCard:CardA
2024-01-01 open Expenses:Uncategorized
2024-01-01 open Expenses:FIXME

2024-01-05 * "COSTCO" "Groceries"
  Liabilities:CreditCard:CardA  -54.20 CAD
  Expenses:Uncategorized         54.20 CAD
"""
    )
    return ledger_path


def test_extension_report_lists_approved_receipt(tmp_path):
    stage_path = _write_receipt(tmp_path)
    ledger_path = _write_ledger(tmp_path, tmp_path / "receipts")

    app = create_app([ledger_path])
    client = app.test_client()

    response = client.get("/test/extension/MatcherExtension/")
    assert response.status_code == 200
    assert b"COSTCO" in response.data
    assert str(stage_path).encode() in response.data


def test_extension_candidates_endpoint_returns_ranked_match(tmp_path):
    stage_path = _write_receipt(tmp_path)
    ledger_path = _write_ledger(tmp_path, tmp_path / "receipts")

    app = create_app([ledger_path])
    client = app.test_client()

    response = client.get(
        "/test/extension/MatcherExtension/candidates",
        query_string={"stage_path": str(stage_path)},
    )
    assert response.status_code == 200
    data = response.get_json()
    assert data["candidates"], data
    assert data["candidates"][0]["file_path"] == str(ledger_path)
    assert data["candidates"][0]["confidence"] > 0.9


def test_extension_apply_endpoint_writes_enriched_entry_and_archives_receipt(tmp_path):
    stage_path = _write_receipt(tmp_path)
    ledger_path = _write_ledger(tmp_path, tmp_path / "receipts")

    app = create_app([ledger_path])
    client = app.test_client()

    candidates = client.get(
        "/test/extension/MatcherExtension/candidates",
        query_string={"stage_path": str(stage_path)},
    ).get_json()
    candidate = candidates["candidates"][0]

    response = client.post(
        "/test/extension/MatcherExtension/apply",
        json={
            "stage_path": str(stage_path),
            "file_path": candidate["file_path"],
            "line_number": candidate["line_number"],
        },
    )
    assert response.status_code == 200
    result = response.get_json()
    assert result["status"] == "applied"
    assert result["enriched_path"] is not None

    ledger_text = ledger_path.read_text()
    assert 'include "_enriched/' in ledger_text

    # The approved-receipts list is now empty since the receipt was archived.
    report = client.get("/test/extension/MatcherExtension/")
    assert b"No approved receipts awaiting a match." in report.data
