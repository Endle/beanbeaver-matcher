"""bb-match: launch Fava with the Matcher extension enabled, via a temporary include
wrapper so the user's ledger is left untouched except by the "Apply" action.
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path


def build_wrapper_content(ledger_path: Path, receipts_dir: Path, merchant_families: Path | None) -> str:
    """The temp `_fava.beancount` Fava is started on: a `fava-extension` custom directive
    plus an `include` of the real ledger. Paths are POSIX-slashed to avoid backslash
    double-escaping once round-tripped through beancount's string lexer and `ast.literal_eval`.
    """
    config: dict[str, str] = {"receipts": receipts_dir.as_posix()}
    if merchant_families is not None:
        config["merchant_families"] = merchant_families.as_posix()
    config_literal = repr(config)

    return (
        'option "title" "BeanBeaver Matcher"\n\n'
        f'2000-01-01 custom "fava-extension" "beanbeaver_matcher.fava_ext" "{config_literal}"\n\n'
        f'include "{ledger_path.as_posix()}"\n'
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="bb-match", description=__doc__)
    parser.add_argument("ledger", type=Path, help="Path to the main beancount ledger file")
    parser.add_argument("--receipts", type=Path, required=True, help="Path to the receipt chains directory")
    parser.add_argument(
        "--merchant-families", type=Path, default=None, help="Path to a merchant_families.toml (optional)"
    )
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args(argv)

    ledger_path = args.ledger.expanduser().resolve()
    if not ledger_path.is_file():
        parser.error(f"Ledger file not found: {ledger_path}")
    receipts_dir = args.receipts.expanduser().resolve()
    merchant_families = args.merchant_families.expanduser().resolve() if args.merchant_families else None

    from fava.application import create_app

    with tempfile.TemporaryDirectory(prefix="bb-match-") as tmp_dir:
        wrapper_path = Path(tmp_dir) / "_fava.beancount"
        wrapper_path.write_text(build_wrapper_content(ledger_path, receipts_dir, merchant_families))

        app = create_app([wrapper_path])
        print(f"Starting Fava (Matcher) on http://{args.host}:{args.port}")
        app.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
