#!/usr/bin/env python3
"""Supplier standing over a period. Reads and prints; writes nothing yet.

The Excel report answers "what is in this batch". This answers a different
question: "is there a supplier something is consistently wrong with". Same
data, different unit -- a supplier over 90 days rather than an invoice in
one run.

    python flagi.py
    python flagi.py --as-of 2026-04-30

The window slides: it always looks back window_days from --as-of, which
defaults to today. A calendar month would split a supplier who overcharged
on the 28th and again on the 3rd into two single incidents.

Every threshold lives in ref.flag_settings, not here. A client will have
their own idea of how often is too often, and changing it must not mean
changing code.

Environment:
    DATABASE_URL   role bench_pipeline

What this does NOT do: draw a conclusion. It counts, groups and lists the
facts with their dates. Whether a supplier is careless, unlucky or worse is
a judgement for a person who knows the relationship.
"""

import argparse
import os
import sys
from datetime import date, datetime, timedelta

import psycopg2

SETTINGS_SQL = "select name, value from ref.flag_settings"

# One row per supplier over the window. Side is decided by the sign of
# impact_eur, never by the finding's type: WRONG_VAT_RATE goes both ways,
# and an ARITHMETIC_MISMATCH with a positive amount is an inflated total,
# which is always in the supplier's favour. Findings with no amount are
# counted apart -- their side cannot be known.
STANDING_SQL = """
select
    i.supplier_code,
    s.name,
    count(distinct i.id)                                as invoices,
    count(distinct i.id) filter (
        where e.resolution like 'confirmed%%'
          and e.impact_eur > 0)                         as invoices_against,
    count(e.id) filter (
        where e.resolution like 'confirmed%%'
          and e.impact_eur > 0)                         as findings_against,
    count(e.id) filter (
        where e.resolution like 'confirmed%%'
          and e.impact_eur < 0)                         as findings_our_way,
    count(e.id) filter (
        where e.resolution like 'confirmed%%'
          and e.impact_eur is null)                     as findings_no_amount,
    coalesce(sum(e.impact_eur) filter (
        where e.resolution like 'confirmed%%'
          and e.impact_eur > 0), 0)                     as amount_against,
    count(distinct i.id) filter (
        where e.resolution like 'confirmed%%'
          and e.impact_eur > 0
          and i.invoice_date >= %s)                     as recent_against
from (
    select distinct on (document_id) *
    from doc.invoices
    where invoice_date between %s and %s
    order by document_id, run_id desc
) i
left join ref.suppliers s on s.supplier_code = i.supplier_code
left join rec.exceptions e on e.invoice_id = i.id and e.run_id = i.run_id
group by i.supplier_code, s.name
order by 4 desc, 3 desc
"""

# The evidence behind a flag: what a person takes to the conversation.
EVIDENCE_SQL = """
select i.invoice_date, i.invoice_number, e.type, e.impact_eur, e.message
from rec.exceptions e
join doc.invoices i on i.id = e.invoice_id
where i.supplier_code = %s
  and i.invoice_date between %s and %s
  and e.resolution like 'confirmed%%'
  and e.impact_eur > 0
order by i.invoice_date, i.invoice_number, e.id
"""

# Largest single discrepancies, whoever they belong to. A one-off overcharge
# on a large order raises no flag -- one invoice is not a pattern -- and
# would otherwise never reach the person who should see it.
LARGEST_SQL = """
select i.invoice_date, s.name, i.invoice_number, e.type, e.impact_eur
from rec.exceptions e
join doc.invoices i on i.id = e.invoice_id
left join ref.suppliers s on s.supplier_code = i.supplier_code
where i.invoice_date between %s and %s
  and e.resolution like 'confirmed%%'
  and e.impact_eur > 0
order by e.impact_eur desc
limit %s
"""

FLAG_OFTEN = "Consistently priced in their favour"
FLAG_SHARE = "Most invoices carry a discrepancy"

# Same names the Excel report uses. Repeated rather than imported: these
# two scripts are run separately and neither should need the other.
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


def load_settings(cur):
    cur.execute(SETTINGS_SQL)
    found = {name: float(value) for name, value in cur.fetchall()}
    missing = [n for n in ("window_days", "recent_days", "min_invoices",
                           "flag_every_days", "flag_share_pct",
                           "near_miss_pct", "top_amounts")
               if n not in found]
    if missing:
        sys.exit(f"ref.flag_settings has no {', '.join(missing)}.")
    return found


