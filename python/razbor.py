"""razbor.py - WF-05, the invoice card.

One invoice at a time: the document on the left, what the system found on
the right, and the reviewer's decision on each finding.

This is the client's screen, not the bench's. Nothing here mentions runs,
ground truth or defects: the reviewer sees invoices and discrepancies. The
role it runs under cannot reach truth or score at all, so the rule is kept
by the database rather than by care.

Findings are grouped by line of the invoice because that is how a person
reads a document. One line can carry two findings - a total that does not
add up and a price above contract are often the same mistake seen twice -
and putting them together is what stops the reviewer from deciding the same
thing twice.

Decisions are stored per finding even so. rec.exceptions is one row per
finding and the score counts findings; collapsing them in the database
would lose what is being measured. The checkboxes exist to let one click
settle several rows, not to merge them.

Every decision lands in three places:
    rec.exceptions      the finding's current state
    rec.decisions_log   the event, insert-only, protected by a trigger
    ref.sku_map         only when the reviewer maps an item (second pass)

Environment, both required in the window this runs in:
    DATABASE_URL   role bench_pipeline
    SUPABASE_KEY   the storage key, same one progon.py uses
    REVIEWER       optional, the name stored with decisions

Run:
    python razbor.py
then open http://127.0.0.1:8000
"""

import html
import json
import os
from datetime import date, timedelta
import sys
import urllib.error
import urllib.request

import psycopg2
from psycopg2.extras import Json

# The reconciliation rules themselves. Imported, not copied: a line the
# reviewer has just mapped has to be judged by the same rules a run would
# use, and two copies of those rules would drift apart within a week.
import sverka
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

PROJECT = "fltszyoexhwblgksbezm"
BUCKET = "kestrel-raw"
STORAGE_URL = f"https://{PROJECT}.supabase.co/storage/v1/object/{BUCKET}/"

# The batch under review. A client installation has one live run; here it is
# the measured one. Nothing on the screen says so.
RUN_ID = 5

# The six words rec.exceptions.reason accepts, with what the reviewer reads.
# The database holds the word, the screen holds the sentence: counting needs
# the first, a person needs the second.
REASONS = [
    ("misread", "Read incorrectly - the system took the wrong value"),
    ("stale_contract", "Price is right, our contract is out of date"),
    ("agreed_separately", "Agreed separately with the supplier"),
    ("within_tolerance", "Difference is too small to pursue"),
    ("already_counted", "Same problem, already decided on another finding"),
    ("other", "Other - explain below"),
]
REASON_WORDS = {word for word, _ in REASONS}

# What the reviewer sees instead of the three status words in rec.doc_checks.
STATUS_TEXT = {
    "AUTO_APPROVED": ("Checked, nothing found", "ok"),
    "NEEDS_REVIEW": ("Needs your attention", "warn"),
    "BLOCKED": ("Do not pay", "stop"),
}

app = FastAPI()


def require(name):
    value = os.environ.get(name)
    if not value:
        sys.exit(f"{name} is not set in this window")
    return value


DATABASE_URL = require("DATABASE_URL")
SUPABASE_KEY = require("SUPABASE_KEY")
REVIEWER = os.environ.get("REVIEWER", "reviewer")


def connect():
    return psycopg2.connect(DATABASE_URL)


# ---------------------------------------------------------------- storage

def fetch_pdf(storage_path, key):
    """Download one PDF from Supabase Storage.

    Both headers are needed. Copied from progon.py rather than rewritten.
    """
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


# ---------------------------------------------------------------- reading

INVOICE_SQL = """
select i.id, i.invoice_number, s.name, v.name, i.invoice_date,
       i.total, d.storage_path, dc.status, i.supplier_code,
       dc.duplicate_of, orig.invoice_number
from doc.invoices i
join doc.documents d on d.id = i.document_id
left join ref.suppliers s on s.supplier_code = i.supplier_code
left join ref.venues v on v.venue_code = i.venue_code
left join rec.doc_checks dc on dc.invoice_id = i.id and dc.run_id = %s
left join doc.invoices orig on orig.id = dc.duplicate_of
where i.id = %s and i.run_id = %s
"""

FIRST_OPEN_SQL = """
select i.id
from doc.invoices i
join rec.exceptions e on e.invoice_id = i.id and e.run_id = %s
where i.run_id = %s and e.resolved_at is null
  and e.retracted_at is null
group by i.id
order by count(*) desc, i.id
limit 1
"""

