"""Fava extension: list approved receipts, rank ledger-transaction candidates, and apply
the chosen match. The one UI for beanbeaver-matcher (see README) — no CLI, no TUI.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import cast
from uuid import uuid4

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
from beanbeaver_matcher.ledger import load_open_accounts, load_transactions
from beanbeaver_matcher.receipts import (
    ReceiptEditError,
    is_legacy_receipt_path,
    list_approved_receipts,
    load_stage_document,
    read_receipt,
    receipt_sha256,
    receipt_chain_dir,
    update_receipt,
    stage_status,
)
from beanbeaver_matcher.scoring import find_duplicate_candidates, resolve_candidates


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

    def _receipt_edit_path(self, raw_path: object) -> Path:
        config = self._matcher_config()
        path = Path(str(raw_path or "")).resolve()
        for root in (config.receipts_dir, config.legacy_receipts_dir):
            if root is None or not path.is_relative_to(root.resolve()):
                continue
            if any(entry.stage_path.resolve() == path for entry in list_approved_receipts(root, include_scanned=True)):
                return path
        raise ReceiptEditError("Only current unmatched receipts inside configured receipt directories can be edited")

    def _approved_path(self, raw_path: object) -> Path:
        path = self._receipt_edit_path(raw_path)
        if not is_legacy_receipt_path(path) and stage_status(load_stage_document(path)) != "approved":
            raise ReceiptEditError("Save a receipt review before matching")
        return path

    def _receipt_edit_payload(self, path: Path) -> dict[str, object]:
        receipt = read_receipt(path)
        source_indices = [
            index
            for index, raw in enumerate(load_stage_document(path).get("items") or [])
            if isinstance(raw, dict) and (is_legacy_receipt_path(path) or not (raw.get("review") or {}).get("removed"))
        ]
        accounts, errors = load_open_accounts(self._ledger_path(), as_of=receipt.date)
        if errors:
            raise ReceiptEditError(f"Ledger has errors: {errors[0]}")
        return {
            "stage_path": str(path),
            "source_sha256": receipt_sha256(path),
            "merchant": receipt.merchant,
            "date": "" if receipt.date_is_placeholder else receipt.date.isoformat(),
            "subtotal": str(receipt.subtotal) if receipt.subtotal is not None else "",
            "tax": str(receipt.tax) if receipt.tax is not None else "",
            "total": str(receipt.total),
            "accounts": accounts,
            "raw_text": receipt.raw_text,
            "warnings": [warning.message for warning in receipt.warnings],
            "items": [
                {
                    "source_index": source_index,
                    "description": item.description,
                    "price": str(item.price),
                    "quantity": item.quantity,
                    "category": item.category or "",
                }
                for source_index, item in zip(source_indices, receipt.items, strict=True)
            ],
            "tenders": [
                {
                    "kind": tender.kind,
                    "amount": str(tender.amount),
                    "account": tender.account or "",
                    "raw_label": tender.raw_label,
                }
                for tender in receipt.tenders
            ],
        }

    def receipts(self) -> list[dict[str, object]]:
        """Approved receipts awaiting a match, for the report template."""
        config = self._matcher_config()
        approved_receipts = list_approved_receipts(config.receipts_dir, include_scanned=True)
        if config.legacy_receipts_dir is not None:
            approved_receipts.extend(list_approved_receipts(config.legacy_receipts_dir, include_scanned=True))
        snapshot = load_transactions(self._ledger_path())
        return [
            {
                "stage_path": str(approved.stage_path),
                "merchant": approved.receipt.merchant,
                "date": approved.receipt.date.isoformat(),
                "date_is_placeholder": approved.receipt.date_is_placeholder,
                "total": str(approved.receipt.total),
                "editable": True,
                "needs_review": not is_legacy_receipt_path(approved.stage_path)
                and stage_status(load_stage_document(approved.stage_path)) == "scanned",
                "source_sha256": receipt_sha256(approved.stage_path),
                "duplicates": [
                    {"file_path": candidate.transaction.file_path, "details": candidate.details}
                    for candidate in find_duplicate_candidates(
                        approved.receipt, snapshot.transactions, config.merchant_families
                    )
                ],
            }
            for approved in approved_receipts
        ]

    @extension_endpoint("delete-duplicate", methods=["POST"])  # type: ignore[arg-type]
    def delete_duplicate_endpoint(self):  # noqa: ANN201 - Flask response
        payload = request.get_json(force=True, silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "Receipt deletion must be an object"}), 400
        try:
            config = self._matcher_config()
            path = Path(str(payload.get("stage_path") or "")).resolve()
            root = next(
                (
                    root.resolve()
                    for root in (config.receipts_dir, config.legacy_receipts_dir)
                    if root is not None
                    and any(entry.stage_path.resolve() == path for entry in list_approved_receipts(root))
                ),
                None,
            )
            chain = receipt_chain_dir(path)
            if root is None or chain.parent != root:
                raise ReceiptEditError("Only pending receipts inside the configured receipt folders can be deleted")
            if receipt_sha256(path) != payload.get("source_sha256"):
                raise ReceiptEditError("Receipt changed; refresh the page before deleting")
            snapshot = load_transactions(self._ledger_path())
            if snapshot.errors:
                raise ReceiptEditError("Resolve ledger errors before deleting a duplicate receipt")
            if not find_duplicate_candidates(read_receipt(path), snapshot.transactions, config.merchant_families):
                raise ReceiptEditError("This receipt no longer has a possible duplicate; refresh the page")
            if any(Path(txn.file_path).resolve().is_relative_to(chain) for txn in snapshot.transactions):
                raise ReceiptEditError("This receipt folder contains active ledger entries and cannot be deleted")
            trash = root / ".trash"
            if trash.is_symlink():
                raise ReceiptEditError("Receipt trash must not be a symbolic link")
            trash.mkdir(exist_ok=True)
            destination = trash / f"{chain.name}-{uuid4().hex}"
            chain.rename(destination)
            return jsonify(
                {
                    "status": "deleted",
                    "message": f"Duplicate receipt moved to {destination}. Restore this folder to {root} to recover it.",
                }
            )
        except (ReceiptEditError, OSError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409

    @extension_endpoint("receipt", methods=["GET"])  # type: ignore[arg-type]
    def receipt_endpoint(self):  # noqa: ANN201 - Flask response
        try:
            path = self._receipt_edit_path(request.args.get("stage_path"))
            return jsonify(self._receipt_edit_payload(path))
        except (ReceiptEditError, OSError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400

    @extension_endpoint("edit-receipt", methods=["POST"])  # type: ignore[arg-type]
    def edit_receipt_endpoint(self):  # noqa: ANN201 - Flask response
        payload = request.get_json(force=True, silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({"error": "Receipt edit must be an object"}), 400
        try:
            path = self._receipt_edit_path(payload.get("stage_path"))
            raw_date = str(payload.get("date") or "").strip()
            as_of = date.fromisoformat(raw_date) if raw_date else read_receipt(path).date
            accounts, errors = load_open_accounts(self._ledger_path(), as_of=as_of)
            if errors:
                raise ReceiptEditError(f"Ledger has errors: {errors[0]}")
            path = update_receipt(
                path,
                payload,
                expected_sha256=str(payload.get("source_sha256") or ""),
                allowed_accounts=set(accounts),
            )
            return jsonify({"status": "saved", **self._receipt_edit_payload(path)})
        except (ReceiptEditError, OSError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409

    @extension_endpoint("candidates", methods=["GET"])  # type: ignore[arg-type]
    def candidates_endpoint(self):  # noqa: ANN201 - Flask response
        try:
            stage_path = self._approved_path(request.args.get("stage_path"))
        except (ReceiptEditError, OSError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409

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

        try:
            stage_path = self._approved_path(stage_path)
        except (ReceiptEditError, OSError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409

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
