"""WF-06: score a run against the reference batch."""

import argparse
import os
import sys

import psycopg2

MAP_SQL = """
select
    d.id                as document_id,
    d.file_sha256       as sha,
    d.source_filename   as filename,
    m.truth_doc_id      as truth_doc_id
from doc.extractions e
join doc.documents d on d.id = e.document_id
left join score.mail_map m on m.file_sha256 = d.file_sha256
where e.run_id = %s
order by d.id
"""

WRITE_MAP_SQL = """
insert into score.doc_map (run_id, document_id, truth_doc_id, matched_by)
values (%s, %s, %s, 'sha256')
on conflict (run_id, document_id) do update
   set truth_doc_id = excluded.truth_doc_id,
       matched_by   = excluded.matched_by
"""
FIELDS_SQL = """
select
    m.truth_doc_id,
    t.invoice_number, t.supplier, t.venue,
    t.received_date, t.line_count, t.total_eur,
    i.invoice_number, s.name, v.name,
    i.invoice_date, count(l.id), i.total
from score.doc_map m
join truth.documents t on t.doc_id = m.truth_doc_id
join doc.extractions x on x.document_id = m.document_id and x.run_id = m.run_id
join doc.invoices i on i.extraction_id = x.id
left join ref.suppliers s on s.supplier_code = i.supplier_code
left join ref.venues v on v.venue_code = i.venue_code
left join doc.invoice_lines l on l.invoice_id = i.id
where m.run_id = %s
group by m.truth_doc_id, t.invoice_number, t.supplier, t.venue,
         t.received_date, t.line_count, t.total_eur,
         i.invoice_number, s.name, v.name, i.invoice_date, i.total
order by m.truth_doc_id
"""

WRITE_FIELD_SQL = """
insert into score.fields
       (run_id, truth_doc_id, field, truth_value, extracted_value, is_match)
values (%s, %s, %s, %s, %s, %s)
on conflict (run_id, truth_doc_id, field) do update
   set truth_value     = excluded.truth_value,
       extracted_value = excluded.extracted_value,
       is_match        = excluded.is_match
"""

SEGMENTS_SQL = """
select m.truth_doc_id, t.layout, t.file_kind
from score.doc_map m
join truth.documents t on t.doc_id = m.truth_doc_id
where m.run_id = %s
"""

WRITE_SUMMARY_SQL = """
insert into score.summary (run_id, metric, segment, value)
values (%s, %s, %s, %s)
on conflict (run_id, metric, segment) do update
   set value = excluded.value
"""

FILE_KIND_NAMES = {
    "PDF с текстом": "pdf_text",
    "скан: аккуратный скан": "scan_clean",
    "скан: потрёпанный": "scan_worn",
    "скан: снято телефоном": "scan_phone",
}

CLEAR_SQL = """
delete from score.defects where run_id = %s;
delete from score.false_alarms where run_id = %s;
"""

WRITE_DEFECT_SQL = """
insert into score.defects
       (run_id, truth_defect_id, status, matched_exception_id, caught_as)
values (%s, %s, %s, %s, %s)
"""

WRITE_ALARM_SQL = """
insert into score.false_alarms (run_id, exception_id, type, truth_doc_id)
values (%s, %s, %s, %s)
"""
TRUTH_DEFECTS_SQL = """
select
    f.id, f.doc_id, f.defect_type, f.line_no, f.field, f.impact_eur
from truth.defects f
join score.doc_map m on m.truth_doc_id = f.doc_id
where m.run_id = %s
order by f.doc_id, f.line_no nulls first, f.id
"""