FINDINGS_SQL = """
select e.id, l.line_no, l.raw_description, e.type, e.severity,
       e.impact_eur, e.message,
       e.resolution, e.resolved_by, e.reason, e.resolved_at, e.note,
       e.line_id, e.origin
from rec.exceptions e
left join doc.invoice_lines l on l.id = e.line_id
where e.invoice_id = %s and e.run_id = %s
  and e.retracted_at is null
order by l.line_no nulls first, e.id
"""


def load_invoice(cur, invoice_id):
    cur.execute(INVOICE_SQL, (RUN_ID, invoice_id, RUN_ID))
    row = cur.fetchone()
    if row is None:
        raise HTTPException(404, "no such invoice")
    return row


def first_open_invoice(cur):
    # Unused since the list became the front door. Kept: the question
    # "which invoice should I open first" will come back.
    cur.execute(FIRST_OPEN_SQL, (RUN_ID, RUN_ID))
    row = cur.fetchone()
    if row is None:
        raise HTTPException(404, "nothing left to review")
    return row[0]


def load_findings(cur, invoice_id):
    cur.execute(FINDINGS_SQL, (invoice_id, RUN_ID))
    return cur.fetchall()


LIST_SQL = """
select i.id, i.invoice_number, s.name, v.name,
       date(d.received_at) as arrived, i.total,
       coalesce(dc.status, 'UNCHECKED') as status,
       count(e.id) filter (where e.resolved_at is null) as open_findings,
       count(e.id) as all_findings
from doc.documents d
join doc.invoices i on i.document_id = d.id and i.run_id = %s
left join rec.doc_checks dc on dc.invoice_id = i.id and dc.run_id = %s
left join rec.exceptions e on e.invoice_id = i.id and e.run_id = %s
     and e.retracted_at is null
left join ref.suppliers s on s.supplier_code = i.supplier_code
left join ref.venues v on v.venue_code = i.venue_code
where date(d.received_at) between %s and %s
group by i.id, i.invoice_number, s.name, v.name, d.received_at,
         i.total, dc.status
order by d.received_at desc, i.invoice_number
"""


def default_period(cur):
    """The last seven days that actually have post in them.

    Not "today": in this batch the newest invoice arrived on 30 April, and a
    screen that opens empty reads as a broken screen rather than a quiet day.
    Not the whole month either - 258 invoices at once is the opposite of the
    one-to-ten-a-day the client will see.

    Seven days rather than one because a bookkeeper who was away for two days
    should not have to go looking for what they missed.
    """
    cur.execute("""
        select max(date(received_at))
        from doc.documents d
        join doc.invoices i on i.document_id = d.id and i.run_id = %s
    """, (RUN_ID,))
    last = cur.fetchone()[0]
    if last is None:
        today = date.today()
        return today, today
    return last - timedelta(days=6), last


def load_list(cur, since, until):
    cur.execute(LIST_SQL, (RUN_ID, RUN_ID, RUN_ID, since, until))
    return cur.fetchall()


def load_catalogue_of(cur, supplier_code):
    """What this supplier sells, for the reviewer to choose from.

    One supplier's list, never the whole catalogue: an invoice belongs to one
    supplier, and offering the other eleven would only invite a wrong pick.
    Measured on 6 September: 104 items across 12 suppliers, 9.3 each on
    average, 13 at most. A client's list will be longer, which is why the
    control types ahead rather than dropping down.
    """
    cur.execute("""
        select sku, description, unit
        from ref.catalog_items
        where supplier_code = %s
        order by description
    """, (supplier_code,))
    return cur.fetchall()


# ---------------------------------------------------------------- writing

class Decision(BaseModel):
    exception_ids: list[int]
    action: str                 # confirm | reject
    reason: str | None = None
    note: str | None = None


def resolution_text(action, reason):
    """The sentence stored next to the machine-readable mark.

    rec.exceptions.resolution is read by people; resolved_by and reason are
    read by code. The rule auto-closures already work this way, and mixing
    the two would mean splitting strings on a separator later.
    """
    # Every sentence written here begins with the decision word -
    # confirmed, rejected, mapped - because the card reads that word back to
    # draw the badge. Nothing else in the schema records which it was.
    if action == "confirm":
        return "confirmed by the reviewer: the finding stands"
    for word, sentence in REASONS:
        if word == reason:
            return f"rejected by the reviewer: {sentence.lower()}"
    return "rejected by the reviewer"