def judge(row, settings, window_days):
    """Flag, near miss or neither -- and why.

    Frequency is the main test: how often a discrepancy arrives, not what
    share of invoices carries one. A share punishes a small supplier and
    lets a large one hide, and the largest supplier in the batch has the
    lowest share while having the second-highest count.

    The share test is a backstop for the other direction: a supplier
    billing once a fortnight cannot reach the frequency threshold even if
    every single invoice is wrong.
    """
    if row["invoices"] < settings["min_invoices"]:
        return "thin", []

    reasons = []
    near = []

    if row["findings_against"]:
        every = window_days / row["findings_against"]
        if every <= settings["flag_every_days"]:
            reasons.append(f"{FLAG_OFTEN}: one every {every:.0f} days")
        elif every <= settings["flag_every_days"] / (
                settings["near_miss_pct"] / 100):
            near.append(f"one every {every:.0f} days")

    if row["invoices"]:
        share = 100 * row["invoices_against"] / row["invoices"]
        if share >= settings["flag_share_pct"]:
            reasons.append(f"{FLAG_SHARE}: {share:.0f}% of invoices")
        elif share >= settings["flag_share_pct"] * (
                settings["near_miss_pct"] / 100):
            near.append(f"{share:.0f}% of invoices")

    if reasons:
        return "flag", reasons
    if near:
        return "near", near
    return "quiet", []


def money(value):
    return "" if value is None else f"{float(value):,.2f}"


def print_report(rows, settings, since, as_of, evidence, largest, undated):
    window = (as_of - since).days
    print(f"\n  SUPPLIER STANDING")
    print(f"  {since} to {as_of} ({window} days)")
    print(f"  {len(rows)} suppliers, "
          f"{sum(r['invoices'] for r in rows)} invoices")
    if undated:
        print(f"\n  {undated} invoice(s) carry no readable date and are not "
              f"in any of the counts below.")
    print()

    flagged = [r for r in rows if r["verdict"] == "flag"]
    near = [r for r in rows if r["verdict"] == "near"]
    thin = [r for r in rows if r["verdict"] == "thin"]

    if flagged:
        print("  " + "=" * 76)
        print(f"  FLAGGED — {len(flagged)} supplier(s)")
        print("  " + "=" * 76)
        for row in flagged:
            print(f"\n  {row['name']}")
            for reason in row["reasons"]:
                print(f"    {reason}")
            tail = ""
            if row["recent_against"]:
                tail = (f"; {row['recent_against']} in the last "
                        f"{int(settings['recent_days'])} days")
            print(f"    {row['invoices_against']} of {row['invoices']} "
                  f"invoices, {money(row['amount_against'])} EUR"
                  f" in total{tail}")
            print()
            for when, number, kind, amount, message in evidence.get(
                    row["supplier_code"], []):
                print(f"      {when}  {number:<18}{kind:<22}"
                      f"{money(amount):>10}")
    else:
        print("  No supplier meets the thresholds.")

    if near:
        print("\n  " + "-" * 76)
        print("  CLOSE TO THE LINE")
        print("  " + "-" * 76)
        for row in near:
            print(f"  {row['name']:<28}{row['invoices_against']:>3} of "
                  f"{row['invoices']:<4}  {'; '.join(row['reasons'])}")

    if largest:
        print("\n  " + "-" * 76)
        print("  LARGEST SINGLE DISCREPANCIES")
        print("  " + "-" * 76)
        for when, supplier, number, kind, amount in largest:
            print(f"  {str(when)}  {(supplier or '')[:24]:<26}"
                  f"{number:<18}{kind:<22}{money(amount):>10}")

    if thin:
        print("\n  " + "-" * 76)
        print(f"  TOO FEW INVOICES TO JUDGE "
              f"(under {int(settings['min_invoices'])})")
        print("  " + "-" * 76)
        for row in thin:
            print(f"  {row['name']:<28}{row['invoices']:>3} invoices, "
                  f"{row['findings_against']} discrepancy(ies) in their favour")

    print("\n  " + "-" * 76)
    print("  Thresholds applied")
    print("  " + "-" * 76)
    print(f"  A flag needs one finding every "
          f"{int(settings['flag_every_days'])} days or more often,")
    print(f"  or {int(settings['flag_share_pct'])}% of invoices affected, "
          f"and at least {int(settings['min_invoices'])} invoices.")
    print(f"  These live in ref.flag_settings and are meant to be changed.\n")


