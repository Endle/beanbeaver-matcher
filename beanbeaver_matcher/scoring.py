"""Receipt-to-ledger-transaction matching algorithm.

Ported from beanbeaver's `src/matcher.rs` (fixed-point i64 scoring, PyO3 boundary) plus the
strict -> relaxed -> amount/date-only fallback tiering from `src/match_service.rs`'s
`resolve_candidates`. This port uses `decimal.Decimal` directly since there is no PyO3
boundary to justify fixed-point scaling.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from beanbeaver_matcher.model import Candidate, LedgerTransaction, MerchantFamily, Receipt

_NOISE_SUFFIXES = {"INC", "LLC", "LTD", "CORP", "CO"}
_ASCII_ALPHA = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
_ASCII_UPPER = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_ASCII_DIGIT = set("0123456789")


@dataclass(frozen=True)
class MatchConfig:
    """Tolerances for one matching pass."""

    date_tolerance_days: int = 3
    amount_tolerance: Decimal = Decimal("0.10")
    amount_tolerance_percent: Decimal = Decimal("0.01")
    merchant_min_similarity: float = 0.30


def strict_config() -> MatchConfig:
    return MatchConfig()


def relaxed_config() -> MatchConfig:
    return MatchConfig(
        date_tolerance_days=7,
        amount_tolerance=Decimal("2.00"),
        amount_tolerance_percent=Decimal("0.08"),
        merchant_min_similarity=0.15,
    )


def _amount_tolerance(receipt_amount: Decimal, config: MatchConfig) -> Decimal:
    return max(config.amount_tolerance, receipt_amount * config.amount_tolerance_percent)


def _strip_noise_suffix(value: str) -> str | None:
    tokens = value.split()
    if len(tokens) < 2:
        return None
    last = tokens[-1].rstrip(".")
    is_noise = (
        last in _NOISE_SUFFIXES
        or (bool(last) and all(ch in _ASCII_DIGIT for ch in last))
        or (last.startswith("#") and len(last) > 1 and all(ch in _ASCII_DIGIT for ch in last[1:]))
    )
    return " ".join(tokens[:-1]) if is_noise else None


def _strip_state_suffix(value: str) -> str | None:
    trimmed = value.rstrip()
    if len(trimmed) < 2:
        return None
    suffix = trimmed[-2:]
    if not all(ch in _ASCII_UPPER for ch in suffix):
        return None
    prefix = trimmed[:-2]
    stripped = prefix.rstrip(", ")
    if len(stripped) == len(prefix):
        return None
    return stripped.rstrip()


def _strip_trailing_city_like(value: str) -> str | None:
    trimmed = value.rstrip()
    end = len(trimmed)
    token_end = end
    while end > 0 and trimmed[end - 1] in _ASCII_ALPHA:
        end -= 1
    if token_end == end:
        return None
    token = trimmed[end:token_end]
    if len(token) < 2:
        return None
    if end == 0:
        return None
    separator = trimmed[end - 1]
    if separator not in (" ", ","):
        return None
    stripped = trimmed[:end].rstrip(", ").rstrip()
    return stripped or None


def normalize_merchant(value: str) -> str:
    """Uppercase, strip a noise suffix (Inc/LLC/#123), a trailing state code, and a trailing
    city-like word, then keep only ASCII alphanumerics."""
    normalized = value.strip().upper()

    stripped = _strip_noise_suffix(normalized)
    if stripped is not None:
        normalized = stripped
    stripped = _strip_state_suffix(normalized)
    if stripped is not None:
        normalized = stripped
    stripped = _strip_trailing_city_like(normalized)
    if stripped is not None:
        normalized = stripped

    return re.sub(r"[^A-Z0-9]", "", normalized)


def _alpha_words(value: str) -> set[str]:
    return {word for word in re.split(r"[^A-Za-z]+", value.upper()) if len(word) >= 3}


@dataclass(frozen=True)
class _NormalizedFamily:
    canonical_label: str
    canonical_normalized: str
    aliases_normalized: tuple[str, ...]


def _build_families(raw_families: Sequence[MerchantFamily]) -> list[_NormalizedFamily]:
    families = []
    for family in raw_families:
        canonical_normalized = normalize_merchant(family.canonical)
        if not canonical_normalized:
            continue
        aliases_normalized = []
        for alias in (family.canonical, *family.aliases):
            normalized_alias = normalize_merchant(alias)
            if normalized_alias:
                aliases_normalized.append(normalized_alias)
        families.append(_NormalizedFamily(family.canonical, canonical_normalized, tuple(aliases_normalized)))
    return families


def _alias_matches(normalized_value: str, normalized_alias: str) -> bool:
    return (
        normalized_value == normalized_alias
        or normalized_alias in normalized_value
        or normalized_value in normalized_alias
    )


def _canonicalize_merchant(value: str, families: Sequence[_NormalizedFamily]) -> tuple[str, str | None]:
    normalized_value = normalize_merchant(value)
    if not normalized_value:
        return normalized_value, None
    for family in families:
        if any(_alias_matches(normalized_value, alias) for alias in family.aliases_normalized):
            return family.canonical_normalized, family.canonical_label
    return normalized_value, None


def _merchant_similarity(
    receipt_merchant: str, txn_payee: str, families: Sequence[_NormalizedFamily]
) -> tuple[float, str | None]:
    normalized_receipt, receipt_family = _canonicalize_merchant(receipt_merchant, families)
    normalized_txn, txn_family = _canonicalize_merchant(txn_payee, families)

    if not normalized_receipt or not normalized_txn:
        return 0.0, None

    if normalized_receipt == normalized_txn and (receipt_family or txn_family):
        return 1.0, receipt_family or txn_family

    if normalized_txn in normalized_receipt or normalized_receipt in normalized_txn:
        return 0.9, None

    common_prefix = 0
    for left, right in zip(normalized_receipt, normalized_txn):
        if left != right:
            break
        common_prefix += 1
    min_len = min(len(normalized_receipt), len(normalized_txn))
    if common_prefix >= 4 and min_len > 0:
        return 0.5 + 0.4 * (common_prefix / min_len), None

    receipt_words = _alpha_words(receipt_merchant)
    txn_words = _alpha_words(txn_payee)
    if receipt_words and txn_words:
        common_words = receipt_words & txn_words
        if common_words:
            union_count = len(receipt_words | txn_words)
            if union_count > 0:
                return 0.3 + 0.4 * (len(common_words) / union_count), None

    return 0.0, None


def merchant_similarity(
    receipt_merchant: str, txn_payee: str, merchant_families: Sequence[MerchantFamily] = ()
) -> float:
    """Similarity score (0.0-1.0) between a receipt merchant and a transaction payee."""
    families = _build_families(merchant_families)
    return _merchant_similarity(receipt_merchant, txn_payee, families)[0]


def _match_receipt_to_transaction(
    receipt: Receipt,
    txn: LedgerTransaction,
    config: MatchConfig,
    families: Sequence[_NormalizedFamily],
) -> tuple[float, str] | None:
    confidence = 0.0
    details: list[str] = []

    if receipt.date_is_placeholder:
        details.append("date: unknown")
    else:
        date_diff = abs((txn.date - receipt.date).days)
        if date_diff > config.date_tolerance_days:
            return None
        if date_diff == 0:
            confidence += 0.4
            details.append("date: exact match")
        else:
            confidence += 0.4 * (1.0 - date_diff / (config.date_tolerance_days + 1))
            details.append(f"date: {date_diff} day(s) off")

    txn_amount = txn.charge_amount
    if txn_amount is None:
        return None

    receipt_amount = receipt.match_amount
    amount_diff = abs(txn_amount - receipt_amount)
    amount_tolerance = _amount_tolerance(receipt_amount, config)
    if amount_diff > amount_tolerance:
        return None

    amount_label = ""
    card_amount = receipt.card_amount
    if card_amount is not None and receipt.non_card_amount > 0:
        amount_label = f" (after ${receipt.non_card_amount:.2f} non-card tender)"

    if amount_diff == 0:
        confidence += 0.4
        details.append(f"amount: exact match{amount_label}")
    else:
        confidence += 0.4 * (1.0 - float(amount_diff / amount_tolerance))
        details.append(f"amount: ${amount_diff:.2f} off{amount_label}")

    merchant_score, matched_family = _merchant_similarity(receipt.merchant, txn.payee or "", families)
    if merchant_score < config.merchant_min_similarity:
        return None

    confidence += 0.2 * merchant_score
    if matched_family:
        details.append(f"merchant: family match ({matched_family})")
    elif merchant_score > 0.8:
        details.append("merchant: good match")
    else:
        details.append(f"merchant: partial match ({merchant_score * 100:.0f}%)")

    return confidence, ", ".join(details)


def match_receipt_to_transactions(
    receipt: Receipt,
    transactions: Sequence[LedgerTransaction],
    config: MatchConfig | None = None,
    merchant_families: Sequence[MerchantFamily] = (),
    strength: Literal["strict", "relaxed", "fallback"] = "strict",
) -> list[Candidate]:
    """Score every transaction against the receipt; matches sorted by confidence desc, then index asc."""
    resolved_config = config or strict_config()
    families = _build_families(merchant_families)

    scored: list[tuple[int, float, str]] = []
    for index, txn in enumerate(transactions):
        result = _match_receipt_to_transaction(receipt, txn, resolved_config, families)
        if result is not None:
            confidence, details = result
            scored.append((index, confidence, details))

    scored.sort(key=lambda entry: (-entry[1], entry[0]))
    return [
        Candidate(transaction=transactions[index], confidence=confidence, details=details, strength=strength)
        for index, confidence, details in scored
    ]


def _relaxed_amount_tolerance(receipt_total: Decimal) -> Decimal:
    return max(Decimal("2.00"), receipt_total * Decimal("0.08"))


def _fallback_candidates(
    receipt: Receipt,
    transactions: Sequence[LedgerTransaction],
    merchant_families: Sequence[MerchantFamily],
) -> list[Candidate]:
    """Amount/date-only candidates for manual review when even the relaxed pass finds nothing."""
    families = _build_families(merchant_families)
    amount_tolerance = _relaxed_amount_tolerance(receipt.total)
    date_tolerance_days = 7

    scored: list[tuple[int, float, str]] = []
    for index, txn in enumerate(transactions):
        amount = txn.charge_amount
        if amount is None:
            continue
        amount_delta = abs(amount - receipt.total)
        if amount_delta > amount_tolerance:
            continue

        date_delta = abs((txn.date - receipt.date).days)
        if not receipt.date_is_placeholder and date_delta > date_tolerance_days:
            continue

        similarity = _merchant_similarity(receipt.merchant, txn.payee, families)[0] if txn.payee else 0.0

        amount_component = 1.0 if amount_delta == 0 else 1.0 - float(amount_delta / amount_tolerance)
        amount_component = max(0.0, min(1.0, amount_component))
        date_component = 0.5 if receipt.date_is_placeholder else 1.0 - date_delta / date_tolerance_days
        date_component = max(0.0, min(1.0, date_component))

        confidence = 0.45 * amount_component + 0.35 * date_component + 0.20 * similarity
        confidence = max(0.0, min(0.75, confidence))

        merchant_details = "merchant: no match" if similarity <= 0.0 else f"merchant: weak match ({similarity:.2f})"
        amount_details = "amount: exact match" if amount_delta == 0 else f"amount: ${amount_delta:.2f} off"
        details = f"date: {date_delta} day(s) off, {amount_details}, {merchant_details}"

        scored.append((index, confidence, details))

    scored.sort(key=lambda entry: (-entry[1], entry[0]))
    return [
        Candidate(transaction=transactions[index], confidence=confidence, details=details, strength="fallback")
        for index, confidence, details in scored
    ]


@dataclass(frozen=True)
class ResolvedCandidates:
    """Result of resolving one receipt against the ledger: candidates plus fallback provenance."""

    candidates: list[Candidate]
    used_relaxed_threshold: bool
    warning: str | None


def resolve_candidates(
    receipt: Receipt,
    transactions: Sequence[LedgerTransaction],
    merchant_families: Sequence[MerchantFamily] = (),
) -> ResolvedCandidates:
    """Strict match; fall back to relaxed, then amount/date-only, tiers for manual review."""
    strict = match_receipt_to_transactions(receipt, transactions, strict_config(), merchant_families, "strict")
    if strict:
        return ResolvedCandidates(candidates=strict, used_relaxed_threshold=False, warning=None)

    relaxed = match_receipt_to_transactions(receipt, transactions, relaxed_config(), merchant_families, "relaxed")
    if relaxed:
        return ResolvedCandidates(
            candidates=relaxed,
            used_relaxed_threshold=True,
            warning="No reliable matches found. Showing weaker candidates for manual review.",
        )

    fallback = _fallback_candidates(receipt, transactions, merchant_families)
    if fallback:
        return ResolvedCandidates(
            candidates=fallback,
            used_relaxed_threshold=True,
            warning=(
                "No reliable or relaxed merchant matches found. Showing amount/date-only candidates for manual review."
            ),
        )

    return ResolvedCandidates(
        candidates=[],
        used_relaxed_threshold=False,
        warning="No reliable matches found, and no weaker fallback candidates were found.",
    )
