"""Plan and transactionally apply one credit-card statement import."""

from __future__ import annotations

import fnmatch
import hashlib
import os
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

from beancount import loader
from beancount.core import data

from beanbeaver_matcher.credit_card.model import (
    ApplyImportResult,
    CardImporterId,
    CreditCardPlan,
    PlannedCardTransaction,
    TransactionEdit,
)
from beanbeaver_matcher.credit_card.parsers import parse_credit_card, route_credit_card
from beanbeaver_matcher.credit_card.rules import MerchantRules

_ISSUER_ALIASES: dict[CardImporterId, tuple[str, ...]] = {
    "cibc": ("CIBC",),
    "bmo": ("BMO",),
    "scotia": ("SCOTIA",),
    "rogers": ("ROGERS",),
    "mbna": ("MBNA",),
    "pcf": ("PCFINANCIAL", "PC"),
    "ctfs": ("CTFS",),
    "amex": ("AMEX", "AMERICANEXPRESS"),
}


class AccountSelectionRequired(RuntimeError):
    def __init__(self, options: tuple[str, ...], label: str) -> None:
        super().__init__(f"Select a {label} account")
        self.options = options
        self.label = label


class ImportApplyError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize(value: str) -> str:
    return "".join(character for character in value.upper() if character.isalnum())


def _load_ledger(ledger_path: Path):
    entries, errors, options = loader.load_file(str(ledger_path))
    return entries, [str(error) for error in errors], options


def _open_accounts(ledger_path: Path, *, as_of: date, pattern: str) -> list[str]:
    entries, errors, _ = _load_ledger(ledger_path)
    if errors:
        raise ImportApplyError(f"Ledger has errors: {errors[0]}")
    opened: dict[str, date] = {}
    closed: dict[str, date] = {}
    for entry in entries:
        if isinstance(entry, data.Open) and entry.date <= as_of:
            opened[entry.account] = entry.date
        elif isinstance(entry, data.Close) and entry.date <= as_of:
            closed[entry.account] = entry.date
    return sorted(
        account
        for account, opened_on in opened.items()
        if fnmatch.fnmatchcase(account, pattern) and (account not in closed or opened_on > closed[account])
    )


def _account_options(ledger_path: Path, importer_id: CardImporterId, source_path: Path, as_of: date) -> tuple[str, ...]:
    accounts = _open_accounts(ledger_path, as_of=as_of, pattern="Liabilities:CreditCard:*")
    aliases = _ISSUER_ALIASES[importer_id]
    matching = [account for account in accounts if any(alias in _normalize(account) for alias in aliases)]

    lower_name = source_path.name.lower()
    preferred_token = None
    if importer_id == "cibc" and "simplii" in lower_name:
        preferred_token = "SIMPLII"
    elif importer_id == "bmo" and lower_name == "porter.csv":
        preferred_token = "PORTER"
    elif importer_id == "amex":
        for filename_token, account_token in (
            ("marr", "MARRIOTT"),
            ("gold", "GOLD"),
            ("aeroplan", "AEROPLAN"),
            ("green", "GREEN"),
            ("plat", "PLAT"),
        ):
            if filename_token in lower_name:
                preferred_token = account_token
                break
    if preferred_token:
        preferred = [account for account in matching if preferred_token in _normalize(account)]
        if preferred:
            matching = preferred
    return tuple(matching)


def _existing_fingerprints(ledger_path: Path, account: str) -> set[tuple[date, str, Decimal]]:
    entries, errors, _ = _load_ledger(ledger_path)
    if errors:
        raise ImportApplyError(f"Ledger has errors: {errors[0]}")
    fingerprints = set()
    for entry in entries:
        if not isinstance(entry, data.Transaction):
            continue
        for posting in entry.postings:
            if posting.account == account and posting.units is not None:
                fingerprints.add((entry.date, entry.payee or "", -posting.units.number))
                break
    return fingerprints


def _imported_source_hashes(ledger_path: Path) -> set[str]:
    entries, _, _ = _load_ledger(ledger_path)
    hashes = set()
    for entry in entries:
        if isinstance(entry, data.Transaction) and entry.meta:
            value = entry.meta.get("bb_source")
            if isinstance(value, str):
                hashes.add(value)
    return hashes


