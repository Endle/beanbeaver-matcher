"""Render the enriched, itemized beancount entry for a matched receipt.

Ported from beanbeaver-core's `receipt_formatter::format_enriched_transaction`. Uses
`decimal.Decimal` directly instead of the Rust port's fixed-point cents-as-strings, since
there's no PyO3 boundary to justify it here. Matcher-exclusive text templating — no drift
risk against beanbeaver's other renderers (`format_parsed_receipt`/`format_draft_beancount`
stay in beanbeaver; they render earlier pipeline stages this tool never sees).
"""

from __future__ import annotations

from decimal import Decimal

from beanbeaver_matcher.model import Candidate, Receipt

_TWO_PLACES = Decimal("0.01")
_DEFAULT_EXPENSE = "Expenses:FIXME"


def _pending_account_for_kind(kind: str) -> str:
    return {
        "gift_card": "Assets:GiftCards:PENDING",
        "cash": "Assets:Cash:PENDING",
        "store_credit": "Assets:StoreCredit:PENDING",
    }.get(kind, "Liabilities:CreditCard:PENDING")


def _fixed(value: Decimal) -> str:
    return f"{value.quantize(_TWO_PLACES):.2f}"


def _amount(value: Decimal) -> str:
    return f"{_fixed(value)} CAD"


def _extract_card_last4(raw_text: str) -> str | None:
    """Pull a card's last-4 digits from a `**** 1234`-style OCR line."""
    for line in raw_text.splitlines():
        if "*" not in line:
            continue
        star_run = 0
        idx = 0
        while idx < len(line):
            if line[idx] == "*":
                star_run += 1
                idx += 1
                continue
            if star_run >= 2:
                while idx < len(line) and line[idx].isspace():
                    idx += 1
                if idx + 4 <= len(line):
                    candidate = line[idx : idx + 4]
                    if candidate.isdigit():
                        boundary_ok = idx + 4 == len(line) or not line[idx + 4].isdigit()
                        if boundary_ok:
                            return candidate
            star_run = 0
            idx += 1
    return None


def _quote_escape(value: str) -> str:
    return value.replace('"', "'")


def _format_postings_aligned(postings: list[tuple[str, str, str | None]], indent: str) -> list[str]:
    if not postings:
        return []
    account_width = max(len(account) for account, _, _ in postings)
    amount_width = max(len(amount) for _, amount, _ in postings)
    lines = []
    for account, amount, comment in postings:
        base = f"{indent}{account:<{account_width}}  {amount:>{amount_width}}"
        lines.append(f"{base}  ; {comment}" if comment else base)
    return lines


def format_enriched_transaction(
    receipt: Receipt,
    candidate: Candidate,
    *,
    default_expense: str = _DEFAULT_EXPENSE,
) -> str:
    """Render the enriched itemized transaction that will replace the matched charge."""
    txn = candidate.transaction
    lines = [
        "; === ENRICHED TRANSACTION - REVIEW NEEDED ===",
        f"; Receipt: {receipt.image_filename}",
        f"; Matched: {txn.file_path}:{txn.line_number}",
        f"; Confidence: {candidate.confidence * 100:.0f}% ({candidate.details})",
        "",
    ]

    payee = _quote_escape(txn.payee or "")
    narration = _quote_escape(txn.narration or "")
    lines.append(f'{txn.date.isoformat()} * "{payee}" "{narration}"')

    charge = txn.charge_posting
    cc_account = charge.account if charge else None
    cc_amount = charge.number if charge else None
    original_expense: str | None = None
    for posting in txn.postings:
        if posting.number is None:
            continue
        if posting.number > 0 and posting.account.startswith("Expenses:"):
            original_expense = posting.account

    expense_base = original_expense or default_expense
    last4 = _extract_card_last4(receipt.raw_text)
    card_comment = f"card ****{last4}" if last4 else None

    postings: list[tuple[str, str, str | None]] = []
    if receipt.tenders:
        # Multi-tender: the card tender's PENDING placeholder becomes the matched CC
        # account; non-card tenders render as additional postings (PENDING fallback
        # until the user assigns a real asset account in review).
        resolved_card_account = cc_account or "Liabilities:CreditCard:FIXME"
        card_used = False
        for tender in receipt.tenders:
            if tender.kind == "card" and not card_used:
                card_used = True
                account = resolved_card_account
                comment = card_comment
            else:
                account = tender.account or _pending_account_for_kind(tender.kind)
                comment = tender.kind.replace("_", " ")
            postings.append((account, _amount(-tender.amount), comment))
    elif cc_account is not None and cc_amount is not None:
        postings.append((cc_account, _amount(cc_amount), None))
    else:
        postings.append(("Liabilities:CreditCard:FIXME", _amount(-receipt.total), None))

    items_total = Decimal("0")
    for item in receipt.items:
        desc_clean = _quote_escape(item.description)
        comment = f"{desc_clean} (qty {item.quantity})" if item.quantity > 1 else desc_clean
        postings.append((item.category or default_expense, _amount(item.price), comment))
        items_total += item.price

    if receipt.tax:
        postings.append(("Expenses:Tax:HST", _amount(receipt.tax), None))
        items_total += receipt.tax

    # Multi-tender: items+tax should equal the full receipt total (the matcher's amount
    # comparison already handles the card-vs-total reconciliation).
    if receipt.tenders:
        expected_total = receipt.total
    elif cc_amount is not None:
        expected_total = abs(cc_amount)
    else:
        expected_total = receipt.total

    if expected_total > 0 and items_total != expected_total:
        diff = expected_total - items_total
        if diff > Decimal("0.01"):
            postings.append((expense_base, _amount(diff), "remaining/unitemized"))
        elif diff < Decimal("-0.01"):
            lines.append(
                f"  ; WARNING: items total ({_fixed(items_total)}) exceeds transaction ({_fixed(expected_total)})"
            )

    lines.extend(_format_postings_aligned(postings, "  "))
    lines.append("")
    lines.append("; --- Original Transaction (to be replaced) ---")
    lines.append(f'; {txn.date.isoformat()} * "{payee}" "{narration}"')
    for posting in txn.postings:
        if posting.number is not None and posting.currency is not None:
            lines.append(f";   {posting.account}  {posting.number} {posting.currency}")

    return "\n".join(lines)
