# beanbeaver-matcher

Match scanned receipts (staged JSON produced by [beanbeaver](https://github.com/Endle/beanbeaver))
against transactions in a [Beancount](https://beancount.github.io) ledger, review candidates and
apply the chosen match, all from a [Fava](https://github.com/beancount/fava) extension.

Pure Python, no Rust. GPL-2.0 (links `beancount`).

## Install & run

```
pixi install
pixi run bb-match /path/to/main.beancount
```

`bb-match` starts Fava with the Matcher extension auto-enabled against a temporary include
wrapper around your ledger — your ledger files are never modified except by the "Apply" action,
which writes an enriched itemized entry next to the matched transaction's file and archives the
matched receipt.

## Staged-JSON contract

This tool reads staged receipt JSON files directly (schema version `"2"`, the same shape
`beanbeaver` writes under each receipt chain's `stages/` directory). It does **not** depend on
`beanbeaver-core`; see `beanbeaver_matcher/receipts.py` for the fields it reads.

## Configuration

Extension options (in the ledger's `custom "fava-extension"` directive):

- `receipts` (required): path to the directory of receipt chains to scan for approved/unmatched receipts.
- `merchant_families` (optional): path to a TOML file of merchant alias families (same format as
  beanbeaver's `merchant_families.toml`).

## Known v1 limitations

- Item categories are not auto-assigned; itemized postings default to `Expenses:FIXME` (see
  `enrich.py`). Re-categorize in your beancount editor after applying a match.
- One receipt matches exactly one card transaction. A receipt split across multiple *credit
  cards* (multiple charges) is not supported.
