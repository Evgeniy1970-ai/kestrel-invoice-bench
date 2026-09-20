#!/usr/bin/env python3
"""WF-07 step one: read what the report needs and print it. Writes nothing.

Two sheets are planned. This script only builds the summary one, and only
prints it, so the numbers can be checked before any file exists.

    python otchet.py --run 5
    python otchet.py --run 5 --limit 20

Environment, required in the window this runs in:
    DATABASE_URL   role bench_pipeline

The role matters. The report is what a bookkeeper sees, and a bookkeeper
does not see the answer key: bench_pipeline cannot read schema truth at all.
"""

import argparse
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime

import psycopg2
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

# One row per invoice. Findings are folded in by the aggregates below, which
# is why every count is a filter over the same left join rather than its own
# subquery: one pass over the run, not five.
#
# billed is what the supplier asked for. impact is what the reviewer agreed
# was wrong. should_be is their difference, and it is deliberately left NULL
# when the system refused to price a line -- see the UNIT_MISMATCH note in
# sverka.py. A blank there is honest; a number would be invented.
SUMMARY_SQL = """
select
    s.name                                       as supplier,
    i.invoice_number,
    i.invoice_date,
    r.finished_at                                as processed_at,
    i.total                                      as billed,
    count(e.id)                                  as findings,
    count(e.id) filter (
        where e.resolved_at is null)             as open_findings,
    count(e.id) filter (
        where e.resolution like 'confirmed%%')   as confirmed,
    count(e.id) filter (
        where e.type = 'DUPLICATE')              as duplicates,
    sum(e.impact_eur) filter (
        where e.resolution like 'confirmed%%')   as impact,
    count(e.id) filter (
        where e.resolution like 'confirmed%%'
          and e.impact_eur is null)              as confirmed_no_amount
from doc.invoices i
left join ref.suppliers s on s.supplier_code = i.supplier_code
left join rec.runs r on r.id = i.run_id
left join rec.exceptions e on e.invoice_id = i.id and e.run_id = i.run_id
where i.run_id = %s
group by i.id, s.name, i.invoice_number, i.invoice_date, r.finished_at,
         i.total
order by i.invoice_date, i.invoice_number, i.id
"""

# One row per finding, ordered so an invoice's findings sit together. Two
# rows about the same discrepancy are left as two: the count of findings is
# what the bench measures, and collapsing them here would report a number
# the system does not produce. Neighbouring rows make the repeat obvious
# without hiding it.
#
# A finding about the document as a whole -- a duplicate, a subtotal that
# does not follow from its lines -- has no line of its own, and those
# columns stay empty on purpose.
FINDINGS_SQL = """
select
    i.invoice_number,
    s.name                          as supplier,
    l.line_no,
    l.raw_description               as item,
    e.type,
    l.unit_price                    as billed_price,
    lc.expected_unit_price          as contract_price,
    e.impact_eur                    as impact,
    e.message,
    e.resolution,
    e.reason,
    e.note
from rec.exceptions e
join doc.invoices i on i.id = e.invoice_id
left join ref.suppliers s on s.supplier_code = i.supplier_code
left join doc.invoice_lines l on l.id = e.line_id
left join rec.line_checks lc on lc.invoice_line_id = e.line_id
                            and lc.run_id = e.run_id
where e.run_id = %s
order by i.invoice_date, i.invoice_number, i.id,
         l.line_no nulls first, e.id
"""

# Five states, in the order a bookkeeper cares about them. A duplicate
# outranks everything else: if the invoice should not be paid at all,
# arguing about a line price on it is beside the point.
CLEAN = "clean"
OPEN = "needs attention"
CONFIRMED = "discrepancy confirmed"
UNPRICED = "discrepancy, amount unknown"
REVIEWED = "reviewed, no issue"
DUPLICATE = "duplicate invoice"
# What each finding is called on the report. The codes are ours; a
# bookkeeper reads none of them. Three are worded to say the system did not
# check rather than that the supplier did something wrong -- naming those
# briefly would read as an accusation the run never made.
FINDING_NAMES = {
    "OVERCHARGE": "Price above contract",
    "UNDERCHARGE": "Price below contract",
    "STALE_PRICE": "Old price list used",
    "MISSING_DISCOUNT": "Contract discount not applied",
    "WRONG_VAT_RATE": "Wrong VAT rate",
    "ARITHMETIC_MISMATCH": "Invoice does not add up",
    "UNIT_MISMATCH": "Priced in a different unit - not checked",
    "SKU_UNMATCHED": "Not a contract item",
    "NO_DELIVERY_DATE": "No delivery date - price not checked",
    "LOW_CONFIDENCE": "VAT bases not checked",
    "DUPLICATE": "Duplicate invoice",
}

