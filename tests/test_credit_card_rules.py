from decimal import Decimal

import pytest

from beanbeaver_matcher.credit_card.rules import MerchantRules


@pytest.mark.parametrize(
    "payee",
    [
        "PETRO CANADA 12345 TORONTO ON",
        "PETRO-CANADA 12345 TORONTO ON",
    ],
)
def test_default_rules_categorize_petro_canada_as_gas(payee: str) -> None:
    assert MerchantRules().categorize(payee, Decimal("60.00")) == "Expenses:Driving:Gas"