@app.post("/decide")
def decide(decision: Decision):
    if decision.action not in ("confirm", "reject"):
        raise HTTPException(400, "action must be confirm or reject")
    if not decision.exception_ids:
        raise HTTPException(400, "nothing selected")

    reason = None
    note = decision.note.strip() if decision.note else None
    if decision.action == "reject":
        reason = decision.reason
        if reason not in REASON_WORDS:
            raise HTTPException(400, "a reason is required to reject")
        # "Other" means the list did not have it, so the words have to come
        # from the person. Without them the row says nothing at all.
        if reason == "other" and not note:
            raise HTTPException(400, "'Other' needs a few words")

    actor = f"human:{REVIEWER}"
    resolution = resolution_text(decision.action, reason)
    written = []

    conn = connect()
    try:
        with conn.cursor() as cur:
            for exception_id in decision.exception_ids:
                # What the row looked like before. The log keeps it so that a
                # decision can be read months later without the row it changed.
                cur.execute(
                    """
                    select run_id, type, resolution, resolved_by, reason, note
                    from rec.exceptions
                    where id = %s and run_id = %s
                    """,
                    (exception_id, RUN_ID),
                )
                row = cur.fetchone()
                if row is None:
                    raise HTTPException(404, f"no finding {exception_id}")
                run_id, kind, was_resolution, was_by, was_reason, was_note = row

                # A finding closed by a rule is not re-decided quietly. The
                # 28 auto-closures of run 5 carry a rule name in resolved_by;
                # overwriting one would erase why it closed.
                if was_by and was_by.startswith("rule:"):
                    raise HTTPException(
                        409,
                        f"finding {exception_id} was closed by a rule")

                cur.execute(
                    """
                    update rec.exceptions
                       set resolution = %s,
                           resolved_by = %s,
                           reason = %s,
                           note = %s,
                           resolved_at = now()
                     where id = %s and run_id = %s
                    """,
                    (resolution, actor, reason, note, exception_id, RUN_ID),
                )

                cur.execute(
                    """
                    insert into rec.decisions_log
                        (run_id, entity_type, entity_id, action, actor,
                         before, after)
                    values (%s, 'exception', %s, %s, %s, %s, %s)
                    """,
                    (run_id, str(exception_id), decision.action, actor,
                     Json({"resolution": was_resolution,
                           "resolved_by": was_by,
                           "reason": was_reason,
                           "note": was_note,
                           "type": kind}),
                     Json({"resolution": resolution,
                           "resolved_by": actor,
                           "reason": reason,
                           "note": note,
                           "type": kind})),
                )
                written.append(exception_id)

        # One commit for the whole click. Half a decision stored is worse
        # than none: the log would disagree with the row it describes.
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return JSONResponse({"decided": written})


# ---------------------------------------------------------------- pages

class Mapping(BaseModel):
    exception_id: int
    sku: str


