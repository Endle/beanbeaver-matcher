from decimal import Decimal

from beanbeaver_matcher.ledger import load_transactions

LEDGER = """
2024-01-01 open Liabilities:CreditCard:CardA
2024-01-01 open Expenses:Uncategorized

2024-01-05 * "COSTCO" "Groceries"
  Liabilities:CreditCard:CardA  -54.20 CAD
  Expenses:Uncategorized         54.20 CAD
"""


def test_load_transactions_reads_postings_and_location(tmp_path):
    ledger_path = tmp_path / "main.beancount"
    ledger_path.write_text(LEDGER)

    snapshot = load_transactions(ledger_path)

    assert snapshot.errors == []
    assert len(snapshot.transactions) == 1
    txn = snapshot.transactions[0]
    assert txn.payee == "COSTCO"
    assert txn.narration == "Groceries"
    assert txn.file_path == str(ledger_path)
    assert txn.line_number == 5
    assert txn.charge_amount == Decimal("54.20")
    assert txn.charge_account == "Liabilities:CreditCard:CardA"


def test_load_transactions_reports_errors_for_broken_ledger(tmp_path):
    ledger_path = tmp_path / "broken.beancount"
    ledger_path.write_text('2024-01-01 * "X" "Y"\n  Assets:Nonexistent  10.00 CAD\n')

    snapshot = load_transactions(ledger_path)

    assert len(snapshot.errors) >= 1