def plan_credit_card_import(
    source_path: Path,
    *,
    ledger_path: Path,
    selected_account: str | None = None,
    importer_id: CardImporterId | None = None,
    merchant_rules_path: Path | None = None,
) -> CreditCardPlan:
    source_path = source_path.resolve()
    routed_importer = route_credit_card(source_path)
    if importer_id is not None and importer_id != routed_importer:
        raise ImportApplyError(
            f"Statement now routes to {routed_importer}, not the reviewed {importer_id} importer"
        )
    resolved_importer = importer_id or routed_importer
    rows = parse_credit_card(source_path, resolved_importer)
    as_of = max(row.date for row in rows)
    account_options = _account_options(ledger_path, resolved_importer, source_path, as_of)
    if not account_options:
        raise ImportApplyError(f"No open {resolved_importer} credit-card accounts found as of {as_of.isoformat()}")
    if selected_account is None and len(account_options) != 1:
        raise AccountSelectionRequired(account_options, resolved_importer.upper())
    account = selected_account or account_options[0]
    if account not in account_options:
        raise ImportApplyError(f"Selected account is not available for this statement: {account}")

    rules = MerchantRules(merchant_rules_path)
    existing = _existing_fingerprints(ledger_path, account)
    categories = _open_accounts(ledger_path, as_of=as_of, pattern="Expenses:*")
    if not categories:
        raise ImportApplyError(f"No open expense accounts found as of {as_of.isoformat()}")
    fallback_category = "Expenses:Uncategorized" if "Expenses:Uncategorized" in categories else categories[0]

    def category_for(payee: str, amount: Decimal) -> str:
        category = rules.categorize(payee, amount)
        return category if category in categories else fallback_category

    transactions = tuple(
        PlannedCardTransaction(
            row_id=row.row_id,
            date=row.date,
            payee=row.payee,
            amount=row.amount,
            currency=row.currency,
            category=category_for(row.payee, row.amount),
            duplicate=(row.date, row.payee, row.amount) in existing,
        )
        for row in rows
    )
    return CreditCardPlan(
        source_path=source_path,
        source_sha256=_sha256(source_path),
        importer_id=resolved_importer,
        account=account,
        transactions=transactions,
        candidate_categories=tuple(categories),
    )


def _quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _amount(value: Decimal) -> str:
    return format(value, "f")


def _edited_transactions(
    plan: CreditCardPlan, edits: tuple[TransactionEdit, ...]
) -> list[tuple[PlannedCardTransaction, Decimal, str]]:
    plan_ids = {transaction.row_id for transaction in plan.transactions}
    edit_ids = [edit.row_id for edit in edits]
    if len(edit_ids) != len(set(edit_ids)):
        raise ImportApplyError("Each transaction row may only be edited once")
    unknown_ids = sorted(set(edit_ids) - plan_ids)
    if unknown_ids:
        raise ImportApplyError(f"Unknown transaction row: {unknown_ids[0]}")
    by_id = {edit.row_id: edit for edit in edits}
    rendered = []
    for transaction in plan.transactions:
        edit = by_id.get(transaction.row_id)
        if edit and edit.deleted:
            continue
        if transaction.duplicate and edit is None:
            raise ImportApplyError(
                f"Row {transaction.row_id} duplicates an existing ledger transaction; explicitly delete or edit it"
            )
        amount = edit.new_amount if edit and edit.new_amount is not None else transaction.amount
        if not amount.is_finite():
            raise ImportApplyError(f"Invalid amount for row {transaction.row_id}: {amount}")
        category = edit.category if edit else transaction.category
        if not category.startswith("Expenses:"):
            raise ImportApplyError(f"Invalid expense category for row {transaction.row_id}: {category}")
        rendered.append((transaction, amount, category))
    if not rendered:
        raise ImportApplyError("No transactions remain to import")
    return rendered