@app.post("/map")
def map_item(mapping: Mapping):
    """The reviewer says what an unrecognised line actually is.

    Three things happen, all or none:

      1. the decision is stored in ref.sku_map, keyed by the description,
         because layouts B and C print no vendor code and every unmatched
         line of run 5 came from one of them;
      2. the finding is closed as mapped - not confirmed, not rejected: the
         system was right that it could not tell, and the person supplied
         what was missing;
      3. the line is checked again, by the run's own rules, and whatever
         that turns up is raised as a new finding marked origin='review'.

    Point 3 is the reason mapping lives on this screen rather than a
    separate one. The reviewer has the document open and has just told the
    system what the line is; if that reveals an overcharge, this is the
    moment to see it.
    """
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                select e.line_id, e.invoice_id, e.type, e.resolved_at,
                       l.raw_description, i.supplier_code
                from rec.exceptions e
                join doc.invoice_lines l on l.id = e.line_id
                join doc.invoices i on i.id = e.invoice_id
                where e.id = %s and e.run_id = %s
            """, (mapping.exception_id, RUN_ID))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, "no such finding, or it has no line")
            line_id, invoice_id, kind, resolved_at, description, supplier = row

            if kind != "SKU_UNMATCHED":
                raise HTTPException(400, "only unmatched items are mapped")
            # ref.sku_map has no UPDATE for this role, so a mapping written
            # here cannot be taken back from this screen.
            if resolved_at is not None:
                raise HTTPException(409, "this finding is already settled")

            cur.execute(
                "select 1 from ref.catalog_items"
                " where sku = %s and supplier_code = %s",
                (mapping.sku, supplier))
            if cur.fetchone() is None:
                raise HTTPException(
                    400, "that item does not belong to this supplier")

            # The map is keyed by supplier and description, not by finding:
            # sku_map_by_text is unique on (supplier_code,
            # lower(vendor_description)). So the same item can already be
            # mapped from an earlier invoice while this finding is still
            # open, and a plain insert fails with 500 - which is what the
            # reviewer met on NB-2026-04-0277.
            #
            # The bar stays in the database. This only asks it beforehand,
            # so that a person reads a sentence instead of an error number.
            # Two callers at once would still collide, and would still be
            # refused by the index; there is one reviewer and one process.
            cur.execute("""
                select sku from ref.sku_map
                 where supplier_code = %s
                   and lower(vendor_description) = lower(%s)
            """, (supplier, description))
            already = cur.fetchone()

            actor = f"human:{REVIEWER}"

            if already is None:
                cur.execute("""
                    insert into ref.sku_map
                        (supplier_code, vendor_sku, vendor_description, sku,
                         match_method, confidence, created_by, run_id)
                    values (%s, null, %s, %s, 'manual', 1.0, %s, %s)
                """, (supplier, description, mapping.sku, actor, RUN_ID))
                resolution = (f"mapped by the reviewer to {mapping.sku}"
                              f" and the line checked again")
            elif already[0] == mapping.sku:
                # Nothing to write - the map already says this. The finding
                # still has to be closed and the line still has to be checked
                # again: that work belongs to this invoice, not to the one
                # where the item was first named.
                resolution = (f"mapped by the reviewer to {mapping.sku}"
                              f" - the same item was mapped on an earlier"
                              f" invoice, and the line checked again")
            else:
                raise HTTPException(
                    409,
                    f"this supplier's \"{description}\" is already mapped"
                    f" to {already[0]}. Changing a mapping is not possible"
                    f" from this screen.")
            cur.execute("""
                update rec.exceptions
                   set resolution = %s, resolved_by = %s, resolved_at = now()
                 where id = %s and run_id = %s
            """, (resolution, actor, mapping.exception_id, RUN_ID))

            cur.execute("""
                insert into rec.decisions_log
                    (run_id, entity_type, entity_id, action, actor,
                     before, after)
                values (%s, 'exception', %s, 'map', %s, %s, %s)
            """, (RUN_ID, str(mapping.exception_id), actor,
                  Json({"type": kind, "description": description,
                        "resolution": None}),
                  Json({"type": kind, "description": description,
                        "sku": mapping.sku, "resolution": resolution})))

            found = recheck_line(cur, invoice_id, line_id)

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return JSONResponse({"mapped": mapping.sku, "new_findings": found})


def recheck_line(cur, invoice_id, line_id):
    """Run the reconciliation rules over one line and store what they say.

    The catalogue and the map are loaded here rather than at start-up: the
    row just written to ref.sku_map has to be visible, and it is only
    visible to a query made after it.

    New findings are marked origin='review'. They belong to run 5 - same
    invoice, same rules - but the score counts what the system found
    unaided, so the two have to stay tellable apart.

    They do not survive the next `sverka.py --write`, which clears and
    rewrites WF-04's findings. That is right rather than wrong: by then the
    mapping is in ref.sku_map, the line matches on its own, and the run
    raises the same finding itself with origin='run'.
    """
    tol_abs, tol_pct = sverka.load_settings(cur, RUN_ID)
    by_sku, by_description, by_supplier = sverka.load_catalogue(cur)
    sku_map = sverka.load_sku_map(cur)
    synonyms = sverka.load_unit_synonyms(cur)
    flags = sverka.load_flags(cur)
    mismatched = sverka.load_mismatched_lines(cur, RUN_ID)

    row = None
    for candidate in sverka.read_lines(cur, RUN_ID, invoice_id):
        if candidate[0] == line_id:
            row = candidate
            break
    if row is None:
        return 0

    checked = sverka.check_line(cur, row, by_sku, by_description,
                               by_supplier, sku_map, mismatched,
                               tol_abs, tol_pct, synonyms)

    written = 0
    for flag, impact, message in checked["written"]["found"]:
        if flag in ("MATCH", "SKU_UNMATCHED"):
            continue
        cur.execute("""
            insert into rec.exceptions
                (run_id, invoice_id, line_id, type, severity,
                 impact_eur, message, origin)
            values (%s, %s, %s, %s, %s, %s, %s, 'review')
        """, (RUN_ID, invoice_id, line_id, flag,
              flags.get(flag, {}).get("severity", "medium"),
              impact, message))
        written += 1
    return written


@app.get("/pdf/{invoice_id}")
def pdf(invoice_id: int):
    """The browser cannot reach the bucket: it is private and the two headers
    cannot travel in an iframe. So the page asks us and we fetch."""
    with connect() as conn, conn.cursor() as cur:
        row = load_invoice(cur, invoice_id)
    storage_path = row[6]
    if not storage_path:
        raise HTTPException(404, "this document has no file")
    try:
        data = fetch_pdf(storage_path, SUPABASE_KEY)
    except RuntimeError as error:
        raise HTTPException(502, str(error))
    # The file in the bucket is named INV-0056_AB26-04-0899.pdf, and the
    # INV- prefix is the ground-truth journal's own numbering. A reviewer who
    # sees it can tell which documents were seeded with a defect, which would
    # make any measurement of the review step meaningless. The name sent to
    # the browser is built from the invoice number instead.
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition":
                 'inline; filename="' + str(row[1]) + '.pdf"'},
    )


def finding_block(row, catalogue):
    (exception_id, line_no, description, kind, severity,
     impact, message, resolution, resolved_by, reason,
     resolved_at, note, line_id, origin) = row

    money = ""
    if impact is not None:
        money = f'<span class="money">{impact} EUR</span>'

    if resolved_at is None:
        state = f'<input type="checkbox" class="pick" value="{exception_id}">'
        settled = ""
    elif resolved_by and resolved_by.startswith("rule:"):
        state = '<span class="mark rule">closed automatically</span>'
        settled = f'<div class="was">{html.escape(resolution or "")}</div>'
    else:
        # The decision word is the first word of the sentence we stored.
        first = (resolution or "").split(" ", 1)[0]
        done = first if first in ("confirmed", "rejected", "mapped") \
            else "rejected"
        if done == "mapped":
            # A mapping cannot be taken back from here: the row it wrote to
            # ref.sku_map stays, and this role can neither update nor delete
            # it. Offering to change the decision would make the screen say
            # something the database does not.
            state = f'<span class="mark {done}">{done}</span>'
        else:
            # Anyone can decide wrongly, so a decision is not a one-way door.
            # The link only uncovers the checkbox - nothing is written until
            # Confirm or Reject is pressed, so leaving the page half-way
            # leaves the finding exactly as it was.
            state = (f'<span class="settled">'
                     f'<span class="mark {done}">{done}</span>'
                     f'<a href="#" class="change">change</a></span>'
                     f'<input type="checkbox" class="pick"'
                     f' value="{exception_id}" hidden>')
        extra = f" &mdash; {html.escape(note)}" if note else ""
        settled = (f'<div class="was">{html.escape(resolution or "")}'
                   f'{extra}</div>')

    # Only an unmatched line can be mapped, and only while it is open. The
    # control is a text box with suggestions rather than a drop-down: this
    # supplier has seven items, a client's may have a thousand, and the same
    # control has to work for both.
    mapper = ""
    if kind == "SKU_UNMATCHED" and resolved_at is None and line_id:
        options = "".join(
            f'<option value="{html.escape(sku)}">'
            f'{html.escape(text)} ({html.escape(unit or "")})</option>'
            for sku, text, unit in catalogue)
        mapper = (
            f'<div class="mapper">'
            f'<select class="sku">'
            f'<option value="">This item is...</option>{options}</select>'
            f'<button class="mapbtn" data-id="{exception_id}">Map</button>'
            f'<span class="hint">or reject it if the catalogue has no '
            f'such item</span></div>')

    # A finding the run raised and one the reviewer's mapping revealed read
    # the same; saying which is which keeps the screen honest.
    from_review = ('<span class="mark review">after mapping</span>'
                   if origin == "review" else "")

    return (
        f'<div class="finding" data-id="{exception_id}">'
        f'<div class="row1">{state}'
        f'<span class="kind">{html.escape(kind)}</span>{from_review}'
        f'{money}</div>'
        f'<div class="msg">{html.escape(message or "")}</div>'
        f'{mapper}{settled}</div>'
    )


@app.get("/", response_class=HTMLResponse)
def home(since: str | None = None, until: str | None = None):
    """The day's post: every invoice that arrived, not only the bad ones.

    Showing only problems would mean an empty screen says two different
    things - nothing went wrong, and nothing arrived at all - with no way to
    tell them apart. An invoice lost on the way in would vanish silently.

    Dated by when the message arrived, not by the date printed on the
    invoice: the bookkeeper's day starts with what landed in the inbox.
    """
    with connect() as conn, conn.cursor() as cur:
        if since and until:
            try:
                first = date.fromisoformat(since)
                last = date.fromisoformat(until)
            except ValueError:
                raise HTTPException(400, "dates must be yyyy-mm-dd")
        else:
            first, last = default_period(cur)
        rows = load_list(cur, first, last)

    lines = []
    seen_day = None
    for (invoice_id, number, supplier, venue, arrived, total,
         status, open_findings, all_findings) in rows:
        if arrived != seen_day:
            seen_day = arrived
            lines.append(f'<h3 class="day">{arrived}</h3>')

        text, css = STATUS_TEXT.get(status, ("Not checked yet", "warn"))
        if open_findings:
            note = (f'{open_findings} to look at'
                    if open_findings > 1 else '1 to look at')
        elif all_findings:
            # The reconciliation verdict does not change when a person
            # settles the findings - it describes what the run saw. But a
            # row that still shouts "needs your attention" after the work is
            # done sends the reviewer back into an invoice they have already
            # finished, so the badge follows the queue rather than the run.
            note = "all settled"
            text, css = "Reviewed", "done"
        else:
            note = ""

        lines.append(
            f'<a class="line" href="/invoice/{invoice_id}'
            f'?since={first}&until={last}">'
            f'<span class="num">{html.escape(number or "")}</span>'
            f'<span class="sup">{html.escape(supplier or "")}</span>'
            f'<span class="ven">{html.escape(venue or "")}</span>'
            f'<span class="tot">{total} EUR</span>'
            f'<span class="status {css}">{text}</span>'
            f'<span class="note">{note}</span></a>')

    if not rows:
        lines.append('<p class="none">Nothing arrived in this period.</p>')

    return LIST_PAGE.format(
        since=first, until=last,
        count=len(rows),
        lines="".join(lines),
    )


LIST_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Invoices</title>
<style>
 body {{ font: 15px/1.55 system-ui, sans-serif; margin: 0; color: #1a1a1a;
        background: #fafafa; }}
 .wrap {{ max-width: 1000px; margin: 0 auto; padding: 28px 24px 60px; }}
 h1 {{ font-size: 21px; margin: 0 0 4px; }}
 .sub {{ color: #666; margin-bottom: 20px; }}
 .period {{ display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
            margin-bottom: 24px; }}
 input[type=date] {{ font: inherit; padding: 6px; }}
 button {{ font: inherit; padding: 7px 14px; border: 1px solid #bbb;
           border-radius: 4px; background: #fff; cursor: pointer; }}
 .day {{ font-size: 13px; color: #777; font-weight: 600; margin: 22px 0 8px;
         letter-spacing: .03em; }}
 .line {{ display: flex; gap: 14px; align-items: center; padding: 10px 14px;
          background: #fff; border: 1px solid #e4e4e4; border-radius: 4px;
          margin-bottom: 6px; text-decoration: none; color: inherit; }}
 .line:hover {{ border-color: #bbb; }}
 .num {{ width: 150px; font-weight: 600; }}
 .sup {{ width: 200px; }}
 .ven {{ width: 170px; color: #666; font-size: 13px; }}
 .tot {{ width: 100px; text-align: right; }}
 .status {{ margin-left: auto; padding: 3px 10px; border-radius: 3px;
            font-size: 12px; font-weight: 600; white-space: nowrap; }}
 .ok {{ background: #e6f4ea; color: #1e6b34; }}
 .warn {{ background: #fdf1dc; color: #8a5a00; }}
 .stop {{ background: #fce8e6; color: #a01b0b; }}
 .done {{ background: #eef0f2; color: #4a5259; }}
 .note {{ width: 110px; text-align: right; font-size: 12px; color: #777; }}
 .none {{ color: #666; }}
</style></head>
<body><div class="wrap">
 <h1>Invoices</h1>
 <div class="sub">{count} arrived between {since} and {until}</div>
 <div class="period">
  <input type="date" id="since" value="{since}">
  <input type="date" id="until" value="{until}">
  <button id="show">Show</button>
 </div>
 {lines}
</div>
<script>
document.getElementById('show').onclick = () => {{
  const a = document.getElementById('since').value;
  const b = document.getElementById('until').value;
  if (a && b) location.href = '/?since=' + a + '&until=' + b;
}};
</script>
</body></html>"""


