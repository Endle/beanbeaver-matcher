from fava.application import create_app


def _setup_import_extension(tmp_path):
    receipts_dir = tmp_path / "receipts"
    receipts_dir.mkdir()
    imports_dir = tmp_path / "imports"
    imports_dir.mkdir()
    statement = imports_dir / "CIBC.csv"
    statement.write_text("Date,Description,Debit\n2024-01-05,COSTCO,12.34\n")
    records_dir = tmp_path / "records"
    year_dir = records_dir / "2024"
    year_dir.mkdir(parents=True)
    (year_dir / "2024.beancount").write_text("")
    ledger_path = tmp_path / "main.beancount"
    config = {
        "receipts": str(receipts_dir),
        "imports": str(imports_dir),
        "records": str(records_dir),
        "ledger": str(ledger_path),
    }
    ledger_path.write_text(
        'option "title" "Test"\n'
        f'2000-01-01 custom "fava-extension" "beanbeaver_matcher.fava_ext" "{config}"\n'
        'include "records/2024/2024.beancount"\n\n'
        "2020-01-01 open Liabilities:CreditCard:CIBC:Primary CAD\n"
        "2020-01-01 open Expenses:Food:Grocery CAD\n"
        "2020-01-01 open Expenses:Uncategorized CAD\n"
    )
    return ledger_path, records_dir


def test_imports_extension_lists_plans_and_applies_a_statement(tmp_path):
    ledger_path, records_dir = _setup_import_extension(tmp_path)
    client = create_app([ledger_path]).test_client()

    report = client.get("/test/extension/ImportsExtension/")
    assert report.status_code == 200
    assert b"CIBC.csv" in report.data

    plan_response = client.get(
        "/test/extension/ImportsExtension/plan",
        query_string={"source_id": "CIBC.csv"},
    )
    assert plan_response.status_code == 200
    plan = plan_response.get_json()
    assert plan["status"] == "ready"
    assert plan["account"] == "Liabilities:CreditCard:CIBC:Primary"
    assert plan["transactions"][0]["category"] == "Expenses:Food:Grocery"

    apply_response = client.post(
        "/test/extension/ImportsExtension/apply",
        json={
            "source_id": plan["source_id"],
            "source_sha256": plan["source_sha256"],
            "importer_id": plan["importer_id"],
            "account": plan["account"],
            "edits": [],
        },
    )
    assert apply_response.status_code == 200
    result = apply_response.get_json()
    assert result["status"] == "applied"
    assert result["transaction_count"] == 1
    assert (records_dir / "2024" / "cibc_primary_0105_0105.beancount").is_file()


def test_imports_extension_rejects_paths_outside_import_directory(tmp_path):
    ledger_path, _ = _setup_import_extension(tmp_path)
    client = create_app([ledger_path]).test_client()

    response = client.get(
        "/test/extension/ImportsExtension/plan",
        query_string={"source_id": "../outside.csv"},
    )
    assert response.status_code == 400
    assert "inside the configured imports directory" in response.get_json()["error"]


def test_imports_extension_lists_new_wealthsimple_chequing_export(tmp_path):
    ledger_path, _ = _setup_import_extension(tmp_path)
    imports_dir = tmp_path / "imports"
    (imports_dir / "activities-export-2026-09-04.csv").write_text(
        "effective_date,account_type,activity_type,description,currency,net_cash_amount\n"
        "2026-09-03,Chequing,MoneyMovement,Direct deposit received,CAD,2524.06\n"
    )
    with ledger_path.open("a") as handle:
        handle.write("2020-01-01 open Assets:Bank:Chequing:Wealthsimple CAD\n2020-01-01 open Income:Salary CAD\n")

    client = create_app([ledger_path]).test_client()
    report = client.get("/test/extension/ImportsExtension/")

    assert report.status_code == 200
    assert b"activities-export-2026-09-04.csv" in report.data
    assert b"WEALTHSIMPLE CHEQUING" in report.data

    plan_response = client.get(
        "/test/extension/ImportsExtension/plan",
        query_string={"source_id": "activities-export-2026-09-04.csv"},
    )
    assert plan_response.status_code == 200
    plan = plan_response.get_json()
    assert plan["account"] == "Assets:Bank:Chequing:Wealthsimple"
    assert plan["transactions"][0]["amount"] == "-2524.06"
