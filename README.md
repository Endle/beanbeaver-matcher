# beanbeaver-matcher

Match scanned receipts (staged JSON produced by [beanbeaver](https://github.com/Endle/beanbeaver))
against transactions in a [Beancount](https://beancount.github.io) ledger, and review and import
credit-card and Wealthsimple chequing statements, all from [Fava](https://github.com/beancount/fava) extensions.

Pure Python, no Rust. GPL-2.0 (links `beancount`).

## Install & run

```
pixi install
pixi run bb-match /path/to/main.beancount --receipts /path/to/receipt-chains
```

`bb-match` starts Fava with the Matcher and Imports extensions enabled against a temporary
include wrapper around your ledger. Ledger files are modified only by an Apply action: receipt
matching writes an enriched itemized entry and archives the matched receipt, while statement
import writes a validated transaction file and updates its yearly summary.

## Statement imports

Open the **Imports** report in Fava to review supported statement CSV files. The first version
supports the retiring beanbeaver importer's CIBC/Simplii, BMO, Scotiabank, Rogers, MBNA,
PC Financial, Canadian Tire Financial, and AMEX formats. It also supports Wealthsimple
`activities-export-YYYY-MM-DD.csv` chequing exports using either the `transaction_date` or newer
`effective_date` column.

The review screen resolves the open card account from the ledger, asks when multiple cards
match, suggests an open expense account, marks exact ledger duplicates, and lets each row be
edited or skipped. Applying a statement:

1. verifies that the source CSV has not changed since review;
2. validates the proposed Beancount entries before writing;
3. writes `records/<year>/<card>_<start>_<end>.beancount` and adds its include to the yearly
   summary;
4. reloads the main ledger and rolls both file changes back if the output is invalid or not
   reachable from the main ledger.

The source SHA-256 is stored as transaction metadata, making a repeated import of the exact
same CSV idempotent.

## Staged-JSON contract

This tool reads staged receipt JSON files directly (schema version `"2"`, the same shape
`beanbeaver` writes under each receipt chain's `stages/` directory). It does **not** depend on
`beanbeaver-core`; see `beanbeaver_matcher/receipts.py` for the fields it reads.

For ledgers created by older BeanBeaver versions, `legacy_receipts` can point to the flat
per-receipt directory. A legacy JSON is offered for matching when its sibling `.beancount`
draft exists and no sibling `.matched` marker exists. Matching preserves explicit item account
names and creates the legacy empty `.matched` marker.

Both staged and legacy receipts can be reviewed and edited from the Matcher report before choosing a candidate. The editor
supports merchant/date/amount fields, item accounts, and payment tenders. Split payments must
include a card tender plus any gift-card, cash, or store-credit tender, and tender amounts must
sum to the full receipt total.

Unreviewed scans appear alongside approved receipts. Choose **Review receipt** to inspect the
OCR text and warnings, correct receipt fields, and add, remove, or categorize items. **Save review**
approves a scanned receipt and recalculates match candidates. Staged edits create a new review
stage with receipt and item overrides, preserving previous stages; legacy edits update their JSON
atomically. Stale edits and edits to matched receipts are rejected. Saving a review does not write
to the ledger; **Apply** writes the selected match.

## Configuration

Extension options (in the ledger's `custom "fava-extension"` directive):

- `receipts` (required by the Matcher report): path to the directory of receipt chains to scan
  for scanned and approved/unmatched receipts.
- `legacy_receipts` (optional): path to older flat receipt directories containing sibling
  `.json`, `.beancount`, and optional `.matched` files.
- `merchant_families` (optional): path to a TOML file of merchant alias families (same format as
  beanbeaver's `merchant_families.toml`).
- `ledger` (recommended for Imports): path to the real main ledger. `bb-match` sets this because
  Fava itself is launched against a temporary wrapper.
- `imports` (optional): directory containing statement CSVs; defaults to `~/Downloads`.
- `records` (optional): output records root; defaults to `records/` beside the main ledger.
- `merchant_rules` (optional): project TOML rules loaded before the bundled credit-card categorization
  defaults. Each `[[rules]]` entry has `keywords = [...]` and `category = "Expenses:..."`.
- `chequing_rules` (optional): project TOML rules for chequing counter-accounts. Each `[[rules]]`
  entry has `pattern = "..."` and `account = "Income:..."` (or any other open ledger account).

## Known v1 limitations

- Item categories are not auto-assigned; itemized postings default to `Expenses:FIXME` (see
  `enrich.py`). Assign accounts in the receipt editor before matching, or in your beancount editor after applying.
- One receipt matches exactly one card transaction. A receipt split across multiple *credit
  cards* (multiple charges) is not supported.
