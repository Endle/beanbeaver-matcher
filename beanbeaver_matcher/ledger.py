"""Direct beancount ledger access.

Replaces beanbeaver's PyO3 `python_ledger_access.rs` — this reads the ledger straight
through `beancount.loader`, which is already the underlying implementation the Rust side
called into via PyO3.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from beancount import loader
from beancount.core import data

from beanbeaver_matcher.model import LedgerPosting, LedgerTransaction


@dataclass(frozen=True)
class LedgerSnapshot:
    path: str
    transactions: list[LedgerTransaction]
    errors: list[str]


def _format_errors(errors: list[object]) -> list[str]:
    formatted = []
    for error in errors:
        source = getattr(error, "source", None)
        message = getattr(error, "message", None) or str(error)
        if isinstance(source, dict):
            filename = source.get("filename")
            lineno = source.get("lineno")
            if filename and lineno:
                formatted.append(f"{filename}:{lineno} - {message}")
                continue
            if filename:
                formatted.append(f"{filename} - {message}")
                continue
        formatted.append(str(message))
    return formatted


def load_transactions(ledger_path: Path | str) -> LedgerSnapshot:
    """Load a beancount ledger and return its Transaction entries as LedgerTransactions."""
    entries, errors, _options = loader.load_file(str(ledger_path))

    transactions = []
    for entry in entries:
        if not isinstance(entry, data.Transaction):
            continue

        meta = entry.meta or {}
        file_path = str(meta.get("filename", "unknown"))
        line_number = int(meta.get("lineno", 0) or 0)

        postings = tuple(
            LedgerPosting(
                account=posting.account,
                number=posting.units.number if posting.units else None,
                currency=posting.units.currency if posting.units else None,
            )
            for posting in entry.postings
        )

        transactions.append(
            LedgerTransaction(
                date=entry.date,
                payee=entry.payee,
                narration=entry.narration,
                postings=postings,
                file_path=file_path,
                line_number=line_number,
            )
        )

    return LedgerSnapshot(path=str(ledger_path), transactions=transactions, errors=_format_errors(errors))