# Same bucket progon.py and razbor.py use. Repeated here rather than
# imported: this script must run when the dashboard is not.
PROJECT = "fltszyoexhwblgksbezm"
BUCKET = "kestrel-raw"
STORAGE_URL = f"https://{PROJECT}.supabase.co/storage/v1/object/{BUCKET}/"

def status_of(row):
    """Which of the six states this invoice is in.

    Order matters. A duplicate outranks everything: if the invoice should
    not be paid at all, arguing about a line price on it is beside the
    point. And a confirmed discrepancy the system refused to price is kept
    apart from one it could -- the two need different work from a person.
    """
    if row["duplicates"]:
        return DUPLICATE
    if row["findings"] == 0:
        return CLEAN
    if row["open_findings"]:
        return OPEN
    if row["confirmed"]:
        if row["confirmed_no_amount"] or row["impact"] is None:
            return UNPRICED
        return CONFIRMED
    return REVIEWED


def should_be(row, status):
    """What the invoice ought to have come to, or None when we cannot say.

    None is not a failure. A confirmed finding with no amount means the
    system declined to price that line on purpose, and subtracting what we
    have would understate the gap while looking exact.
    """
    if status in (DUPLICATE, CLEAN, REVIEWED, UNPRICED):
        return None
    if row["confirmed_no_amount"]:
        return None
    if row["impact"] is None:
        return None
    return row["billed"] - row["impact"]


def read_summary(cur, run_id):
    cur.execute(SUMMARY_SQL, (run_id,))
    columns = [c.name for c in cur.description]
    rows = []
    for values in cur.fetchall():
        row = dict(zip(columns, values))
        row["status"] = status_of(row)
        row["should_be"] = should_be(row, row["status"])
        rows.append(row)
    return rows

def read_findings(cur, run_id):
    """Every finding of the run, in the order the second sheet shows them."""
    cur.execute(FINDINGS_SQL, (run_id,))
    columns = [c.name for c in cur.description]
    return [dict(zip(columns, values)) for values in cur.fetchall()]


def decision_of(row):
    """What a person decided about this finding, in one word.

    resolution starts with the decision word by convention -- see the note
    in razbor.py. A rule closure does not start with one, so it is named
    separately rather than mistaken for a person's verdict.
    """
    resolution = row["resolution"]
    if not resolution:
        return "not reviewed"
    first = resolution.split(" ", 1)[0]
    if first in ("confirmed", "rejected", "mapped"):
        return first
    return "closed by rule"


def money(value):
    return "" if value is None else f"{value:>10,.2f}"


def print_rows(rows, limit):
    head = (f"  {'invoice':<18}{'supplier':<22}{'date':<12}"
            f"{'billed':>12}{'should be':>12}{'gap':>10}  "
            f"{'status':<24}{'n':>3}")
    print()
    print(head)
    print("  " + "-" * (len(head) - 2))

    for row in rows[:limit]:
        gap = None
        if row["should_be"] is not None:
            gap = row["billed"] - row["should_be"]
        print(f"  {row['invoice_number']:<18}{(row['supplier'] or '')[:20]:<22}"
              f"{str(row['invoice_date']):<12}"
              f"{money(row['billed'])}{money(row['should_be'])}"
              f"{money(gap)[:10]:>10}  "
              f"{row['status']:<24}{row['findings']:>3}")

    if len(rows) > limit:
        print(f"\n  {len(rows) - limit} more rows not shown "
              f"(--limit {len(rows)} for all)")

def print_findings(rows, limit):
    """The second sheet, printed so the numbers can be checked."""
    head = (f"  {'invoice':<18}{'ln':>3}  {'item':<24}{'what':<22}"
            f"{'billed':>9}{'contract':>9}{'gap':>9}  {'why':<40}")
    print()
    print(head)
    print("  " + "-" * (len(head) - 2))

    for row in rows[:limit]:
        line_no = "" if row["line_no"] is None else str(int(row["line_no"]))
        print(f"  {row['invoice_number']:<18}{line_no:>3}  "
              f"{(row['item'] or '')[:22]:<24}{row['type'][:20]:<22}"
              f"{money(row['billed_price']):>11}"
              f"{money(row['contract_price']):>11}"
              f"{money(row['impact']):>11}  "
              f"{(row['message'] or '')[:38]:<40}")

    if len(rows) > limit:
        print(f"\n  {len(rows) - limit} more findings not shown")