# Only what the run raised on its own. A finding marked origin='review'
# exists because a person mapped an unrecognised item and the line was then
# checked again - counting it would credit the system with a catch the
# reviewer made possible. Added 6 September, when the column was created;
# there were no such rows yet, which is why the numbers did not move.
#
# A finding the reviewer rejected still counts: the system did raise it, and
# whether a person agreed is a different question from whether it was found.
OUR_EXCEPTIONS_SQL = """
select
    e.id, m.truth_doc_id, e.type, l.line_no, g.truth_defect, g.is_service
from rec.exceptions e
join rec.flags g on g.flag = e.type
left join doc.invoice_lines l on l.id = e.line_id
left join doc.invoices i on i.id = e.invoice_id
join score.doc_map m
     on m.run_id = e.run_id
    and m.document_id = coalesce(e.document_id, i.document_id)
where e.run_id = %s
  and (e.resolved_by is null or e.resolved_by not like 'rule:%%')
  and e.origin = 'run'
  and e.retracted_at is null
order by e.id
"""

AUTO_CLOSED_SQL = """
select m.truth_doc_id, e.type, e.resolved_by
from rec.exceptions e
left join doc.invoices i on i.id = e.invoice_id
join score.doc_map m
     on m.run_id = e.run_id
    and m.document_id = coalesce(e.document_id, i.document_id)
where e.run_id = %s
  and e.resolved_by like 'rule:%%'
  and e.retracted_at is null
order by e.id
"""
RETRACTED_SQL = """
select m.truth_doc_id, e.type
from rec.exceptions e
left join doc.invoices i on i.id = e.invoice_id
join score.doc_map m
     on m.run_id = e.run_id
    and m.document_id = coalesce(e.document_id, i.document_id)
where e.run_id = %s
  and e.retracted_at is not null
order by e.id
"""

