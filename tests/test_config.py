import pytest

from beanbeaver_matcher.config import resolve_config


def test_resolve_config_requires_receipts_option():
    with pytest.raises(ValueError, match="receipts"):
        resolve_config({})


def test_resolve_config_reads_receipts_dir(tmp_path):
    config = resolve_config({"receipts": str(tmp_path / "receipts")})
    assert config.receipts_dir == tmp_path / "receipts"
    assert config.merchant_families == ()


def test_resolve_config_reads_optional_legacy_receipts_dir(tmp_path):
    config = resolve_config(
        {
            "receipts": str(tmp_path / "receipts"),
            "legacy_receipts": str(tmp_path / "beanbeaver_receipts"),
        }
    )

    assert config.legacy_receipts_dir == tmp_path / "beanbeaver_receipts"


def test_resolve_config_loads_merchant_families_toml(tmp_path):
    toml_path = tmp_path / "merchant_families.toml"
    toml_path.write_text(
        """
        [[families]]
        canonical = "REAL CANADIAN SUPERSTORE"
        aliases = ["REAL CANADIAN", "RCSS"]
        """
    )

    config = resolve_config({"receipts": str(tmp_path), "merchant_families": str(toml_path)})

    assert len(config.merchant_families) == 1
    assert config.merchant_families[0].canonical == "REAL CANADIAN SUPERSTORE"
    assert config.merchant_families[0].aliases == ("REAL CANADIAN", "RCSS")