def print_totals(rows):
    counts = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1

    print("\n  by status")
    print("  " + "-" * 44)
    for status in (CLEAN, OPEN, CONFIRMED, UNPRICED, REVIEWED, DUPLICATE):
        print(f"  {status:<28}{counts.get(status, 0):>6}")
    print(f"  {'total':<28}{len(rows):>6}")

    billed = sum(r["billed"] or 0 for r in rows)
    over = sum(r["impact"] for r in rows
               if r["status"] == CONFIRMED
               and r["impact"] is not None and r["impact"] > 0)
    under = sum(-r["impact"] for r in rows
                if r["status"] == CONFIRMED
                and r["impact"] is not None and r["impact"] < 0)
    partial = sum(r["impact"] for r in rows
                  if r["status"] == UNPRICED
                  and r["impact"] is not None and r["impact"] > 0)
    doubled = sum(r["billed"] for r in rows if r["status"] == DUPLICATE)
    blank = sum(1 for r in rows
                if r["status"] == CONFIRMED and r["should_be"] is None)

    print("\n  money")
    print("  " + "-" * 44)
    print(f"  {'billed in total':<28}{billed:>16,.2f}")
    print(f"  {'overcharged':<28}{over:>16,.2f}")
    if partial:
        print(f"  {'+ where total unknown':<28}{partial:>16,.2f}")
    if under:
        print(f"  {'undercharged, your favour':<28}{under:>16,.2f}")
    print(f"  {'billed twice':<28}{doubled:>16,.2f}")
    if blank:
        print(f"\n  {blank} invoice(s) confirmed wrong with no amount: "
              f"the system declined to price the line.")

HEAD_FILL = PatternFill("solid", fgColor="1F3864")
HEAD_FONT = Font(name="Arial", size=10, bold=True, color="FFFFFF")
BODY_FONT = Font(name="Arial", size=10)
NOTE_FONT = Font(name="Arial", size=9, italic=True, color="595959")

MONEY = '#,##0.00;-#,##0.00;""'


def write_sheet(ws, headers, widths, rows_out, note):
    """One sheet: a note, a header row, then the data. No formulas.

    Numbers are written as numbers, not as formulas over other cells. This
    file is a snapshot of what the run found on a date, not a model to be
    edited: a formula here would be an invitation to change the figures and
    have the rest follow, and the file would stop matching the database.
    """
    ws["A1"] = note
    ws["A1"].font = NOTE_FONT

    for column, (header, width) in enumerate(zip(headers, widths), start=1):
        cell = ws.cell(row=3, column=column, value=header)
        cell.font = HEAD_FONT
        cell.fill = HEAD_FILL
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(column)].width = width

    for r, values in enumerate(rows_out, start=4):
        for c, value in enumerate(values, start=1):
            cell = ws.cell(row=r, column=c, value=value)
            cell.font = BODY_FONT
            if isinstance(value, float):
                cell.number_format = MONEY

    ws.freeze_panes = "A4"
    ws.auto_filter.ref = (f"A3:{get_column_letter(len(headers))}"
                          f"{max(3, len(rows_out) + 3)}")


def write_workbook(path, run_id, rows, findings):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    note = (f"Kestrel Invoice Bench - run {run_id}, exported {stamp}. "
            f"A snapshot of what the run found; figures are values, not "
            f"formulas. Synthetic data.")

    book = Workbook()

    summary = book.active
    summary.title = "Invoices"
    write_sheet(
        summary,
        ["Supplier", "Invoice no.", "Invoice date", "Processed",
         "Billed, EUR", "Should be, EUR", "Difference, EUR",
         "Status", "Findings"],
        [24, 18, 13, 18, 14, 15, 15, 28, 10],
        [[row["supplier"], row["invoice_number"],
          row["invoice_date"], naive(row["processed_at"]),
          as_float(row["billed"]), as_float(row["should_be"]),
          gap_of(row), row["status"], row["findings"]]
         for row in rows],
        note)

    detail = book.create_sheet("Findings")
    write_sheet(
        detail,
        ["Invoice no.", "Supplier", "Line", "Item", "Finding",
         "Line price, EUR", "Contract price, EUR", "Amount, EUR",
         "What the system says", "Decision"],
        [18, 24, 6, 30, 34, 15, 17, 13, 62, 14],
        [[row["invoice_number"], row["supplier"],
          None if row["line_no"] is None else int(row["line_no"]),
          row["item"],
          FINDING_NAMES.get(row["type"], row["type"]),
          as_float(row["billed_price"]), as_float(row["contract_price"]),
          as_float(row["impact"]), row["message"], decision_of(row)]
         for row in findings],
        note)

    book.save(path)
    return path


def as_float(value):
    return None if value is None else float(value)

def naive(value):
    """The day the run finished. Excel has no timezones, and the clock time
    of a batch run tells a bookkeeper nothing -- the date is the fact."""
    return None if value is None else value.date()