def write_markdown(path, rows, settings, since, as_of, evidence, largest,
                   undated):
    """The same standing as a document, not a table.

    A spreadsheet would fight this: the sections are of different shapes,
    and the evidence list under a flagged supplier is the point of the
    whole thing. Markdown reads as written and turns into PDF later.
    """
    out = []
    add = out.append

    add(f"# Supplier standing")
    add("")
    add(f"**{since} to {as_of}** ({(as_of - since).days} days) · "
        f"{len(rows)} suppliers · "
        f"{sum(r['invoices'] for r in rows)} invoices")
    add("")
    if undated:
        add(f"> {undated} invoice(s) carry no readable date and are not in "
            f"any of the counts below.")
        add("")
    add("This is a list of facts with their dates, not a conclusion. "
        "Whether a supplier is careless, unlucky or worse is a judgement "
        "for someone who knows the relationship.")
    add("")

    flagged = [r for r in rows if r["verdict"] == "flag"]
    near = [r for r in rows if r["verdict"] == "near"]
    thin = [r for r in rows if r["verdict"] == "thin"]

    add("## Flagged")
    add("")
    if not flagged:
        add("No supplier meets the thresholds for this period.")
        add("")
    for row in flagged:
        add(f"### {row['name']}")
        add("")
        for reason in row["reasons"]:
            add(f"- {reason}")
        tail = ""
        if row["recent_against"]:
            tail = (f", {row['recent_against']} of them in the last "
                    f"{int(settings['recent_days'])} days")
        add(f"- {row['invoices_against']} of {row['invoices']} invoices, "
            f"{money(row['amount_against'])} EUR in total{tail}")
        add("")
        add("| Date | Invoice | Finding | EUR |")
        add("|---|---|---|---|")
        for when, number, kind, amount, message in evidence.get(
                row["supplier_code"], []):
            add(f"| {when} | {number} | {FINDING_NAMES.get(kind, kind)} "
                f"| {money(amount)} |")
        add("")

    if near:
        add("## Close to the line")
        add("")
        add("Not flagged under the current thresholds, and the thresholds "
            "are a setting.")
        add("")
        add("| Supplier | Invoices affected | How close |")
        add("|---|---|---|")
        for row in near:
            add(f"| {row['name']} | {row['invoices_against']} of "
                f"{row['invoices']} | {'; '.join(row['reasons'])} |")
        add("")

    if largest:
        add("## Largest single discrepancies")
        add("")
        add("Independent of any flag: a one-off overcharge on a large "
            "order is not a pattern, and would otherwise not appear here.")
        add("")
        add("| Date | Supplier | Invoice | Finding | EUR |")
        add("|---|---|---|---|---|")
        for when, supplier, number, kind, amount in largest:
            add(f"| {when} | {supplier or ''} | {number} | "
                f"{FINDING_NAMES.get(kind, kind)} | {money(amount)} |")
        add("")

    if thin:
        add("## Too few invoices to judge")
        add("")
        add(f"Under {int(settings['min_invoices'])} invoices in the window. "
            f"Listed so nothing is hidden; no flag is raised on this much "
            f"evidence.")
        add("")
        add("| Supplier | Invoices | In their favour |")
        add("|---|---|---|")
        for row in thin:
            add(f"| {row['name']} | {row['invoices']} | "
                f"{row['findings_against']} |")
        add("")

    add("## Thresholds applied")
    add("")
    add(f"- A flag needs one finding every "
        f"{int(settings['flag_every_days'])} days or more often")
    add(f"- or {int(settings['flag_share_pct'])}% of invoices affected")
    add(f"- and at least {int(settings['min_invoices'])} invoices "
        f"in the window")
    add("")
    add("These live in `ref.flag_settings` and are meant to be changed.")
    add("")

    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(out))
    return path

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", metavar="YYYY-MM-DD",
                        help="last day of the window (default: today)")
    parser.add_argument("--write", metavar="PATH",
                        help="write the standing to this markdown file")
    args = parser.parse_args()

    as_of = date.today()
    if args.as_of:
        as_of = datetime.strptime(args.as_of, "%Y-%m-%d").date()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        sys.exit("DATABASE_URL is not set in this window.")

    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            settings = load_settings(cur)
            window_days = int(settings["window_days"])
            since = as_of - timedelta(days=window_days)
            recent_since = as_of - timedelta(days=int(settings["recent_days"]))

            cur.execute(STANDING_SQL, (recent_since, since, as_of))
            columns = [c.name for c in cur.description]
            rows = []
            for values in cur.fetchall():
                row = dict(zip(columns, values))
                row["verdict"], row["reasons"] = judge(row, settings,
                                                       window_days)
                rows.append(row)

            evidence = {}
            for row in rows:
                if row["verdict"] == "flag":
                    cur.execute(EVIDENCE_SQL,
                                (row["supplier_code"], since, as_of))
                    evidence[row["supplier_code"]] = cur.fetchall()

            cur.execute(LARGEST_SQL,
                        (since, as_of, int(settings["top_amounts"])))
            largest = cur.fetchall()

            cur.execute("""
                select count(distinct document_id)
                from doc.invoices
                where invoice_date is null
            """)
            undated = cur.fetchone()[0]
    finally:
        conn.close()

    if not rows:
        sys.exit(f"No invoices between {since} and {as_of}.")

    print_report(rows, settings, since, as_of, evidence, largest, undated)

    if args.write:
        path = write_markdown(args.write, rows, settings, since, as_of,
                              evidence, largest, undated)
        print(f"  Written: {path}\n")


if __name__ == "__main__":
    main()
