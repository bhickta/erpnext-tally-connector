# Native financial migration

The native inbound flow posts Tally accounting into standard ERPNext Sales Invoices,
Purchase Invoices, Payment Entries and Journal Entries. Each document must submit
synchronously, preserve its source posting date and produce exactly the source GL
by account and party before the import commits. Successful source versions are
idempotent; failed records roll back and stop the migration runner.

This migration's authorized scope is accounting. Existing ERPNext stock documents
and Stock Ledger Entries are preserved. Delivery/receipt notes are excluded from
the financial run. Invoice amounts use a nonstock accounting item; original item,
quantity and rate remain in descriptions. Financial quantities do not move stock.
Complex vouchers that cannot be faithfully represented as invoices or payments
use standard accounting Journal Entries.

## Run and verify

Run standalone scripts using the bench virtualenv Python from the bench `sites`
directory. Supply the recovery directory as `--root`; it contains the audited
source cache, ledger/item mappings and private configuration. Never commit or
publish that directory or its `bridge-config-private.json`.

1. Preserve the ERPNext recovery database, files and configuration outside backup
   retention. Disable automatic sync during migration.
2. Export every active source voucher and complete master mappings. The `audit`
   action checks cache identities, completeness, balanced entries and mappings.
   `openings` posts source financial opening balances using standard Opening Entries.
3. Run `migrate_all_tally.py import --types 'Sales,Purchase,Credit Note,Debit Note,Journal,Contra,Receipt,Payment'`.
   Resume with the same command after fixing a failure. Optional and zero-value
   vouchers are logged without fabricated financial documents.
4. Run `check_migration_source.py` to compare live IDs and alteration versions
   against every cached source voucher. The requested Tally period must be echoed
   correctly; object-count metadata is excluded from voucher records.
5. Run `verify_native_migration.py`. It verifies every current source-version log,
   one submitted standard document per nonzero voucher, dates and exact posted GL.
   Evidence is saved as `native-document-verification.json` and `.csv`.
6. If restoring a baseline whose accounting is being fully replaced, first archive
   its original accounting documents. Review the dry run of
   `retire_restored_accounting.py`; `--apply` requires complete replacement
   verification. It cancels archived accounting documents, preserves their audit
   history, and refuses documents linked to stock or stock GL. This is a separate,
   explicit replacement operation, never an automatic closing-balance adjustment.
7. Run `migrate_all_tally.py reconcile` and repeat document verification. Require
   zero differences for every mapped posted financial ledger. Review ERP balances
   outside the source mappings, source snapshot coverage and bill-reference review.
   Preserve final stock counts/totals and a recovery snapshot before cutover.
8. Run `set_migrated_party_accounts.py` after complete verification. It sets
   standard Customer/Supplier company account defaults and checks ERPNext's own
   account resolver for every mapped party. For multiple source ledger aliases it
   preserves a mapped current default, then prefers the normal balance-sheet root,
   an embedded ERP party ID and the most-used source ledger. All historical alias
   accounts and their balances remain intact. The prior defaults and selected
   accounts are saved in `migrated-party-defaults.json`; preserve an updated backup.

## Reporting boundaries

Financial reconciliation compares source opening balances plus complete posted
financial movements with ERPNext GL. It also independently compares those source
posting balances with Tally's closing statement.

Tally's `Profit & Loss A/c` can include current profit calculated from manual stock
values without any voucher posting to that ledger. The report retains that
stock-derived difference explicitly. Preserving ERPNext stock does not make its
valuation or stock-dependent profit identical to Tally. No journal is added merely
to force those reports to match.

Bill allocations resolve only to an unambiguous source invoice and its submitted
ERPNext counterpart. In accounting-only mode, references to prior receipts or
unavailable original invoices remain in journal remarks and
`bill-reference-review.json`. Exact party GL can be verified without claiming that
every historical bill allocation or invoice aging report has been migrated.

Opening counterbalances and retained stock accounting must remain visible in the
final evidence. Matching source ledger leaves alone must not hide leftover old
payment, journal or bank balances. Keep automatic sync off during accounting
replacement and retain a read-only Tally archive after cutover. Windows executable
deployment and production cutover are separate from the CLI migration result.