def gap_of(row):
    if row["should_be"] is None:
        return None
    return float(row["billed"] - row["should_be"])

def safe_name(text):
    """A filename piece with nothing in it a filesystem objects to."""
    return re.sub(r"[^A-Za-z0-9._-]", "-", str(text or ""))


def fetch_pdf(storage_path, key):
    """Download one PDF from Supabase Storage. Copied from razbor.py."""
    request = urllib.request.Request(
        STORAGE_URL + storage_path,
        headers={"Authorization": f"Bearer {key}", "apikey": key},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"storage HTTP {error.code}: {body}")
    except urllib.error.URLError as error:
        raise RuntimeError(f"storage unreachable: {error.reason}")


FILES_SQL = """
select i.id, i.invoice_date, i.supplier_code, i.invoice_number,
       d.storage_path
from doc.invoices i
join doc.documents d on d.id = i.document_id
where i.run_id = %s
order by i.invoice_date, i.invoice_number, i.id
"""


def save_pdfs(cur, run_id, folder, statuses, key, limit):
    """Copy the invoices out of the bucket and onto disk, renamed.

    The name is built from the database, never from source_filename. The
    file in the bucket is called INV-0056_AB26-04-0899.pdf, and that INV-
    prefix is the ground-truth journal's own numbering: a folder of those
    would announce which documents carry a planted defect, and no signal
    other than the defect itself may predict the defect.

    id goes on the end of every name, not only where two would collide. A
    duplicate shares its date, supplier and number with the original, so
    collisions are certain; and an unconditional id makes every file
    traceable back to its row even when the invoice number was misread.
    """
    os.makedirs(folder, exist_ok=True)
    cur.execute(FILES_SQL, (run_id,))

    saved = 0
    failed = []
    for invoice_id, date, supplier, number, storage_path in cur.fetchall():
        if limit and saved >= limit:
            break
        if not storage_path:
            failed.append((number, "no file in the bucket"))
            continue

        name = "_".join([
            str(date),
            safe_name(supplier),
            safe_name(number),
            safe_name(statuses.get(invoice_id, "unknown")).replace(",-", ""),
            f"i{invoice_id}",
        ]) + ".pdf"

        try:
            data = fetch_pdf(storage_path, key)
        except RuntimeError as error:
            failed.append((number, str(error)))
            continue

        with open(os.path.join(folder, name), "wb") as handle:
            handle.write(data)
        saved += 1

    return saved, failed

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=int, required=True,
                        help="rec.runs.id to report on")
    parser.add_argument("--limit", type=int, default=15,
                        help="rows to print (default 15)")
    parser.add_argument("--write", metavar="PATH",
                        help="also write the workbook to this path")
    parser.add_argument("--pdf", metavar="FOLDER",
                        help="also copy the invoice PDFs into this folder")
    parser.add_argument("--pdf-limit", type=int, default=10,
                        help="how many PDFs to copy (default 10, 0 for all)")
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        sys.exit("DATABASE_URL is not set in this window.")

    key = os.environ.get("SUPABASE_KEY")
    if args.pdf and not key:
        sys.exit("SUPABASE_KEY is not set in this window; --pdf needs it.")
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            rows = read_summary(cur, args.run)
            findings = read_findings(cur, args.run)
            copied = None
            if args.pdf:
                statuses = {r["invoice_number"]: r["status"] for r in rows}
                by_id = {}
                cur.execute("select id, invoice_number from doc.invoices "
                            "where run_id = %s", (args.run,))
                for invoice_id, number in cur.fetchall():
                    by_id[invoice_id] = statuses.get(number, "unknown")
                copied = save_pdfs(cur, args.run, args.pdf, by_id, key,
                                   args.pdf_limit)
    finally:
        conn.close()

    if not rows:
        sys.exit(f"Run {args.run} has no invoices.")

    print(f"\n  run {args.run}, {len(rows)} invoices, "
          f"{len(findings)} findings")
    print_rows(rows, args.limit)
    print_findings(findings, args.limit)
    print_totals(rows)

    if args.write:
        path = write_workbook(args.write, args.run, rows, findings)
        print(f"\n  Written: {path}")
        print(f"  {len(rows)} invoices, {len(findings)} findings, "
              f"two sheets.")
    else:
        print("\n  Nothing was written. Add --write PATH "
              "to store the workbook.")

    if copied is not None:
        saved, failed = copied
        print(f"\n  PDFs copied: {saved} into {args.pdf}")
        if args.pdf_limit:
            print(f"  Limit was {args.pdf_limit}. "
                  f"--pdf-limit 0 copies the whole run.")
        for number, reason in failed:
            print(f"  not copied: {number} - {reason}")

    print()


if __name__ == "__main__":
    main()
