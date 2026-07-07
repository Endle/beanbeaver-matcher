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
