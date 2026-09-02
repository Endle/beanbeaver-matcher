from decimal import Decimal

import pytest

from beanbeaver_matcher.credit_card import (
    AccountSelectionRequired,
    ImportApplyError,
    TransactionEdit,
    apply_credit_card_import,
    plan_credit_card_import,
)
from beanbeaver_matcher.credit_card.service import render_credit_card_plan


def _write_ledger(tmp_path, *, second_card=False, existing_transaction=False):
    records_dir = tmp_path / "records"
    year_dir = records_dir / "2024"
    year_dir.mkdir(parents=True)
    (year_dir / "2024.beancount").write_text("")
    second_open = "2020-01-01 open Liabilities:CreditCard:CIBC:Backup CAD\n" if second_card else ""
    existing = (
        '2024-01-05 * "COSTCO" ""\n'
        "  Liabilities:CreditCard:CIBC:Primary  -12.34 CAD\n"
        "  Expenses:Food:Grocery                 12.34 CAD\n"
        if existing_transaction
        else ""
    )
    ledger_path = tmp_path / "main.beancount"
    ledger_path.write_text(
        'include "records/2024/2024.beancount"\n\n'
        "2020-01-01 open Liabilities:CreditCard:CIBC:Primary CAD\n"
        f"{second_open}"
        "2020-01-01 open Expenses:Food:Grocery CAD\n"
        "2020-01-01 open Expenses:Uncategorized CAD\n"
        "2020-01-01 open Expenses:Shopping:NotAssigned CAD\n\n"
        f"{existing}"
    )
    return ledger_path, records_dir


def _write_statement(tmp_path):
    statement = tmp_path / "CIBC.csv"
    statement.write_text("Date,Description,Debit\n2024-01-05,COSTCO,12.34\n2024-01-06,CAFE,4.00\n")
    return statement


def test_plan_requires_account_choice_when_multiple_cards_match(tmp_path):
    ledger_path, _ = _write_ledger(tmp_path, second_card=True)
    statement = _write_statement(tmp_path)

    with pytest.raises(AccountSelectionRequired) as error:
        plan_credit_card_import(statement, ledger_path=ledger_path)

    assert error.value.options == (
        "Liabilities:CreditCard:CIBC:Backup",
        "Liabilities:CreditCard:CIBC:Primary",
    )


def test_plan_marks_exact_ledger_duplicates_and_requires_an_explicit_decision(tmp_path):
    ledger_path, _ = _write_ledger(tmp_path, existing_transaction=True)
    statement = _write_statement(tmp_path)
    plan = plan_credit_card_import(statement, ledger_path=ledger_path)

    assert plan.transactions[0].duplicate is True
    assert plan.transactions[1].duplicate is False
    with pytest.raises(ImportApplyError, match="explicitly delete or edit"):
        render_credit_card_plan(plan, ())

    output, count = render_credit_card_plan(
        plan,
        (TransactionEdit(row_id="1", category=plan.transactions[0].category, deleted=True),),
    )
    assert count == 1
    assert '"CAFE"' in output
    assert '"COSTCO"' not in output


def test_apply_writes_reachable_statement_and_is_idempotent_by_source_hash(tmp_path):
    ledger_path, records_dir = _write_ledger(tmp_path)
    statement = _write_statement(tmp_path)
    plan = plan_credit_card_import(statement, ledger_path=ledger_path)
    edits = (
        TransactionEdit(row_id="2", category="Expenses:Food:Grocery", new_amount=Decimal("4.50")),
    )

    result = apply_credit_card_import(
        statement,
        ledger_path=ledger_path,
        records_dir=records_dir,
        selected_account=plan.account,
        expected_source_sha256=plan.source_sha256,
        edits=edits,
        importer_id=plan.importer_id,
    )

    assert result.status == "applied"
    assert result.transaction_count == 2
    output = result.output_path.read_text()
    assert f'bb_source: "{plan.source_sha256}"' in output
    assert "Expenses:Food:Grocery  4.50 CAD" in output
    assert f'include "{result.output_path.name}"' in (records_dir / "2024" / "2024.beancount").read_text()

    repeated = apply_credit_card_import(
        statement,
        ledger_path=ledger_path,
        records_dir=records_dir,
        selected_account=plan.account,
        expected_source_sha256=plan.source_sha256,
        edits=(),
        importer_id=plan.importer_id,
    )
    assert repeated.status == "already_applied"


def test_apply_rejects_a_statement_changed_after_review_without_writing(tmp_path):
    ledger_path, records_dir = _write_ledger(tmp_path)
    statement = _write_statement(tmp_path)
    plan = plan_credit_card_import(statement, ledger_path=ledger_path)
    statement.write_text(statement.read_text() + "2024-01-07,SHOP,9.00\n")

    with pytest.raises(ImportApplyError, match="changed after review"):
        apply_credit_card_import(
            statement,
            ledger_path=ledger_path,
            records_dir=records_dir,
            selected_account=plan.account,
            expected_source_sha256=plan.source_sha256,
            edits=(),
            importer_id=plan.importer_id,
        )

    assert list((records_dir / "2024").glob("cibc_*.beancount")) == []