@app.get("/invoice/{invoice_id}", response_class=HTMLResponse)
def card(invoice_id: int, since: str | None = None,
         until: str | None = None):
    """One invoice, with the document beside the findings.

    since and until are carried through untouched so that "All invoices"
    goes back to the day the reviewer was working on. Without them the link
    landed on the default period, and someone working through April had to
    retype the dates after every single invoice.
    """
    with connect() as conn, conn.cursor() as cur:
        row = load_invoice(cur, invoice_id)
        rows = load_findings(cur, invoice_id)
        catalogue = load_catalogue_of(cur, row[8])

    (number, supplier, venue, date, total, _path, status, _code,
     duplicate_of, duplicate_number) = row[1:]

    # The finding says "repeats invoice id 63" - a database key, which tells
    # the reviewer nothing and cannot be looked up from this screen. The
    # invoice it repeats is named here instead, with a way to open it.
    repeats = ""
    if duplicate_of and duplicate_number:
        repeats = (
            f'<div class="repeats">Repeats '
            f'<a href="/invoice/{duplicate_of}">'
            f'{html.escape(duplicate_number)}</a>'
            f' &mdash; already received</div>')
    status_text, status_class = STATUS_TEXT.get(
        status, ("Not checked yet", "warn"))

    blocks = []
    current = object()
    for finding in rows:
        line_no = finding[1]
        if line_no != current:
            current = line_no
            heading = ("The document as a whole" if line_no is None
                       else f"Line {line_no} &mdash; "
                            f"{html.escape(finding[2] or '')}")
            blocks.append(f'<h3>{heading}</h3>')
        blocks.append(finding_block(finding, catalogue))

    if not blocks:
        blocks.append('<p class="none">No discrepancies on this invoice.</p>')

    options = "".join(
        f'<option value="{word}">{html.escape(sentence)}</option>'
        for word, sentence in REASONS)

    # Built once, in one place. The way back and the reload after a decision
    # are the same period seen from two sides, and computing it twice is how
    # they would come to disagree.
    period = ""
    back = "/"
    if since and until:
        period = f"?since={since}&until={until}"
        back = "/" + period

    return HTML_PAGE.format(
        invoice_id=invoice_id,
        period=period,
        back=back,
        number=html.escape(number or ""),
        supplier=html.escape(supplier or ""),
        venue=html.escape(venue or ""),
        date=date,
        total=total,
        status_text=status_text,
        status_class=status_class,
        repeats=repeats,
        findings="".join(blocks),
        reason_options=options,
    )


