"""Apply one selected match: replace the ledger charge with an include, write the enriched
itemized entry, and archive the matched receipt.

Ported from beanbeaver's `python_ledger_access.rs` (`replace_transaction_with_include_impl`,
`ledger_access_apply_receipt_match`'s snapshot/rollback) and `match_service.rs`
(`apply_receipt_match_service`'s orchestration and itemized-total guard), plus
`receipt_storage.move_to_matched`'s stage-cloning for archiving.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from beanbeaver_matcher import receipts
from beanbeaver_matcher.enrich import format_enriched_transaction
from beanbeaver_matcher.ledger import load_transactions
from beanbeaver_matcher.model import Candidate

_STAGES_DIRNAME = "stages"
_ENRICHED_DIRNAME = "_enriched"


def _is_transaction_start(line: str) -> bool:
    """Detect a beancount transaction's opening line: `YYYY-MM-DD <flag> ...`."""
    stripped = line.lstrip()
    if len(stripped) < 11:
        return False
    if not (
        stripped[0:4].isdigit()
        and stripped[4] == "-"
        and stripped[5:7].isdigit()
        and stripped[7] == "-"
        and stripped[8:10].isdigit()
        and stripped[10].isspace()
    ):
        return False
    if len(stripped) < 12:
        return False
    flag = stripped[11]
    if not (flag in "*!?" or flag.isalpha()):
        return False
    return not (len(stripped) > 12 and not stripped[12].isspace())


def _find_transaction_end(lines: list[str], start_idx: int) -> int:
    """Exclusive end index of the transaction block starting at `start_idx`."""
    idx = start_idx + 1
    while idx < len(lines):
        line = lines[idx]
        if line.strip() == "":
            idx += 1
            break
        if line.startswith((" ", "\t")):
            idx += 1
            continue
        break
    return idx


def _comment_block(lines: list[str]) -> list[str]:
    out = []
    for line in lines:
        if line.strip() == "":
            out.append(line)
        elif line.lstrip().startswith(";"):
            out.append(line)
        else:
            out.append(f"; {line}")
    return out


def _parse_include_path(line: str) -> str | None:
    stripped = line.lstrip()
    if stripped.startswith(";") or not stripped.startswith('include "'):
        return None
    rest = stripped[len('include "') :]
    quote_end = rest.find('"')
    if quote_end == -1:
        return None
    include_path = rest[:quote_end]
    suffix = rest[quote_end + 1 :].lstrip()
    if suffix == "" or suffix.startswith(";"):
        return include_path
    return None


def _replace_transaction_with_include(
    statement_path: Path, line_number: int, include_rel_path: str, receipt_name: str
) -> str:
    """Comment out the matched transaction block and replace it with an `include`.
    Returns "already_applied" if the include is already present, else "applied"."""
    text = statement_path.read_text()
    lines = text.splitlines(keepends=True)
    include_prefix = f'include "{include_rel_path}"'

    for line in lines:
        if _parse_include_path(line) == include_rel_path:
            return "already_applied"

    if line_number == 0:
        raise ValueError(f"Invalid line number {line_number} for {statement_path}")

    start_idx = line_number - 1
    if start_idx >= len(lines):
        raise ValueError(f"Invalid line number {line_number} for {statement_path}")
    start_line = lines[start_idx]
    if not _is_transaction_start(start_line):
        raise ValueError(f"Line {line_number} in {statement_path} is not a transaction start: {start_line.strip()}")

    end_idx = _find_transaction_end(lines, start_idx)
    original_block = lines[start_idx:end_idx]
    if not original_block:
        raise ValueError(f"Empty transaction block at {statement_path}:{line_number}")

    stamp = date.today().isoformat()
    replacement = [f"; bb-match replaced from receipt {receipt_name} on {stamp}\n"]
    replacement.extend(_comment_block(original_block))
    if replacement[-1].strip():
        replacement.append("\n")
    replacement.append(f"{include_prefix}  ; bb-match: {receipt_name}\n")
    replacement.append("\n")

    new_lines = lines[:start_idx] + replacement + lines[end_idx:]
    statement_path.write_text("".join(new_lines))
    return "applied"


