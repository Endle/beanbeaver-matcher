"""Fava extension: list approved receipts, rank ledger-transaction candidates, and apply
the chosen match. The one UI for beanbeaver-matcher (see README) — no CLI, no TUI.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import cast

from fava.ext import FavaExtensionBase, extension_endpoint
from fava.helpers import FavaAPIError
from flask import jsonify, request

from beanbeaver_matcher.apply import apply_match
from beanbeaver_matcher.config import ImportConfig, MatcherConfig, resolve_config, resolve_import_config
from beanbeaver_matcher.credit_card import (
    AccountSelectionRequired,
    ImportApplyError,
    TransactionEdit,
    apply_credit_card_import,
    plan_credit_card_import,
)
from beanbeaver_matcher.credit_card.model import CardImporterId
from beanbeaver_matcher.credit_card.parsers import CardParseError, route_credit_card
from beanbeaver_matcher.ledger import load_transactions
from beanbeaver_matcher.receipts import list_approved_receipts, read_receipt
from beanbeaver_matcher.scoring import resolve_candidates


class MatcherExtension(FavaExtensionBase):
    """List approved receipts and match them against ledger transactions."""

    report_title = "Matcher"
    has_js_module = True

    def _matcher_config(self) -> MatcherConfig:
        if not isinstance(self.config, dict):
            raise FavaAPIError(
                "beanbeaver_matcher: extension config must be a dict, e.g. "
                "'custom \"fava-extension\" \"beanbeaver_matcher.fava_ext\" \"{'receipts': '/path'}\"'"
            )
        return resolve_config(self.config)

    def _ledger_path(self) -> Path:
        return Path(self.ledger.beancount_file_path)

    def receipts(self) -> list[dict[str, object]]:
        """Approved receipts awaiting a match, for the report template."""
        config = self._matcher_config()
        return [
            {
                "stage_path": str(approved.stage_path),
                "merchant": approved.receipt.merchant,
                "date": approved.receipt.date.isoformat(),
                "date_is_placeholder": approved.receipt.date_is_placeholder,
                "total": str(approved.receipt.total),
            }
            for approved in list_approved_receipts(config.receipts_dir)
        ]

    @extension_endpoint("candidates", methods=["GET"])  # type: ignore[arg-type]
    def candidates_endpoint(self):  # noqa: ANN201 - Flask response
        stage_path = Path(request.args.get("stage_path", ""))
        if not stage_path.is_file():
            return jsonify({"error": f"Receipt not found: {stage_path}"}), 404

        config = self._matcher_config()
        receipt = read_receipt(stage_path)
        snapshot = load_transactions(self._ledger_path())
        resolved = resolve_candidates(receipt, snapshot.transactions, config.merchant_families)

        return jsonify(
            {
                "ledger_errors": snapshot.errors,
                "warning": resolved.warning,
                "used_relaxed_threshold": resolved.used_relaxed_threshold,
                "candidates": [
                    {
                        "file_path": candidate.transaction.file_path,
                        "line_number": candidate.transaction.line_number,
                        "date": candidate.transaction.date.isoformat(),
                        "payee": candidate.transaction.payee,
                        "narration": candidate.transaction.narration,
                        "amount": (
                            str(candidate.transaction.charge_amount)
                            if candidate.transaction.charge_amount is not None
                            else None
                        ),
                        "confidence": candidate.confidence,
                        "details": candidate.details,
                        "strength": candidate.strength,
                    }
                    for candidate in resolved.candidates
                ],
            }
        )

    @extension_endpoint("apply", methods=["POST"])  # type: ignore[arg-type]
    def apply_endpoint(self):  # noqa: ANN201 - Flask response
        payload = request.get_json(force=True, silent=True) or {}
        stage_path = Path(str(payload.get("stage_path", "")))
        file_path = str(payload.get("file_path", ""))
        try:
            line_number = int(payload.get("line_number", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid line_number"}), 400

        if not stage_path.is_file():
            return jsonify({"error": f"Receipt not found: {stage_path}"}), 404

        config = self._matcher_config()
        receipt = read_receipt(stage_path)
        ledger_path = self._ledger_path()
        snapshot = load_transactions(ledger_path)
        resolved = resolve_candidates(receipt, snapshot.transactions, config.merchant_families)

        candidate = next(
            (
                c
                for c in resolved.candidates
                if c.transaction.file_path == file_path and c.transaction.line_number == line_number
            ),
            None,
        )
        if candidate is None:
            return (
                jsonify(
                    {
                        "status": "candidate_missing",
                        "message": "Selected match candidate is no longer available.",
                    }
                ),
                409,
            )

        result = apply_match(stage_path, candidate, ledger_path=ledger_path)
        return jsonify(
            {
                "status": result.status,
                "matched_receipt_path": str(result.matched_receipt_path) if result.matched_receipt_path else None,
                "enriched_path": str(result.enriched_path) if result.enriched_path else None,
                "message": result.message,
            }
        )


_STATEMENT_IMPORTERS = {
    "cibc",
    "bmo",
    "scotia",
    "rogers",
    "mbna",
    "pcf",
    "ctfs",
    "amex",
    "wealthsimple_chequing",
}


class ImportsExtension(FavaExtensionBase):
    """Review and import supported credit-card and chequing statement CSV files."""

    report_title = "Imports"
    has_js_module = True

    def _import_config(self) -> ImportConfig:
        if not isinstance(self.config, dict):
            raise FavaAPIError(
                "beanbeaver_matcher: extension config must be a dict, e.g. "
                "'custom \"fava-extension\" \"beanbeaver_matcher.fava_ext\" \"{'ledger': '/path'}\"'"
            )
        return resolve_import_config(self.config, fava_ledger_path=Path(self.ledger.beancount_file_path))

    def imports_path(self) -> str:
        return str(self._import_config().imports_dir)

    def files(self) -> list[dict[str, str]]:
        """Supported statement CSVs available for review."""
        imports_dir = self._import_config().imports_dir
        if not imports_dir.is_dir():
            return []
        files = []
        for path in sorted(imports_dir.iterdir(), key=lambda item: item.name.lower()):
            if not path.is_file() or path.suffix.lower() != ".csv":
                continue
            try:
                importer_id = route_credit_card(path)
            except CardParseError:
                continue
            importer_label = "WEALTHSIMPLE CHEQUING" if importer_id == "wealthsimple_chequing" else importer_id.upper()
            files.append({"source_id": path.name, "name": path.name, "importer": importer_label})
        return files

    def _source_path(self, source_id: object) -> Path:
        config = self._import_config()
        source_name = str(source_id or "")
        if not source_name:
            raise CardParseError("Missing statement filename")
        source_path = (config.imports_dir / source_name).resolve()
        try:
            source_path.relative_to(config.imports_dir)
        except ValueError as exc:
            raise CardParseError("Statement must be inside the configured imports directory") from exc
        if not source_path.is_file() or source_path.suffix.lower() != ".csv":
            raise CardParseError(f"Statement not found: {source_name}")
        return source_path

    @staticmethod
    def _importer_id(value: object) -> CardImporterId | None:
        if value is None or value == "":
            return None
        importer_id = str(value).lower()
        if importer_id not in _STATEMENT_IMPORTERS:
            raise CardParseError(f"Unsupported statement importer: {value}")
        return cast(CardImporterId, importer_id)

    @staticmethod
    def _plan_payload(plan, source_id: str) -> dict[str, object]:  # noqa: ANN001
        return {
            "status": "ready",
            "source_id": source_id,
            "source_sha256": plan.source_sha256,
            "importer_id": plan.importer_id,
            "account": plan.account,
            "start_date": plan.start_date.isoformat(),
            "end_date": plan.end_date.isoformat(),
            "candidate_categories": plan.candidate_categories,
            "transactions": [
                {
                    "row_id": transaction.row_id,
                    "date": transaction.date.isoformat(),
                    "payee": transaction.payee,
                    "amount": str(transaction.amount),
                    "currency": transaction.currency,
                    "category": transaction.category,
                    "duplicate": transaction.duplicate,
                }
                for transaction in plan.transactions
            ],
        }

    @extension_endpoint("plan", methods=["GET"])  # type: ignore[arg-type]
    def plan_endpoint(self):  # noqa: ANN201 - Flask response
        source_id = request.args.get("source_id", "")
        try:
            config = self._import_config()
            source_path = self._source_path(source_id)
            plan = plan_credit_card_import(
                source_path,
                ledger_path=config.ledger_path,
                selected_account=request.args.get("selected_account") or None,
                merchant_rules_path=config.merchant_rules,
                chequing_rules_path=config.chequing_rules,
            )
        except AccountSelectionRequired as exc:
            return jsonify(
                {
                    "status": "needs_account",
                    "source_id": source_id,
                    "label": exc.label,
                    "accounts": exc.options,
                }
            )
        except (CardParseError, ImportApplyError, OSError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(self._plan_payload(plan, source_id))

    @extension_endpoint("apply", methods=["POST"])  # type: ignore[arg-type]
    def apply_endpoint(self):  # noqa: ANN201 - Flask response
        payload = request.get_json(force=True, silent=True) or {}
        raw_edits = payload.get("edits", [])
        if not isinstance(raw_edits, list):
            return jsonify({"error": "edits must be a list"}), 400
        try:
            config = self._import_config()
            source_path = self._source_path(payload.get("source_id"))
            edits = []
            for item in raw_edits:
                if not isinstance(item, dict):
                    raise CardParseError("Each edit must be an object")
                raw_amount = item.get("new_amount")
                new_amount = None if raw_amount in (None, "") else Decimal(str(raw_amount))
                edits.append(
                    TransactionEdit(
                        row_id=str(item.get("row_id", "")),
                        category=str(item.get("category", "")),
                        new_amount=new_amount,
                        deleted=item.get("deleted") is True,
                    )
                )
            result = apply_credit_card_import(
                source_path,
                ledger_path=config.ledger_path,
                records_dir=config.records_dir,
                selected_account=str(payload.get("account", "")),
                expected_source_sha256=str(payload.get("source_sha256", "")),
                edits=tuple(edits),
                importer_id=self._importer_id(payload.get("importer_id")),
                merchant_rules_path=config.merchant_rules,
                chequing_rules_path=config.chequing_rules,
            )
        except (CardParseError, ImportApplyError, InvalidOperation, OSError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify(
            {
                "status": result.status,
                "output_path": str(result.output_path),
                "transaction_count": result.transaction_count,
                "message": result.message,
            }
        )
