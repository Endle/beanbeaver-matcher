"""Verifies the launcher's temp-include wrapper: Fava starts on it, the extension loads,
and it sees the real ledger's transactions through the `include`."""

import json

from fava.application import create_app

from beanbeaver_matcher.ledger import load_transactions
from beanbeaver_matcher.launcher import build_wrapper_content


def test_build_wrapper_content_is_valid_beancount_and_includes_real_ledger(tmp_path):
    ledger_path = tmp_path / "main.beancount"
    ledger_path.write_text(
        "2024-01-01 open Assets:Checking\n"
        "2024-01-01 open Expenses:Food\n\n"
        '2024-01-05 * "Cafe" "Coffee"\n'
        "  Assets:Checking  -5.00 CAD\n"
        "  Expenses:Food     5.00 CAD\n"
    )
    receipts_dir = tmp_path / "receipts"
    receipts_dir.mkdir()

    wrapper_path = tmp_path / "_fava.beancount"
    wrapper_path.write_text(build_wrapper_content(ledger_path, receipts_dir, None))

    snapshot = load_transactions(wrapper_path)
    assert snapshot.errors == []
    assert len(snapshot.transactions) == 1
    # file_path/line_number reflect the *included* ledger, not the wrapper.
    assert snapshot.transactions[0].file_path == str(ledger_path)


def test_wrapper_boots_fava_with_matcher_extension_registered(tmp_path):
    ledger_path = tmp_path / "main.beancount"
    ledger_path.write_text("2024-01-01 open Assets:Checking\n2024-01-01 open Expenses:Food\n")
    receipts_dir = tmp_path / "receipts"
    receipts_dir.mkdir()
    (receipts_dir / "some-chain" / "stages").mkdir(parents=True)
    (receipts_dir / "some-chain" / "stages" / "010_review.receipt.json").write_text(
        json.dumps(
            {
                "meta": {"stage": "review_stage_1", "stage_index": 1},
                "receipt": {"merchant": "CAFE", "date": "2024-01-05", "total": "5.00"},
                "items": [],
                "warnings": [],
                "tenders": [],
                "raw_text": "",
            }
        )
    )

    wrapper_path = tmp_path / "_fava.beancount"
    wrapper_path.write_text(build_wrapper_content(ledger_path, receipts_dir, None))

    app = create_app([wrapper_path])
    client = app.test_client()

    response = client.get("/beanbeaver-matcher/extension/MatcherExtension/")
    assert response.status_code == 200
    assert b"CAFE" in response.data