def move_to_matched(stage_path: Path) -> Path:
    """Write a new "matched" stage for one receipt chain; returns the new stage file path."""
    document = receipts.load_stage_document(stage_path)
    if receipts.stage_status(document) == "matched":
        return stage_path

    matched_document = copy.deepcopy(document)
    meta = matched_document.setdefault("meta", {})
    current_index = int(meta.get("stage_index", 0) or 0)
    meta["stage"] = "matched"
    meta["stage_index"] = current_index + 1
    meta["created_at"] = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    meta["created_by"] = "beanbeaver_matcher"
    meta["pass_name"] = "apply_match"
    meta["parent_file"] = stage_path.name

    stages_dir = receipts.receipt_chain_dir(stage_path) / _STAGES_DIRNAME
    stages_dir.mkdir(parents=True, exist_ok=True)
    matched_path = stages_dir / "900_matched.receipt.json"
    matched_path.write_text(json.dumps(matched_document, indent=2) + "\n")
    return matched_path


@dataclass(frozen=True)
class ApplyResult:
    status: str
    ledger_path: Path
    matched_receipt_path: Path | None = None
    enriched_path: Path | None = None
    message: str | None = None


def apply_match(
    stage_path: Path,
    candidate: Candidate,
    *,
    ledger_path: Path,
    default_expense: str = "Expenses:FIXME",
) -> ApplyResult:
    """Apply one selected candidate match end to end (see module docstring)."""
    receipt = receipts.read_receipt(stage_path)
    txn = candidate.transaction

    matched_file = Path(txn.file_path)
    if txn.file_path == "unknown" or not matched_file.exists():
        return ApplyResult(
            status="target_missing",
            ledger_path=ledger_path,
            message=f"Match target file missing: {txn.file_path}",
        )

    expected_total = txn.charge_amount
    if expected_total is not None:
        delta = expected_total - receipt.itemized_total
        if delta < Decimal("-0.01"):
            return ApplyResult(
                status="receipt_total_exceeds_transaction",
                ledger_path=ledger_path,
                message=(
                    f"Itemized receipt total (${receipt.itemized_total:.2f}) exceeds card transaction "
                    f"(${expected_total:.2f}) by ${abs(delta):.2f}. Re-edit the receipt first."
                ),
            )

    receipt_name = receipts.receipt_chain_name(stage_path)
    enriched_text = format_enriched_transaction(receipt, candidate, default_expense=default_expense)
    enriched_dir = matched_file.parent / _ENRICHED_DIRNAME
    enriched_path = enriched_dir / f"{receipt_name}.beancount"
    include_rel = enriched_path.relative_to(matched_file.parent).as_posix()

    original_statement = matched_file.read_text()
    original_enriched = enriched_path.read_text() if enriched_path.exists() else None

    try:
        status = _replace_transaction_with_include(matched_file, txn.line_number, include_rel, receipt_name)
        if status != "already_applied":
            enriched_dir.mkdir(parents=True, exist_ok=True)
            enriched_path.write_text(enriched_text)

            snapshot = load_transactions(ledger_path)
            if snapshot.errors:
                preview = "; ".join(snapshot.errors[:2])
                raise RuntimeError(f"ledger validation failed after replacement: {preview}")
    except Exception as exc:  # noqa: BLE001 - roll back and report, whatever failed
        matched_file.write_text(original_statement)
        if original_enriched is not None:
            enriched_dir.mkdir(parents=True, exist_ok=True)
            enriched_path.write_text(original_enriched)
        elif enriched_path.exists():
            enriched_path.unlink()
        return ApplyResult(status="apply_failed", ledger_path=ledger_path, message=str(exc))

    matched_receipt_path = move_to_matched(stage_path)
    action_msg = "already applied; receipt archived" if status == "already_applied" else "applied"
    return ApplyResult(
        status=status,
        ledger_path=ledger_path,
        matched_receipt_path=matched_receipt_path,
        enriched_path=enriched_path,
        message=f"Transaction {action_msg}. Enriched file: {enriched_path}",
    )