def place_matches(defect_line, exception_line):
    """Same place: same document, plus same line when both sides name one."""
    if defect_line is None or exception_line is None:
        return True
    return defect_line == exception_line


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=int, required=True)
    ap.add_argument("--write", action="store_true",
                    help="write the mapping into score.doc_map")
    args = ap.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        sys.exit("DATABASE_URL is not set in this window")

    conn = psycopg2.connect(dsn)
    cur = conn.cursor()

    cur.execute("select current_user")
    role = cur.fetchone()[0]
    print(f"role: {role}")
    if role != "bench_score":
        sys.exit("wrong role: WF-06 must run as bench_score")

    # ---------- part 1: documents ----------

    cur.execute(MAP_SQL, (args.run,))
    rows = cur.fetchall()

    print(f"\nrun {args.run}: {len(rows)} documents\n")
    print(f"{'doc':>5}  {'truth':<10}  {'sha':<10}  filename")
    print("-" * 60)

    mapped = []
    unmapped = 0
    for document_id, sha, filename, truth_doc_id in rows:
        if truth_doc_id is None:
            unmapped += 1
            shown = "-- NONE --"
        else:
            mapped.append((document_id, truth_doc_id))
            shown = truth_doc_id
        print(f"{document_id:>5}  {shown:<10}  {sha[:8]:<10}  {filename}")

    print("-" * 60)
    print(f"mapped: {len(mapped)}   unmapped: {unmapped}")
    if unmapped:
        print("\nWARNING: unmapped documents cannot enter the score.")

    if args.write:
        for document_id, truth_doc_id in mapped:
            cur.execute(WRITE_MAP_SQL, (args.run, document_id, truth_doc_id))
        conn.commit()
        print(f"\nwritten to score.doc_map: {len(mapped)} rows")
    else:
        print("\ndry run: score.doc_map not written.")

    cur.execute("select count(*) from score.doc_map where run_id = %s", (args.run,))
    in_map = cur.fetchone()[0]
    print(f"score.doc_map holds: {in_map} rows for run {args.run}")
    if in_map == 0:
        print("\nSTOP: nothing to score against. Run with --write first.")
        conn.close()
        return

    # ---------- part 2: defects and false alarms ----------

    cur.execute(TRUTH_DEFECTS_SQL, (args.run,))
    truth_defects = cur.fetchall()

    cur.execute(OUR_EXCEPTIONS_SQL, (args.run,))
    ours = cur.fetchall()

    # Closed by a rule, not by a person: these never reached a human, so
    # they are not false alarms -- a false alarm measures work landing on
    # someone's desk. The filter is on the rule prefix, not on resolved_by
    # being set at all: WF-05 will fill the same column when a PERSON
    # decides, and that work did happen.
    #
    # They are dropped from defect matching too, on purpose. If a rule ever
    # closes the only signal for a real defect, that defect turns into a
    # miss and the number says so. Auto-closing must not be able to improve
    # the score quietly.
    cur.execute(AUTO_CLOSED_SQL, (args.run,))
    auto_closed = cur.fetchall()
    cur.execute(RETRACTED_SQL, (args.run,))
    retracted = cur.fetchall()
    service = [e for e in ours if e[5]]
    candidates = [e for e in ours if not e[5]]
    used = set()
    results = []

    for defect_id, doc_id, defect_type, line_no, field, impact in truth_defects:
        exact = None
        loose = None
        for exc in candidates:
            exc_id, exc_doc, exc_type, exc_line, exc_truth, _ = exc
            if exc_id in used or exc_doc != doc_id:
                continue
            if not place_matches(line_no, exc_line):
                continue
            if exc_truth == defect_type:
                exact = exc
                break
            if loose is None:
                loose = exc

        hit = exact or loose
        if hit is None:
            results.append((defect_id, doc_id, defect_type, line_no,
                            "missed", None, None))
        else:
            used.add(hit[0])
            status = "caught" if hit is exact else "partial"
            results.append((defect_id, doc_id, defect_type, line_no,
                            status, hit[0], hit[2]))

    print(f"\n\nreference defects in these documents: {len(results)}\n")
    print(f"{'truth':<10}  {'line':>4}  {'expected':<18}  {'status':<8}  caught_as")
    print("-" * 72)
    for _, doc_id, defect_type, line_no, status, exc_id, caught_as in results:
        line_txt = "-" if line_no is None else str(line_no)
        caught_txt = "-" if caught_as is None else f"{caught_as} (exc {exc_id})"
        print(f"{doc_id:<10}  {line_txt:>4}  {defect_type:<18}  {status:<8}  {caught_txt}")

    false_alarms = [e for e in candidates if e[0] not in used]

    print(f"\n\nfalse alarms: {len(false_alarms)}"
          f"   (service flags excluded: {len(service)})\n")
    if false_alarms:
        print(f"{'exc':>5}  {'truth':<10}  {'line':>4}  flag")
        print("-" * 46)
        for exc_id, exc_doc, exc_type, exc_line, _, _ in false_alarms:
            line_txt = "-" if exc_line is None else str(exc_line)
            print(f"{exc_id:>5}  {exc_doc:<10}  {line_txt:>4}  {exc_type}")

    caught = sum(1 for r in results if r[4] == "caught")
    partial = sum(1 for r in results if r[4] == "partial")
    missed = sum(1 for r in results if r[4] == "missed")

    print("\n\nsummary")
    print("-" * 46)
    print(f"defects in scope : {len(results)}")
    print(f"  caught         : {caught}")
    print(f"  partial        : {partial}")
    print(f"  missed         : {missed}")
    print(f"false alarms     : {len(false_alarms)}")
    print(f"  auto-closed    : {len(auto_closed)}  (not counted above)")
    print(f"  retracted      : {len(retracted)}  (not counted above)")
    print(f"service flags    : {len(service)}")
    if not args.write:
        print("\ndry run: score.defects and score.false_alarms not written.")
        conn.close()
        return
    cur.execute("delete from score.summary where run_id = %s", (args.run,))
    cur.execute(CLEAR_SQL, (args.run, args.run))

    for defect_id, _, _, _, status, exc_id, caught_as in results:
        cur.execute(WRITE_DEFECT_SQL,
                    (args.run, defect_id, status, exc_id, caught_as))
    for exc_id, exc_doc, exc_type, _, _, _ in false_alarms:
        cur.execute(WRITE_ALARM_SQL, (args.run, exc_id, exc_type, exc_doc))
    conn.commit()

    cur.execute("select count(*) from score.defects where run_id = %s", (args.run,))
    print(f"\nscore.defects holds      : {cur.fetchone()[0]} rows")
    cur.execute("select count(*) from score.false_alarms where run_id = %s", (args.run,))
    print(f"score.false_alarms holds : {cur.fetchone()[0]} rows")
    
    cur.execute(SEGMENTS_SQL, (args.run,))
    segments = {}
    for truth_doc_id, layout, file_kind in cur.fetchall():
        names = ["all", f"layout_{layout}"]
        kind = FILE_KIND_NAMES.get(file_kind)
        if kind:
            names.append(kind)
        else:
            print(f"WARNING: unknown file_kind {file_kind!r}")
        for name in names:
            segments.setdefault(name, set()).add(truth_doc_id)

    defect_doc = {r[0]: r[1] for r in
                  [(d[0], d[1]) for d in truth_defects]}

    print("\n\nsummary by segment")
    print("-" * 66)
    print(f"{'segment':<12}  {'total':>5}  {'caught':>6}  {'partial':>7}"
          f"  {'missed':>6}  {'alarms':>6}")
    print("-" * 66)

    for name in sorted(segments):
        docs = segments[name]
        rows = [r for r in results if r[1] in docs]
        alarms = [a for a in false_alarms if a[1] in docs]
        closed_here = [a for a in auto_closed if a[0] in docs]
        retracted_here = [r for r in retracted if r[0] in docs]
        counts = {
            "defects_total": len(rows),
            "defects_caught": sum(1 for r in rows if r[4] == "caught"),
            "defects_partial": sum(1 for r in rows if r[4] == "partial"),
            "defects_missed": sum(1 for r in rows if r[4] == "missed"),
            "false_alarms": len(alarms),
            "auto_closed": len(closed_here),
            "retracted": len(retracted_here),
        }
        print(f"{name:<12}  {counts['defects_total']:>5}"
              f"  {counts['defects_caught']:>6}  {counts['defects_partial']:>7}"
              f"  {counts['defects_missed']:>6}  {counts['false_alarms']:>6}")
        for metric, value in counts.items():
            cur.execute(WRITE_SUMMARY_SQL, (args.run, metric, name, value))

    conn.commit()
    cur.execute("select count(*) from score.summary where run_id = %s", (args.run,))
    print(f"\nscore.summary holds      : {cur.fetchone()[0]} rows")
    
    cur.execute("delete from score.fields where run_id = %s", (args.run,))
    cur.execute(FIELDS_SQL, (args.run,))

    names = ["invoice_number", "supplier", "venue",
             "received_date", "line_count", "total_eur"]
    field_hits = {n: [0, 0] for n in names}

    for row in cur.fetchall():
        doc = row[0]
        truth_vals = row[1:7]
        our_vals = row[7:13]
        for name, tv, ov in zip(names, truth_vals, our_vals):
            if tv is None or ov is None:
                same = False
            elif name == "total_eur":
                same = abs(float(tv) - float(ov)) <= 0.01
            else:
                same = str(tv).strip() == str(ov).strip()
            field_hits[name][0] += 1
            field_hits[name][1] += 1 if same else 0
            cur.execute(WRITE_FIELD_SQL,
                        (args.run, doc, name, str(tv), str(ov), same))

    conn.commit()

    print("\n\nextraction accuracy by field")
    print("-" * 46)
    print(f"{'field':<16}  {'checked':>7}  {'match':>5}  {'wrong':>5}")
    print("-" * 46)
    for name in names:
        total, hits = field_hits[name]
        print(f"{name:<16}  {total:>7}  {hits:>5}  {total - hits:>5}")

    cur.execute("select count(*) from score.fields where run_id = %s", (args.run,))
    print(f"\nscore.fields holds       : {cur.fetchone()[0]} rows")

    conn.close()


if __name__ == "__main__":
    main()