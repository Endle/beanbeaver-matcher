"""Fava extension: list approved receipts, rank ledger-transaction candidates, and apply
the chosen match. The one UI for beanbeaver-matcher (see README) — no CLI, no TUI.
"""

from __future__ import annotations

from pathlib import Path

from fava.ext import FavaExtensionBase, extension_endpoint
from fava.helpers import FavaAPIError
from flask import jsonify, request

from beanbeaver_matcher.apply import apply_match
from beanbeaver_matcher.config import MatcherConfig, resolve_config
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
