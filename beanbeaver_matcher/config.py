"""Resolve matcher configuration from Fava extension options.

No `beanbeaver-core`/beanbeaver dependency: merchant-family aliases are loaded straight from
a TOML file in the same format as beanbeaver's `merchant_families.toml`
(`[[families]] canonical = "..." aliases = [...]`), pointed to by an extension option.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from beanbeaver_matcher.model import MerchantFamily


def load_merchant_families(path: Path) -> tuple[MerchantFamily, ...]:
    """Load merchant alias families from a TOML file. Missing file -> no families."""
    if not path.exists():
        return ()
    with path.open("rb") as handle:
        data = tomllib.load(handle)

    families = []
    for family in data.get("families", []):
        canonical = str(family.get("canonical", "")).strip()
        if not canonical:
            continue
        aliases = tuple(
            alias.strip() for alias in family.get("aliases", []) if isinstance(alias, str) and alias.strip()
        )
        families.append(MerchantFamily(canonical=canonical, aliases=aliases))
    return tuple(families)


@dataclass(frozen=True)
class MatcherConfig:
    receipts_dir: Path
    merchant_families: tuple[MerchantFamily, ...] = ()


@dataclass(frozen=True)
class ImportConfig:
    ledger_path: Path
    imports_dir: Path
    records_dir: Path
    merchant_rules: Path | None = None


def resolve_config(options: dict[str, object]) -> MatcherConfig:
    """Build a MatcherConfig from the extension's `custom "fava-extension"` options dict."""
    receipts_value = options.get("receipts")
    if not receipts_value:
        raise ValueError('beanbeaver_matcher requires a "receipts" extension option (path to receipt chains)')
    receipts_dir = Path(str(receipts_value)).expanduser()

    families: tuple[MerchantFamily, ...] = ()
    families_value = options.get("merchant_families")
    if families_value:
        families = load_merchant_families(Path(str(families_value)).expanduser())

    return MatcherConfig(receipts_dir=receipts_dir, merchant_families=families)


def resolve_import_config(options: dict[str, object], *, fava_ledger_path: Path) -> ImportConfig:
    """Resolve statement-import paths, with useful defaults for direct extension use."""
    ledger_value = options.get("ledger")
    ledger_path = Path(str(ledger_value)).expanduser() if ledger_value else fava_ledger_path
    ledger_path = ledger_path.resolve()

    imports_value = options.get("imports")
    imports_dir = Path(str(imports_value)).expanduser() if imports_value else Path.home() / "Downloads"

    records_value = options.get("records")
    records_dir = Path(str(records_value)).expanduser() if records_value else ledger_path.parent / "records"

    rules_value = options.get("merchant_rules")
    merchant_rules = Path(str(rules_value)).expanduser() if rules_value else None
    return ImportConfig(
        ledger_path=ledger_path,
        imports_dir=imports_dir.resolve(),
        records_dir=records_dir.resolve(),
        merchant_rules=merchant_rules.resolve() if merchant_rules else None,
    )
