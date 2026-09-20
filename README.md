# Kestrel Invoice Bench

A supplier invoice checking system built on n8n, Claude and Postgres. What matters here is not that it finds discrepancies — it is that we know **how many it misses**.

## Why a measurement bench

An ordinary system reports: "found 14 overcharged invoices." A bench answers a different question — what share of the discrepancies did it find, and what share did it miss? Answering that requires knowing the right answer in advance.

So this project was built backwards. First came the batch: 260 documents with 98 defects of eight types planted on purpose, and a ledger recording which document, which line, which field was corrupted and what the correct value should have been. Only then was the pipeline built — the one that gets measured against that ledger.

## Results

| | |
|---|---|
| Documents in the batch | 260 (248 invoices + 12 duplicates) |
| Defects planted | 98 across 90 documents |
| Found and named correctly | 94 |
| Found, named differently | 2 |
| Not found | 2 |
| Time to process the batch | 1 h 30 min |

The two misses are byte-identical copies of earlier invoices. They are rejected at intake by file fingerprint and never reach reconciliation — that is the first line of defence doing its job, not a gap.

**The data is synthetic, and that is the point.** A ledger with a known answer is what makes an exact figure possible. Real documents without one do not.

## How it works

```
email → original storage                          [n8n]
      → extraction (model) → arithmetic (code)    [Python]
      → catalogue matching
      → price as of delivery date
      → reconciliation → classification           [Python + Postgres]
      → [clean]    archive → reports
      → [disputed] review screen → decision       [Python, web UI]
```

Mail intake and failure handling run as n8n workflows. Everything after
that — extraction, arithmetic, matching, reconciliation, the review screen,
metrics and reports — is Python against Postgres.

Two rules run through the whole thing.

**The model reads, the code counts.** The model extracts what it sees in the document and points to the fragment each value came from. Not one arithmetic operation and not one price-list comparison is delegated to it — not out of distrust, but for reproducibility: a run you cannot repeat measures nothing.

**The original is never altered.** The system does not correct a supplier's invoice; it raises a correction request instead. Rewriting someone else's document destroys the audit trail.

**Prices are looked up by delivery date, not invoice date.** Some April invoices cover March deliveries. Without that gap, an entire class of discrepancy — being billed against a stale price list — is invisible.

## What is in this repository

```
sql/       schemas: reference data, documents, reconciliation,
           ground truth isolated by database roles
n8n/       two workflows: mail intake and error handling
prompts/   three versions of the extraction prompt
python/    extraction, batch runs, reconciliation, metrics,
           reports, supplier flags — and the review screen:
           a local web UI showing the invoice beside its findings,
           one decision per finding, every decision logged
```

The invoice batch and the ground-truth ledger are not included.

## Stack

Python · PostgreSQL (Supabase) · Claude API · n8n

## More on this

The system was built in the open, one decision at a time, mistakes included:

- [Build log on LinkedIn](https://www.linkedin.com/in/yevhenii-nohin-06b403347/)
- [NeolithAI Agency](https://neolithai.agency/)

## License

MIT — see [LICENSE](LICENSE).