HTML_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>{number}</title>
<style>
 body {{ font: 15px/1.55 system-ui, sans-serif; margin: 0; color: #1a1a1a; }}
 .split {{ display: flex; height: 100vh; }}
 .left {{ width: 52%; border-right: 1px solid #d8d8d8; }}
 .right {{ width: 48%; overflow-y: auto; padding: 24px 28px 0;
           box-sizing: border-box; }}
 .spacer {{ height: 180px; }}
 iframe {{ width: 100%; height: 100%; border: 0; }}
 .back {{ display: inline-block; margin-bottom: 10px; font-size: 13px;
           color: #555; text-decoration: none; }}
 .back:hover {{ text-decoration: underline; }}
 h2 {{ margin: 0 0 6px; font-size: 20px; }}
 .meta {{ color: #666; }}
 .repeats {{ margin: 8px 0 0; font-size: 13px; }}
 .status {{ display: inline-block; margin: 12px 0 4px; padding: 3px 10px;
            border-radius: 3px; font-size: 13px; font-weight: 600; }}
 .ok {{ background: #e6f4ea; color: #1e6b34; }}
 .warn {{ background: #fdf1dc; color: #8a5a00; }}
 .stop {{ background: #fce8e6; color: #a01b0b; }}
 h3 {{ margin: 26px 0 10px; font-size: 15px; }}
 .finding {{ margin: 0 0 14px; padding: 10px 12px; border: 1px solid #e2e2e2;
             border-radius: 4px; }}
 .row1 {{ display: flex; align-items: center; gap: 8px; }}
 .kind {{ font-weight: 600; font-size: 13px; letter-spacing: .02em; }}
 .money {{ margin-left: auto; font-weight: 600; }}
 .msg {{ margin-top: 6px; color: #333; }}
 .was {{ margin-top: 8px; font-size: 13px; color: #666; }}
 .mark {{ font-size: 12px; font-weight: 600; padding: 2px 8px;
          border-radius: 3px; }}
 .confirmed {{ background: #e6f4ea; color: #1e6b34; }}
 .rejected {{ background: #eee; color: #555; }}
 .mapped {{ background: #e9f1fb; color: #1f4b80; }}
 .rule {{ background: #eef2fb; color: #3a4d80; }}
 .review {{ background: #f2ecf9; color: #5b3d80; }}
 .settled {{ display: inline-flex; align-items: center; }}
 .settled[hidden] {{ display: none; }}
 .change {{ margin-left: 8px; font-size: 12px; color: #666; }}
 .mapper {{ margin-top: 10px; display: flex; gap: 8px; align-items: center;
            flex-wrap: wrap; }}
 .mapper select {{ min-width: 260px; }}
 .hint {{ font-size: 12px; color: #888; }}
 .none {{ color: #666; }}
 .bar {{ position: fixed; bottom: 0; right: 0; width: 48%;
         box-sizing: border-box; padding: 12px 28px; background: #fff;
         border-top: 1px solid #d8d8d8; display: flex; gap: 8px;
         align-items: center; flex-wrap: wrap; }}
 button {{ font: inherit; padding: 7px 14px; border: 1px solid #bbb;
           border-radius: 4px; background: #fff; cursor: pointer; }}
 button:disabled {{ opacity: .45; cursor: default; }}
 select, input[type=text] {{ font: inherit; padding: 6px; }}
 input[type=text] {{ flex: 1 1 100%; }}
 #say {{ flex: 1 1 100%; font-size: 13px; color: #666; }}
</style></head>
<body><div class="split">
 <div class="left"><iframe src="/pdf/{invoice_id}"></iframe></div>
 <div class="right">
  <a class="back" href="{back}">&larr; All invoices</a>
  <h2>{number}</h2>
  <div class="meta">{supplier} &middot; {venue} &middot; {date}
   &middot; {total} EUR</div>
  <div class="status {status_class}">{status_text}</div>
  {repeats}
  {findings}
  <div class="spacer"></div>
 </div>
</div>

<div class="bar">
 <button id="all">Select all open</button>
 <button id="confirm" disabled>Confirm</button>
 <button id="reject" disabled>Reject</button>
 <select id="reason"><option value="">Reason for rejecting...</option>
  {reason_options}</select>
 <input type="text" id="note" placeholder="Note (required for 'Other')">
 <div id="say">Tick the findings you have decided on.</div>
</div>

<script>
const INVOICE_ID = {invoice_id};
// Empty when the card was opened without dates, and then the reload is the
// bare address it was before.
const PERIOD = "{period}";
const picks = () => [...document.querySelectorAll('.pick:checked')]
                      .map(b => Number(b.value));
const say = document.getElementById('say');

function refresh() {{
  const n = picks().length;
  document.getElementById('confirm').disabled = n === 0;
  document.getElementById('reject').disabled = n === 0;
  say.textContent = n === 0
    ? 'Tick the findings you have decided on.'
    : n + ' selected.';
}}

document.addEventListener('change', e => {{
  if (e.target.classList.contains('pick')) refresh();
}});

document.getElementById('all').onclick = () => {{
  // Only the boxes on show. A settled finding has one too, hidden until its
  // 'change' link is used, and "all open" must not sweep those up.
  const boxes = [...document.querySelectorAll('.pick:not([hidden])')];
  const turnOn = boxes.some(b => !b.checked);
  boxes.forEach(b => b.checked = turnOn);
  refresh();
}};

document.addEventListener('click', e => {{
  if (!e.target.classList.contains('change')) return;
  e.preventDefault();
  const finding = e.target.closest('.finding');
  finding.querySelector('.settled').hidden = true;
  const box = finding.querySelector('.pick');
  box.hidden = false;
  box.checked = true;
  refresh();
}});

async function send(action) {{
  const body = {{
    exception_ids: picks(),
    action: action,
    reason: document.getElementById('reason').value || null,
    note: document.getElementById('note').value || null
  }};
  say.textContent = 'Saving...';
  const r = await fetch('/decide', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(body)
  }});
  if (r.ok) {{
    location.href = '/invoice/' + INVOICE_ID + PERIOD;
    return;
  }}
  const problem = await r.json().catch(() => ({{}}));
  say.textContent = 'Not saved: ' + (problem.detail || r.status);
}}

document.getElementById('confirm').onclick = () => send('confirm');
document.getElementById('reject').onclick = () => send('reject');

document.addEventListener('click', async e => {{
  if (!e.target.classList.contains('mapbtn')) return;
  const box = e.target.closest('.mapper').querySelector('.sku');
  if (!box.value) {{ say.textContent = 'Pick an item first.'; return; }}
  say.textContent = 'Saving...';
  const r = await fetch('/map', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{
      exception_id: Number(e.target.dataset.id),
      sku: box.value
    }})
  }});
  if (r.ok) {{
    location.href = '/invoice/' + INVOICE_ID + PERIOD;
    return;
  }}
  const problem = await r.json().catch(() => ({{}}));
  say.textContent = 'Not saved: ' + (problem.detail || r.status);
}});

// The bar is two or three rows tall depending on the window width, so the
// spacer is measured against it rather than guessed.
function clearBar() {{
  const bar = document.querySelector('.bar');
  document.querySelector('.spacer').style.height =
    (bar.offsetHeight + 32) + 'px';
}}
clearBar();
window.addEventListener('resize', clearBar);
</script>
</body></html>"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
