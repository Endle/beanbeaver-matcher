import json
from datetime import date
from decimal import Decimal

from beanbeaver_matcher.receipts import (
    list_approved_receipts,
    receipt_chain_name,
    receipt_from_stage_document,
)


def _base_document() -> dict:
    return {
        "meta": {
            "schema_version": "2",
            "receipt_id": "r-1",
            "stage": "review_stage_1",
            "stage_index": 1,
            "image_filename": "costco.jpg",
        },
        "receipt": {
            "merchant": "COSTCO",
            "date": "2026-03-07",
            "currency": "CAD",
            "subtotal": "455.00",
            "tax": "5.72",
            "total": "466.68",
        },
        "items": [
            {
                "id": "item-0001",
                "description": "COKE ZERO",
                "price": "17.19",
                "quantity": 1,
                "classification": {"category": "grocery_soda", "tags": []},
                "warnings": [],
            },
        ],
        "warnings": [],
        "tenders": [
            {"amount": "466.68", "account": None, "kind": "card", "raw_label": "MasterCard"},
        ],
        "raw_text": "COSTCO\nTOTAL 466.68",
    }


def test_receipt_from_stage_document_reads_effective_fields():
    receipt = receipt_from_stage_document(_base_document())

    assert receipt.merchant == "COSTCO"
    assert receipt.date == date(2026, 3, 7)
    assert receipt.date_is_placeholder is False
    assert receipt.total == Decimal("466.68")
    assert receipt.tax == Decimal("5.72")
    assert len(receipt.items) == 1
    assert receipt.items[0].description == "COKE ZERO"
    assert receipt.items[0].price == Decimal("17.19")
    # Category resolution is deferred to v1's Expenses:FIXME fallback in enrich.py.
    assert receipt.items[0].category is None
    assert len(receipt.tenders) == 1
    assert receipt.tenders[0].kind == "card"
    assert receipt.tenders[0].amount == Decimal("466.68")


def test_review_overlay_overrides_receipt_and_item_fields():
    document = _base_document()
    document["review"] = {"merchant": "COSTCO WHOLESALE", "total": "470.00"}
    document["items"][0]["review"] = {"price": "18.00"}

    receipt = receipt_from_stage_document(document)

    assert receipt.merchant == "COSTCO WHOLESALE"
    assert receipt.total == Decimal("470.00")
    assert receipt.items[0].price == Decimal("18.00")


def test_removed_item_and_tender_are_excluded():
    document = _base_document()
    document["items"].append(
        {"id": "item-0002", "description": "MILK", "price": "4.99", "quantity": 1, "review": {"removed": True}}
    )
    document["tenders"].append(
        {"amount": "10.00", "account": "Assets:GiftCards:Costco", "kind": "gift_card", "review": {"removed": True}}
    )

    receipt = receipt_from_stage_document(document)

    assert [item.description for item in receipt.items] == ["COKE ZERO"]
    assert len(receipt.tenders) == 1


def test_missing_date_yields_placeholder():
    document = _base_document()
    del document["receipt"]["date"]

    receipt = receipt_from_stage_document(document)

    assert receipt.date_is_placeholder is True
    assert receipt.date.day == 1


def test_list_approved_receipts_skips_scanned_and_matched(tmp_path):
    def _write_chain(name: str, stage_filename: str, stage: str, stage_index: int) -> None:
        chain_dir = tmp_path / name / "stages"
        chain_dir.mkdir(parents=True)
        document = _base_document()
        document["meta"]["stage"] = stage
        document["meta"]["stage_index"] = stage_index
        (chain_dir / stage_filename).write_text(json.dumps(document))

    _write_chain("2026-03-01_costco_466_68_aaaa", "000_parsed.receipt.json", "parsed", 0)
    _write_chain("2026-03-02_costco_100_00_bbbb", "010_review.receipt.json", "review_stage_1", 1)
    _write_chain("2026-03-03_costco_200_00_cccc", "900_matched.receipt.json", "matched", 2)

    approved = list_approved_receipts(tmp_path)

    assert len(approved) == 1
    assert approved[0].stage_path.name == "010_review.receipt.json"
    assert receipt_chain_name(approved[0].stage_path) == "2026-03-02_costco_100_00_bbbb"
