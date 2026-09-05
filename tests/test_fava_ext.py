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


def test_extension_edits_legacy_split_tenders_and_recalculates_match(tmp_path):
    receipts_dir = tmp_path / "receipts"
    receipts_dir.mkdir()
    legacy_root = tmp_path / "beanbeaver_receipts"
    chain_dir = legacy_root / "2024-01-05_costco_54_20_aaaa"
    chain_dir.mkdir(parents=True)
    receipt_path = chain_dir / "2024-01-05_costco_54_20_aaaa.json"
    receipt_path.write_text(
        json.dumps(
            {
                "merchant": "COSTCO",
                "date": "2024-01-05",
                "dateIsPlaceholder": False,
                "subtotal": "54.20",
                "tax": None,
                "total": "54.20",
                "items": [{"description": "GROCERIES", "price": "54.20", "quantity": 1, "category": None}],
                "warnings": [],
            }
        )
    )
    receipt_path.with_suffix(".beancount").write_text(
        '2024-01-05 * "COSTCO" "Receipt scan"\n'
        "  Liabilities:CreditCard:PENDING  -54.20 CAD\n"
        "  Expenses:Food:Grocery            54.20 CAD  ; GROCERIES\n"
    )
    ledger_path = tmp_path / "main.beancount"
    config = {"receipts": str(receipts_dir), "legacy_receipts": str(legacy_root)}
    ledger_path.write_text(
        'option "title" "Test"\n'
        f'2000-01-01 custom "fava-extension" "beanbeaver_matcher.fava_ext" "{config}"\n\n'
        "2024-01-01 open Liabilities:CreditCard:CardA\n"
        "2024-01-01 open Assets:PrepaidCard:GiftCard\n"
        "2024-01-01 open Expenses:Food:Grocery\n\n"
        '2024-01-05 * "COSTCO" "Groceries"\n'
        "  Liabilities:CreditCard:CardA  -44.20 CAD\n"
        "  Expenses:Food:Grocery          44.20 CAD\n"
    )
    client = create_app([ledger_path]).test_client()

    report = client.get("/test/extension/MatcherExtension/")
    assert report.status_code == 200
    assert b"Edit receipt" in report.data

    edit_response = client.get(
        "/test/extension/MatcherExtension/receipt",
        query_string={"stage_path": str(receipt_path)},
    )
    edit = edit_response.get_json()
    assert edit_response.status_code == 200
    assert edit["items"][0]["category"] == "Expenses:Food:Grocery"

    edit["tenders"] = [
        {"kind": "gift_card", "amount": "10.00", "account": "Assets:PrepaidCard:GiftCard", "raw_label": "Shop Card"},
        {"kind": "card", "amount": "44.20", "account": "", "raw_label": "Mastercard"},
    ]
    save_response = client.post("/test/extension/MatcherExtension/edit-receipt", json=edit)
    assert save_response.status_code == 200
    assert save_response.get_json()["status"] == "saved"

    candidates_response = client.get(
        "/test/extension/MatcherExtension/candidates",
        query_string={"stage_path": str(receipt_path)},
    )
    candidates = candidates_response.get_json()["candidates"]
    assert candidates_response.status_code == 200
    assert candidates[0]["amount"] == "44.20"
    assert candidates[0]["confidence"] > 0.9

    # Add a row, then remove the first row: metadata must stay with its source item.
    edit = save_response.get_json()
    edit["items"].append(
        {
            "source_index": None,
            "description": "NEW ITEM",
            "price": "5.00",
            "quantity": 1,
            "category": "Expenses:Food:Grocery",
        }
    )
    added = client.post("/test/extension/MatcherExtension/edit-receipt", json=edit)
    assert added.status_code == 200
    assert len(added.get_json()["items"]) == 2
    document = json.loads(receipt_path.read_text())
    document["items"][1]["ocr_note"] = "keep with new item"
    receipt_path.write_text(json.dumps(document))
    edit = client.get(
        "/test/extension/MatcherExtension/receipt", query_string={"stage_path": str(receipt_path)}
    ).get_json()
    edit["items"].pop(0)
    # Removing a split tender is supported when the remaining card covers the total.
    edit["tenders"] = [{"kind": "card", "amount": "54.20", "account": "", "raw_label": "Card"}]
    removed = client.post("/test/extension/MatcherExtension/edit-receipt", json=edit)
    assert removed.status_code == 200
    saved = json.loads(receipt_path.read_text())
    assert len(saved["items"]) == 1
    assert saved["items"][0]["description"] == "NEW ITEM"
    assert saved["items"][0]["ocr_note"] == "keep with new item"
    assert len(saved["tenders"]) == 1