def render_credit_card_plan(plan: CreditCardPlan, edits: tuple[TransactionEdit, ...]) -> tuple[str, int]:
    transactions = _edited_transactions(plan, edits)
    lines = [
        ";; -*- mode: beancount -*-",
        f";; Credit-card import from {plan.source_path.name}",
        f";; importer: {plan.importer_id}",
        f";; source-sha256: {plan.source_sha256}",
        "",
    ]
    for transaction, amount, category in transactions:
        lines.extend(
            [
                f'{transaction.date.isoformat()} * "{_quote(transaction.payee)}" ""',
                f'  bb_source: "{plan.source_sha256}"',
                f'  bb_importer: "{plan.importer_id}"',
                f'  bb_row: "{transaction.row_id}"',
                f"  {plan.account}  {_amount(-amount)} {transaction.currency}",
                f"  {category}  {_amount(amount)} {transaction.currency}",
                "",
            ]
        )
    return "\n".join(lines), len(transactions)


def _result_filename(plan: CreditCardPlan) -> str:
    account_name = plan.account.removeprefix("Liabilities:CreditCard:").replace(":", "_").lower()
    return f"{account_name}_{plan.start_date:%m%d}_{plan.end_date:%m%d}.beancount"


def _validate_with_candidate(ledger_path: Path, candidate_path: Path) -> list[str]:
    with tempfile.NamedTemporaryFile("w", suffix=".beancount", delete=False, dir=ledger_path.parent) as handle:
        wrapper_path = Path(handle.name)
        handle.write(f'include "{ledger_path.as_posix()}"\ninclude "{candidate_path.as_posix()}"\n')
    try:
        _, errors, _ = loader.load_file(str(wrapper_path))
        return [str(error) for error in errors]
    finally:
        wrapper_path.unlink(missing_ok=True)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=path.parent, encoding="utf-8") as handle:
        temporary = Path(handle.name)
        handle.write(content)
    os.replace(temporary, path)


def apply_credit_card_import(
    source_path: Path,
    *,
    ledger_path: Path,
    records_dir: Path,
    selected_account: str,
    expected_source_sha256: str,
    edits: tuple[TransactionEdit, ...],
    importer_id: CardImporterId | None = None,
    merchant_rules_path: Path | None = None,
) -> ApplyImportResult:
    if _sha256(source_path) != expected_source_sha256:
        raise ImportApplyError("Statement changed after review; refresh the import plan")
    plan = plan_credit_card_import(
        source_path,
        ledger_path=ledger_path,
        selected_account=selected_account,
        importer_id=importer_id,
        merchant_rules_path=merchant_rules_path,
    )
    if plan.source_sha256 in _imported_source_hashes(ledger_path):
        year_dir = records_dir / str(plan.end_date.year)
        output_path = year_dir / _result_filename(plan)
        return ApplyImportResult("already_applied", output_path, 0, "This exact statement was already imported")
    if plan.start_date.year != plan.end_date.year:
        raise ImportApplyError("Statements spanning calendar years are not supported yet")

    output, count = render_credit_card_plan(plan, edits)
    year_dir = records_dir / str(plan.end_date.year)
    output_path = year_dir / _result_filename(plan)
    summary_path = year_dir / f"{plan.end_date.year}.beancount"
    if output_path.exists():
        raise ImportApplyError(f"Output file already exists: {output_path}")

    year_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".beancount", delete=False, dir=year_dir) as handle:
        candidate_path = Path(handle.name)
        handle.write(output)
    try:
        errors = _validate_with_candidate(ledger_path, candidate_path)
        if errors:
            raise ImportApplyError(f"Proposed import does not validate: {errors[0]}")
    finally:
        candidate_path.unlink(missing_ok=True)

    summary_original = summary_path.read_text() if summary_path.exists() else None
    include = f'include "{output_path.name}"'
    summary_content = summary_original or ""
    if include not in summary_content:
        if summary_content and not summary_content.endswith("\n"):
            summary_content += "\n"
        summary_content += include + "\n"

    try:
        _atomic_write(output_path, output)
        _atomic_write(summary_path, summary_content)
        _, final_errors, _ = _load_ledger(ledger_path)
        if final_errors:
            raise ImportApplyError(f"Ledger validation failed after import: {final_errors[0]}")
        if plan.source_sha256 not in _imported_source_hashes(ledger_path):
            raise ImportApplyError(f"Imported statement is not reachable from {ledger_path}")
    except Exception:
        output_path.unlink(missing_ok=True)
        if summary_original is None:
            summary_path.unlink(missing_ok=True)
        else:
            _atomic_write(summary_path, summary_original)
        raise

    return ApplyImportResult("applied", output_path, count, f"Imported {count} transaction(s) into {output_path}")
