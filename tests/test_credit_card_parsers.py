from decimal import Decimal

import pytest

from beanbeaver_matcher.credit_card.parsers import CardParseError, parse_credit_card, route_credit_card


@pytest.mark.parametrize(
    ("filename", "content", "importer_id", "payee"),
    [
        ("CIBC.csv", "Date,Description,Debit\n2024-01-05,CIBC SHOP,12.34\n", "cibc", "CIBC SHOP"),
        (
            "statement.csv",
            "Account summary\nGenerated today\nTRANSACTION DATE,TRANSACTION AMOUNT,DESCRIPTION\n"
            "20240105,12.34,BMO SHOP\n",
            "bmo",
            "BMO SHOP",
        ),
        (
            "Card Scotiabank.csv",
            "Statement export\nignored,2024-01-05,SCOTIA SHOP,ignored,ignored,Debit,12.34\n",
            "scotia",
            "SCOTIA SHOP",
        ),
        (
            "Transactions (2).csv",
            "DATE,MERCHANT NAME,AMOUNT\n2024-01-05,ROGERS SHOP,$12.34\n",
            "rogers",
            "ROGERS SHOP",
        ),
        (
            "MBNA.csv",
            "POSTED DATE,PAYEE,ADDRESS,AMOUNT\n01/05/2024,MBNA SHOP,Toronto,-12.34\n",
            "mbna",
            "MBNA SHOP",
        ),
        (
            "report.csv",
            "Merchant,Type,Ignored,Date,Ignored,Amount\nPCF SHOP,PURCHASE,x,01/05/2024,x,-12.34\n",
            "pcf",
            "PCF SHOP",
        ),
        (
            "Transactions.csv",
            "Account\nPeriod\nCurrency\nTRANSACTION DATE,AMOUNT,DESCRIPTION,TYPE\n"
            "2024-01-05,12.34,CTFS SHOP,PURCHASE\n",
            "ctfs",
            "CTFS SHOP",
        ),
        (
            "activity.csv",
            "DATE,DESCRIPTION,AMOUNT\n05 Jan 2024,AMEX SHOP,12.34\n",
            "amex",
            "AMEX SHOP",
        ),
    ],
)
def test_routes_and_parses_retiring_importer_formats(tmp_path, filename, content, importer_id, payee):
    path = tmp_path / filename
    path.write_text(content)

    assert route_credit_card(path) == importer_id
    rows = parse_credit_card(path, importer_id)
    assert [(row.payee, row.amount) for row in rows] == [(payee, Decimal("12.34"))]


def test_shared_payment_and_amex_offer_filters_are_preserved(tmp_path):
    path = tmp_path / "activity.csv"
    path.write_text(
        "Date,Description,Amount\n"
        "05 Jan 2024,PAYMENT RECEIVED,-100.00\n"
        "06 Jan 2024,PRESTO FARE OFFER,-2.00\n"
        "07 Jan 2024,CAFE,4.00\n"
    )

    rows = parse_credit_card(path, "amex")
    assert [(row.payee, row.amount) for row in rows] == [("CAFE", Decimal("4.00"))]


def test_ambiguous_transactions_filename_requires_a_known_header(tmp_path):
    path = tmp_path / "Transactions.csv"
    path.write_text("Date,Description,Amount\n2024-01-05,Unknown,1.00\n")

    with pytest.raises(CardParseError, match="does not match"):
        route_credit_card(path)


@pytest.mark.parametrize("date_column", ["effective_date", "transaction_date"])
def test_routes_and_parses_wealthsimple_chequing_exports(tmp_path, date_column):
    path = tmp_path / "activities-export-2026-09-04.csv"
    path.write_text(
        f"{date_column},effective_time,account_type,activity_type,description,currency,net_cash_amount\n"
        "2026-08-31,04:00:00,Chequing,MoneyMovement,Online bill payment (executed at 2026-08-31),CAD,-1266.08\n"
        "2026-09-03,17:00:15,Chequing,MoneyMovement,Direct deposit received,CAD,2524.06\n"
        "2026-09-03,17:00:15,Cash,MoneyMovement,Ignored investment activity,CAD,100.00\n"
    )

    assert route_credit_card(path) == "wealthsimple_chequing"
    rows = parse_credit_card(path, "wealthsimple_chequing")
    assert [(row.payee, row.amount) for row in rows] == [
        ("Online bill payment", Decimal("1266.08")),
        ("Direct deposit received", Decimal("-2524.06")),
    ]
