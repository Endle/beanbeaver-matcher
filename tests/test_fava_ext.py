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


def test_duplicate_warning_and_recoverable_deletion(tmp_path):
    from beanbeaver_matcher.receipts import list_approved_receipts, receipt_sha256

    stage_path = _write_receipt(tmp_path)
    ledger_path = _write_ledger(tmp_path, tmp_path / "receipts")
    enriched = tmp_path / "_enriched" / "earlier.beancount"
    enriched.parent.mkdir()
    enriched.write_text(ledger_path.read_text())
    ledger_path.write_text('option "title" "Test"\ninclude "_enriched/earlier.beancount"\n')
    ledger_before = ledger_path.read_bytes(), enriched.read_bytes()
    receipt_before = stage_path.read_bytes()
    client = create_app([ledger_path]).test_client()
    report = client.get("/test/extension/MatcherExtension/")
    assert b"Possible duplicate" in report.data
    assert b"Delete duplicate receipt" in report.data
    assert str(enriched).encode() in report.data

    endpoint = "/test/extension/MatcherExtension/delete-duplicate"
    payload = {"stage_path": str(stage_path), "source_sha256": "stale"}
    assert client.post(endpoint, json=payload).status_code == 409
    assert stage_path.exists()
    outside = tmp_path / "outside.json"
    outside.write_bytes(receipt_before)
    assert client.post(endpoint, json={"stage_path": str(outside)}).status_code == 409
    assert outside.exists()

    payload["source_sha256"] = receipt_sha256(stage_path)
    response = client.post(endpoint, json=payload)
    assert response.status_code == 200
    assert response.get_json()["status"] == "deleted"
    assert not stage_path.exists()
    archived = list((tmp_path / "receipts" / ".trash").glob("*/stages/010_review.receipt.json"))
    assert len(archived) == 1
    assert archived[0].read_bytes() == receipt_before
    assert list_approved_receipts(tmp_path / "receipts") == []
    assert (ledger_path.read_bytes(), enriched.read_bytes()) == ledger_before
    assert client.post(endpoint, json=payload).status_code == 409


def test_delete_duplicate_rejects_unmatched_receipt(tmp_path):
    from beanbeaver_matcher.receipts import receipt_sha256

    stage_path = _write_receipt(tmp_path)
    ledger_path = _write_ledger(tmp_path, tmp_path / "receipts")
    client = create_app([ledger_path]).test_client()
    response = client.post(
        "/test/extension/MatcherExtension/delete-duplicate",
        json={"stage_path": str(stage_path), "source_sha256": receipt_sha256(stage_path)},
    )
    assert response.status_code == 409
    assert stage_path.exists()


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

    # The candidates panel renders the entry as beancount, so it needs the whole posting
    # list, the flag, and which account the charge actually lands on.
    top = data["candidates"][0]
    assert top["flag"] == "*"
    assert top["charge_account"] == "Liabilities:CreditCard:CardA"
    assert [(posting["account"], posting["number"]) for posting in top["postings"]] == [
        ("Liabilities:CreditCard:CardA", "-54.20"),
        ("Expenses:Uncategorized", "54.20"),
    ]


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
    assert b"No receipts awaiting review or a match." in report.data


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
    assert b"Review receipt" in report.data

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


def test_review_scanned_receipt_preserves_source_and_matches_edited_accounts(tmp_path):
    from pathlib import Path
    from beanbeaver_matcher.receipts import read_receipt, list_approved_receipts

    source = _write_receipt(tmp_path)
    document = json.loads(source.read_text())
    document["meta"]["stage"] = "scanned"
    document["review"] = None
    document["items"][0]["review"] = None
    document["items"].append({"id": "removed", "description": "BAD OCR", "price": "9", "review": {"removed": True}})
    document["warnings"] = [{"message": "Check OCR"}]
    source.write_text(json.dumps(document))
    original = source.read_bytes()
    ledger = _write_ledger(tmp_path, tmp_path / "receipts")
    client = create_app([ledger]).test_client()
    base = "/test/extension/MatcherExtension/"
    assert b"Review receipt" in client.get(base).data
    assert not list_approved_receipts(tmp_path / "receipts")
    assert client.get(base + "candidates", query_string={"stage_path": str(source)}).status_code == 409
    edit = client.get(base + "receipt", query_string={"stage_path": str(source)}).get_json()
    assert edit["raw_text"] == "COSTCO"
    assert edit["warnings"] == ["Check OCR"]
    assert len(edit["items"]) == 1
    edit["merchant"] = "COSTCO reviewed"
    edit["items"][0]["category"] = "Expenses:Uncategorized"
    response = client.post(base + "edit-receipt", json=edit)
    assert response.status_code == 200
    saved = response.get_json()
    reviewed = Path(saved["stage_path"])
    assert reviewed != source
    assert source.read_bytes() == original
    assert read_receipt(reviewed).items[0].category == "Expenses:Uncategorized"
    assert json.loads(reviewed.read_text())["items"][0]["description"] == "COKE ZERO"
    assert len(list_approved_receipts(tmp_path / "receipts")) == 1
    assert client.post(base + "edit-receipt", json=edit).status_code == 409
    assert client.get(base + "receipt", query_string={"stage_path": str(source)}).status_code == 400
    candidate = client.get(base + "candidates", query_string={"stage_path": str(reviewed)}).get_json()["candidates"][0]
    applied = client.post(base + "apply", json={"stage_path": str(reviewed), **candidate})
    assert applied.get_json()["status"] == "applied"
    assert "Expenses:Uncategorized" in Path(applied.get_json()["enriched_path"]).read_text()
    assert client.post(base + "edit-receipt", json=saved).status_code == 409


def test_staged_review_validation_and_item_removal(tmp_path):
    from pathlib import Path
    from beanbeaver_matcher.receipts import read_receipt

    source = _write_receipt(tmp_path)
    ledger = _write_ledger(tmp_path, tmp_path / "receipts")
    client = create_app([ledger]).test_client()
    base = "/test/extension/MatcherExtension/"
    edit = client.get(base + "receipt", query_string={"stage_path": str(source)}).get_json()
    original = source.read_bytes()
    for patch in ({"tax": "bad"}, {"source_sha256": "stale"}, {"total": "NaN"}):
        assert client.post(base + "edit-receipt", json={**edit, **patch}).status_code == 409
        assert source.read_bytes() == original
    edit["items"] = [
        {
            "description": "Replacement",
            "price": "54.20",
            "quantity": 1,
            "category": "Expenses:Uncategorized",
            "source_index": None,
        }
    ]
    saved = client.post(base + "edit-receipt", json=edit).get_json()
    path = Path(saved["stage_path"])
    assert [item.description for item in read_receipt(path).items] == ["Replacement"]
    assert saved["items"][0]["source_index"] == 1
    assert json.loads(path.read_text())["items"][0]["review"]["removed"] is True
    saved["tax"] = ""
    assert client.post(base + "edit-receipt", json=saved).status_code == 200
    assert client.get(base + "receipt", query_string={"stage_path": str(ledger)}).status_code == 400
