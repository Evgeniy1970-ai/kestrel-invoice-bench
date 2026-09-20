"""
progon.py - WF-02 end to end: PDF in storage, answer from the model, rows in
the database, arithmetic verdict.

Replaces four scripts that were run by hand one after another:
zapis-izvlecheniya.py, razbor-shapki.py, razbor-strok.py, proverka-arifmetiki.py.
izvlechenie.py stays as a bench tool for one-off calls.

Per document:

    storage  ->  model  ->  doc.extractions
                        ->  doc.invoices + doc.invoice_lines   (one transaction)
                        ->  rec.doc_checks + rec.exceptions

Three environment variables, all checked before any work starts:

    DATABASE_URL     role bench_pipeline - no access to schema truth
    OPENAI_API_KEY   the model
    SUPABASE_KEY     storage; BOTH Authorization and apikey headers are needed,
                     a missing apikey returns "Bucket not found" for a bucket
                     that plainly exists

Run, from 04-kod\\python:

    python progon.py --documents 19,20 --dry-run
    python progon.py --documents 19,20
    python progon.py --all-new --kind debug
    python progon.py --run 3 --all-new        # finish an interrupted run

A run is opened by this script and closed by it. Interrupted halfway, restart
with --run <id> --all-new: documents already extracted in that run are skipped,
because doc.extractions has unique (document_id, run_id). That is the whole
resume mechanism - rec.job_queue was deliberately left alone, see 00-opis.md.
"""

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path

import psycopg2
from psycopg2.extras import Json

API_URL = "https://api.openai.com/v1/responses"
PROJECT = "fltszyoexhwblgksbezm"
BUCKET = "kestrel-raw"
STORAGE_URL = f"https://{PROJECT}.supabase.co/storage/v1/object/{BUCKET}/"

# USD per million tokens, checked 30 August 2026. These move - re-check before
# quoting a cost anywhere. Same numbers as izvlechenie.py; if they are edited
# there they must be edited here too.
PRICES = {
    "gpt-5.6-sol":   {"in": 4.00, "out": 20.00},
    "gpt-5.6-terra": {"in": 2.00, "out": 12.00},
    "gpt-5.6-luna":  {"in": 0.20, "out": 1.20},
}

# 8000 was enough for a 30-line invoice (6459 out) but not by much. A truncated
# answer comes back as status "incomplete" with valid-looking JSON missing its
# tail - silent data loss. The status check below is what actually protects us;
# the higher ceiling just makes it rare.
MAX_OUTPUT_TOKENS = 16000

FLAG = "ARITHMETIC_MISMATCH"
SEVERITY = "high"

# rec.doc_checks.status is the document's fate, not the result of one check.
# WF-02 never writes AUTO_APPROVED: arithmetic agreeing with itself says nothing
# about price, discount, unit or VAT assignment - seven of the eight defect
# types are invisible here. WF-04 promotes what it has actually checked.
STATUS = "NEEDS_REVIEW"


# ---------------------------------------------------------------- prompt

def load_prompt(path: Path):
    """System prompt and user message out of the prompt file.

    Same splitting rule as izvlechenie.py: sections separated by lines holding
    only '---', headed '## System prompt' and '## User message'. The prompt file
    is the single source of truth; keeping a copy in the script would drift.
    """
    if not path.exists():
        sys.exit(f"Prompt file not found: {path.resolve()}")

    blocks = path.read_text(encoding="utf-8").split("\n---\n")
    system_text = user_text = None

    for block in blocks:
        stripped = block.strip()
        if stripped.startswith("## System prompt"):
            system_text = stripped[len("## System prompt"):].strip()
        elif stripped.startswith("## User message"):
            user_text = stripped[len("## User message"):].strip()

    if not system_text:
        sys.exit(f"No '## System prompt' section found in {path}")
    if not user_text:
        sys.exit(f"No '## User message' section found in {path}")

    return system_text, user_text


# ---------------------------------------------------------------- storage

def fetch_pdf(storage_path, key):
    """Download one PDF from Supabase Storage."""
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


# ---------------------------------------------------------------- model

def call_model(pdf_bytes, filename, model, detail, system_text, user_text, key):
    """One call. Returns (parsed answer, tokens_in, tokens_out, ms).

    filename comes from doc.documents.source_filename, never from the batch
    file name on disk: that one carries the ground-truth doc_id, and no signal
    other than the defect itself may predict the defect.
    """
    encoded = base64.b64encode(pdf_bytes).decode("ascii")
    body = {
        "model": model,
        "instructions": system_text,
        "input": [{
            "role": "user",
            "content": [
                {
                    "type": "input_file",
                    "filename": filename,
                    "file_data": f"data:application/pdf;base64,{encoded}",
                    "detail": detail,
                },
                {"type": "input_text", "text": user_text},
            ],
        }],
        "max_output_tokens": MAX_OUTPUT_TOKENS,
    }

    request = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        method="POST",
    )

    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        detail_text = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"model HTTP {error.code}: {detail_text}")
    except urllib.error.URLError as error:
        raise RuntimeError(f"model unreachable: {error.reason}")

    elapsed_ms = int((time.time() - started) * 1000)
    response = json.loads(raw)

    # A truncated answer is the dangerous case: the JSON may still parse and
    # simply be missing its last lines. Refuse it outright.
    status = response.get("status")
    if status != "completed":
        raise RuntimeError(f"model returned status '{status}', not 'completed'")

    parts = []
    for item in response.get("output", []):
        for chunk in item.get("content", []):
            if chunk.get("type") == "output_text":
                parts.append(chunk.get("text", ""))
    text = "\n".join(parts).strip()

    if not text:
        raise RuntimeError("model returned no text")
    if text.startswith("```"):
        raise RuntimeError("answer wrapped in a markdown fence, which the "
                           "prompt forbids - not stripping it silently")

    try:
        answer = json.loads(text)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"answer is not valid JSON: {error}")

    usage = response.get("usage", {})
    return (answer,
            usage.get("input_tokens", 0),
            usage.get("output_tokens", 0),
            elapsed_ms)


# ---------------------------------------------------------------- helpers

def to_decimal(value):
    return None if value is None else Decimal(str(value))


def percent_to_fraction(value, field, line_no, warnings):
    """Percent as the model prints it -> fraction as the database stores it.

    discount_pct and vat_rate are numeric(5,4): one digit before the point.
    A VAT rate of 20 does not fit at all and crashes; a discount of 8 fits and
    lands as 8.0000 instead of 0.0800, silently. Loud in some places and silent
    in others is worse than always silent, so the conversion is explicit.
    """
    if value is None:
        return None
    number = Decimal(str(value))
    if number != 0 and abs(number) < 1:
        warnings.append(
            f"line {line_no}: {field} came back as {number}. Below 1, so it may "
            f"already be a fraction. Converted as agreed - check the document.")
    return number / Decimal(100)


def gap(left, right, tolerance):
    if left is None or right is None:
        return None
    difference = left - right
    return difference if abs(difference) > tolerance else None


# ---------------------------------------------------------------- run

def open_run(cur, kind, batch_code, model, prompt_version, note):
    cur.execute(
        """
        insert into rec.runs (kind, batch_code, model, prompt_version,
                              settings_snapshot, note)
        values (%s, %s, %s, %s, rec.settings_snapshot(), %s)
        returning id
        """,
        (kind, batch_code, model, prompt_version, note),
    )
    return cur.fetchone()[0]


def run_settings(cur, run_id):
    cur.execute("select settings_snapshot from rec.runs where id = %s",
                (run_id,))
    row = cur.fetchone()
    if row is None:
        sys.exit(f"No run with id {run_id}.")
    return row[0] or {}


def pick_documents(cur, run_id, explicit, all_new):
    """Which documents this run should process."""
    if explicit:
        cur.execute(
            """
            select d.id, d.source_filename, d.storage_path, d.sender
            from doc.documents d
            where d.id = any(%s)
            order by d.id
            """,
            (explicit,),
        )
        found = cur.fetchall()
        missing = set(explicit) - {row[0] for row in found}
        if missing:
            sys.exit(f"No such documents: {sorted(missing)}")
        return found

    # Everything received that this run has not extracted yet. This is the
    # resume mechanism: unique (document_id, run_id) makes the leftover list
    # a plain query, no job queue needed.
    cur.execute(
        """
        select d.id, d.source_filename, d.storage_path, d.sender
        from doc.documents d
        where d.ingest_status = 'received'
          and not exists (select 1 from doc.extractions e
                           where e.document_id = d.id and e.run_id = %s)
        order by d.id
        """,
        (run_id,),
    )
    return cur.fetchall()


def close_run(conn, run_id, docs_done, tokens_in, tokens_out, cost):
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                update rec.runs
                   set finished_at = now(),
                       docs_total  = %s,
                       tokens_in   = %s,
                       tokens_out  = %s,
                       cost_usd    = %s
                 where id = %s
                """,
                (docs_done, tokens_in, tokens_out, cost, run_id),
            )


def record_failure(conn, run_id, document_id, stage, message):
    """A failed document does not stop the batch - it is written down.

    Goes to rec.workflow_errors, where WF-01's failures already live, rather
    than to a new table. One place to look after a run.
    """
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into rec.workflow_errors
                    (workflow_id, workflow_name, execution_id, failed_node,
                     error_message, run_id, raw_json)
                values (%s, %s, %s, %s, %s, %s, %s)
                """,
                ("progon.py", "WF-02 progon.py", str(run_id), stage,
                 message[:4000], run_id,
                 Json({"document_id": document_id, "stage": stage,
                       "message": message})),
            )


# ---------------------------------------------------------------- one document

def store_extraction(cur, document_id, run_id, model, prompt_version, answer,
                     tokens_in, tokens_out, cost, duration_ms):
    """Raw answer first, parsing after.

    The call already cost money and time. If parsing fails the answer must
    already be in the database, and a later empty field must be tellable apart:
    "the model did not return it" versus "we did not parse it".
    """
    cur.execute(
        """
        insert into doc.extractions
            (document_id, run_id, model, prompt_version, raw_json,
             tokens_in, tokens_out, cost_usd, duration_ms)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (document_id, run_id) do nothing
        returning id
        """,
        (document_id, run_id, model, prompt_version, Json(answer),
         tokens_in, tokens_out, cost, duration_ms),
    )
    row = cur.fetchone()
    return row[0] if row else None


def find_supplier(cur, printed_name, sender):
    """Name first, envelope second. Returns (code, layout, how).

    Name first on purpose: we are measuring how well the document is read, and
    leaning on the envelope first would hide a reading failure. On layout C the
    supplier name is printed in capitals and never matches, so the envelope does
    the work for roughly a third of the batch.
    """
    if printed_name:
        cur.execute("select supplier_code, layout from ref.suppliers "
                    "where name = %s", (printed_name,))
        row = cur.fetchone()
        if row:
            return row[0], row[1], "name"
    if sender:
        cur.execute("select supplier_code, layout from ref.suppliers "
                    "where lower(billing_email) = lower(%s)", (sender,))
        row = cur.fetchone()
        if row:
            return row[0], row[1], "sender"
    return None, None, "not found"


def find_venue(cur, printed_name):
    """Exact name only. ref.venues holds 58 Alder Road and 58a Alder Road one
    letter apart, so the address is a check, never the key."""
    if not printed_name:
        return None, None
    cur.execute("select venue_code, address from ref.venues where name = %s",
                (printed_name,))
    row = cur.fetchone()
    return (row[0], row[1]) if row else (None, None)


def store_invoice_and_lines(cur, extraction_id, document_id, run_id, answer,
                            sender, warnings, notes):
    """Header and lines in one transaction.

    Not for tidiness: an invoice with half its lines makes the arithmetic check
    report a mismatch on a clean document. A false finding is worse for a
    measuring bench than a miss.
    """
    supplier_code, layout, how = find_supplier(
        cur, answer.get("supplier_name"), sender)
    venue_code, ref_address = find_venue(cur, answer.get("venue_name"))

    notes.append(f"supplier {supplier_code or '-'} by {how}, layout "
                 f"{layout or '-'}, venue {venue_code or '-'}")

    printed_address = answer.get("venue_address")
    if venue_code and ref_address and printed_address != ref_address:
        notes.append(f"ADDRESS MISMATCH: invoice '{printed_address}', "
                     f"reference '{ref_address}'")

    cur.execute(
        """
        insert into doc.invoices
            (extraction_id, document_id, run_id, supplier_code, venue_code,
             invoice_number, invoice_date, delivery_date, period_from,
             period_to, waybill_ref, subtotal, vat_amount, total, layout)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (extraction_id) do nothing
        returning id
        """,
        (extraction_id, document_id, run_id, supplier_code, venue_code,
         answer.get("invoice_number"), answer.get("invoice_date"),
         answer.get("delivery_date"), answer.get("period_from"),
         answer.get("period_to"), answer.get("waybill_ref"),
         answer.get("subtotal"), answer.get("vat_amount"),
         answer.get("total"), layout),
    )
    row = cur.fetchone()
    if row is None:
        raise RuntimeError("this extraction already has an invoice")
    invoice_id = row[0]

    lines = answer.get("lines") or []
    if not lines:
        raise RuntimeError("the answer has no lines")

    rows = []
    for line in lines:
        line_no = line.get("line_no")
        rows.append((
            invoice_id, run_id, line_no,
            line.get("raw_description"), line.get("vendor_sku"),
            line.get("quantity"), line.get("unit"), line.get("unit_price"),
            percent_to_fraction(line.get("discount_pct"), "discount_pct",
                                line_no, warnings),
            percent_to_fraction(line.get("vat_rate"), "vat_rate",
                                line_no, warnings),
            line.get("line_net"), line.get("source_text"),
            line.get("page"), line.get("confidence"), line.get("delivery_date"),
        ))

    cur.executemany(
        """
        insert into doc.invoice_lines
            (invoice_id, run_id, line_no, raw_description, vendor_sku,
             quantity, unit, unit_price, discount_pct, vat_rate, line_net,
             source_text, page, confidence, delivery_date)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        rows,
    )
    return invoice_id, len(rows)


def check_arithmetic(cur, invoice_id, run_id, answer, tolerance):
    """Five checks, all against the same tolerance. Returns problems found.

    Lines are read back from doc.invoice_lines - what was actually stored, not
    what the model said, so a botched unit conversion would show up here. The
    VAT breakdown comes from the answer: there is no table for it.

    Each problem is (line_id, message, impact). Impact carries the money gap and
    is filled ONLY for checks 2 and 5 - lines against subtotal, and subtotal plus
    VAT against total. Everything else stays None, for two different reasons:

      - line-level gaps (check 1) are not a loss on their own. The money is lost
        on the price, and WF-04 records that under OVERCHARGE on the same line.
        Filling both would count one overcharge twice.
      - check 3 restates the same gap as check 2 from the VAT side, and check 4
        measures tax whose money effect already lands in check 5. On INV-0143
        checks 2 and 3 both fired on one 34.37 discrepancy; paying impact to
        both would report 68.74.
    """
    cur.execute(
        """
        select id, line_no, quantity, unit_price, discount_pct, line_net
        from doc.invoice_lines
        where invoice_id = %s
        order by line_no
        """,
        (invoice_id,),
    )
    lines = cur.fetchall()

    cur.execute("select subtotal, vat_amount, total from doc.invoices "
                "where id = %s", (invoice_id,))
    subtotal, vat_amount, total = [to_decimal(v) for v in cur.fetchone()]

    problems = []
    running = Decimal(0)

    # 1. every line
    for line_id, line_no, qty, price, discount, net in lines:
        qty, price, net = to_decimal(qty), to_decimal(price), to_decimal(net)
        discount = to_decimal(discount) or Decimal(0)

        if qty is None or price is None or net is None:
            problems.append((line_id, f"Line {line_no} cannot be checked: "
                                      f"a number is missing.", None))
            continue

        # The discount must come off: on layouts B and C unit_price is printed
        # BEFORE it and line_net is computed AFTER it.
        expected = qty * price * (Decimal(1) - discount)
        off = gap(net, expected, tolerance)
        running += net

        if off is not None:
            # Work the price back out of the total: that is the number the sum
            # was actually computed from, and it is more precise than the
            # printed one. On INV-0034 this recovered 1.04901 where the column
            # showed 1.05 - the ground truth holds 1.049.
            divisor = qty * (Decimal(1) - discount)
            implied = ""
            if divisor != 0:
                implied = (f" The total was computed from a unit price of "
                           f"{round(net / divisor, 5)}, not the printed {price}.")
            problems.append((
                line_id,
                f"Line {line_no}: {qty} x {price} less {discount * 100}% gives "
                f"{expected:.2f}, invoice says {net:.2f}.{implied}",
                None))

    # 2. subtotal
    off = gap(subtotal, running, tolerance)
    if off is not None:
        # Carries impact: the subtotal is what the client is billed from, so an
        # inflated one is money, and no price flag will ever name it.
        problems.append((None, f"Lines add up to {running:.2f}, subtotal says "
                               f"{subtotal}.", off))

    # 3 and 4. VAT, from the totals block - the only way that works on layout C,
    # which has no per-line rate at all. Catches an item left out of every base,
    # but not an item put under the wrong rate when the bases still add up.
    breakdown = answer.get("vat_breakdown") or []
    if not breakdown:
        problems.append((None, "No VAT breakdown in the answer, so VAT could "
                               "not be checked."))
    else:
        bases = taxes = Decimal(0)
        for part in breakdown:
            rate = to_decimal(part.get("rate"))
            base = to_decimal(part.get("base"))
            amount = to_decimal(part.get("amount"))
            if rate is None or base is None or amount is None:
                problems.append((None, "A VAT rate line is incomplete.",
                                 None))
                continue
            bases += base
            taxes += amount
            expected = base * rate / Decimal(100)
            if gap(amount, expected, tolerance) is not None:
                problems.append((None, f"VAT at {rate}%: {expected:.2f} "
                                       f"expected on base {base:.2f}, invoice "
                                       f"says {amount:.2f}.", None))

        if gap(subtotal, bases, tolerance) is not None:
            problems.append((None, f"VAT bases add up to {bases:.2f} but the "
                                   f"subtotal is {subtotal} - an item may be "
                                   f"missing from every base.", None))
        if gap(vat_amount, taxes, tolerance) is not None:
            problems.append((None, f"VAT amounts add up to {taxes:.2f} but "
                                   f"vat_amount says {vat_amount}.", None))

    # 5. total
    if subtotal is None or vat_amount is None:
        problems.append((None, "Total could not be checked: subtotal or VAT "
                               "amount is missing.", None))
    else:
        expected = subtotal + vat_amount
        off = gap(total, expected, tolerance)
        if off is not None:
            # Carries impact: the total is the sum the client actually pays.
            problems.append((None, f"Subtotal {subtotal} plus VAT {vat_amount} "
                                   f"gives {expected:.2f}, total says {total}.",
                             off))

    return problems


def store_check(cur, invoice_id, run_id, problems):
    arithmetic_ok = len(problems) == 0
    cur.execute(
        """
        insert into rec.doc_checks (run_id, invoice_id, status, arithmetic_ok,
                                    flags)
        values (%s, %s, %s, %s, %s)
        on conflict (run_id, invoice_id) do nothing
        """,
        (run_id, invoice_id, STATUS, arithmetic_ok,
         [] if arithmetic_ok else [FLAG]),
    )
    for line_id, message, impact in problems:
        # impact_eur is filled only where check_arithmetic put a number there:
        # a gap in the totals. A gap inside one line stays empty - the money is
        # named by the price flag WF-04 raises on the same line, and writing it
        # twice would double the sum. A gap in the totals has no price flag to
        # name it, so if WF-02 leaves it empty nobody ever counts it.
        cur.execute(
            """
            insert into rec.exceptions
                (run_id, invoice_id, line_id, type, severity, message,
                 impact_eur)
            values (%s, %s, %s, %s, %s, %s, %s)
            """,
            (run_id, invoice_id, line_id, FLAG, SEVERITY, message, impact),
        )
    return arithmetic_ok


# ---------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(description="WF-02 end to end")
    parser.add_argument("--documents", help="comma-separated doc.documents.id")
    parser.add_argument("--all-new", action="store_true",
                        help="every received document this run has not "
                             "extracted yet")
    parser.add_argument("--run", type=int,
                        help="continue an existing run instead of opening one")
    parser.add_argument("--kind", default="debug", choices=["debug", "measured"])
    parser.add_argument("--batch", default="april-2026")
    parser.add_argument("--model", default="gpt-5.6-terra")
    parser.add_argument("--detail", default="auto",
                        choices=["auto", "low", "high"])
    parser.add_argument("--prompt",
                        default=str(Path("..") / "prompts" / "extract-v3.md"))
    parser.add_argument("--note", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.documents and not args.all_new:
        sys.exit("Give either --documents or --all-new.")

    explicit = None
    if args.documents:
        try:
            explicit = [int(x) for x in args.documents.split(",")]
        except ValueError:
            sys.exit("--documents wants numbers: --documents 19,20,21")

    # All three checked before anything is opened or spent.
    dsn = os.environ.get("DATABASE_URL")
    api_key = os.environ.get("OPENAI_API_KEY")
    storage_key = os.environ.get("SUPABASE_KEY")
    missing = [name for name, value in
               (("DATABASE_URL", dsn), ("OPENAI_API_KEY", api_key),
                ("SUPABASE_KEY", storage_key)) if not value]
    if missing and not args.dry_run:
        sys.exit("Not set in this window: " + ", ".join(missing))

    prompt_path = Path(args.prompt)
    system_text, user_text = load_prompt(prompt_path)
    prompt_version = prompt_path.stem

    price = PRICES.get(args.model)
    if price is None:
        sys.exit(f"No price on file for model {args.model}.")

    conn = psycopg2.connect(dsn)

    try:
        # --- open or continue the run
        with conn:
            with conn.cursor() as cur:
                if args.run:
                    run_id = args.run
                    settings = run_settings(cur, run_id)
                    opened = False
                elif args.dry_run:
                    run_id = None
                    settings = {}
                    opened = False
                else:
                    run_id = open_run(cur, args.kind, args.batch, args.model,
                                      prompt_version, args.note or None)
                    settings = run_settings(cur, run_id)
                    opened = True

        tolerance = None
        if settings:
            from_run = settings.get("arithmetic_tolerance_eur")
            if from_run is None:
                sys.exit(f"Run {run_id} has no arithmetic_tolerance_eur in its "
                         f"settings snapshot.")
            tolerance = Decimal(str(from_run))

        # --- which documents
        with conn.cursor() as cur:
            documents = pick_documents(cur, run_id or 0, explicit, args.all_new)

        print()
        print(f"  run:        {run_id if run_id else '(dry run, none opened)'}"
              f"{'  (opened now)' if opened else ''}")
        print(f"  model:      {args.model}   prompt: {prompt_version}")
        print(f"  tolerance:  {tolerance if tolerance else '(unknown yet)'}")
        print(f"  documents:  {len(documents)}")
        for doc_id, filename, _, _ in documents:
            print(f"      {doc_id:>4}  {filename}")

        if args.dry_run:
            print("\n  This is the plan. Nothing was written, nothing was sent.")
            return

        if not documents:
            print("\n  Nothing to do.")
            close_run(conn, run_id, 0, 0, 0, 0)
            return

        # --- the work
        done = failed = 0
        total_in = total_out = 0
        total_cost = 0.0
        failures = []

        print()
        for doc_id, filename, storage_path, sender in documents:
            print(f"  [{doc_id}] {filename}")
            stage = "storage"
            try:
                pdf_bytes = fetch_pdf(storage_path, storage_key)

                stage = "model"
                answer, tokens_in, tokens_out, duration_ms = call_model(
                    pdf_bytes, filename, args.model, args.detail,
                    system_text, user_text, api_key)

                cost = (tokens_in / 1e6 * price["in"]
                        + tokens_out / 1e6 * price["out"])

                # Raw answer in its own transaction, on purpose: if parsing
                # fails the answer is already saved and need not be bought again.
                stage = "extractions"
                with conn:
                    with conn.cursor() as cur:
                        extraction_id = store_extraction(
                            cur, doc_id, run_id, args.model, prompt_version,
                            answer, tokens_in, tokens_out, cost, duration_ms)

                total_in += tokens_in
                total_out += tokens_out
                total_cost += cost

                if extraction_id is None:
                    print("        already extracted in this run, skipped")
                    continue

                warnings, notes = [], []

                stage = "invoice"
                with conn:
                    with conn.cursor() as cur:
                        invoice_id, line_count = store_invoice_and_lines(
                            cur, extraction_id, doc_id, run_id, answer,
                            sender, warnings, notes)

                stage = "arithmetic"
                with conn:
                    with conn.cursor() as cur:
                        problems = check_arithmetic(cur, invoice_id, run_id,
                                                    answer, tolerance)
                        ok = store_check(cur, invoice_id, run_id, problems)

                done += 1
                for note in notes:
                    print(f"        {note}")
                for warning in warnings:
                    print(f"        WARNING: {warning}")
                print(f"        invoice {invoice_id}, {line_count} lines, "
                      f"{duration_ms / 1000:.1f}s, ${cost:.4f}, "
                      f"{'arithmetic ok' if ok else FLAG}")
                for _, message, _impact in problems:
                    print(f"        - {message}")

            except Exception as error:                    # noqa: BLE001
                failed += 1
                message = f"{type(error).__name__}: {error}"
                failures.append((doc_id, stage, message))
                print(f"        FAILED at {stage}: {message}")
                # A failed document is written down and the batch moves on.
                # Stopping would throw away work already paid for.
                try:
                    record_failure(conn, run_id, doc_id, stage, message)
                except Exception as inner:                # noqa: BLE001
                    print(f"        (could not record the failure: {inner})")

        close_run(conn, run_id, done, total_in, total_out, total_cost)

        print()
        print(f"  run {run_id} closed")
        print(f"  processed:  {done}    failed: {failed}")
        print(f"  tokens:     {total_in:,} in / {total_out:,} out")
        print(f"  cost:       ${total_cost:.4f}")
        if failures:
            print()
            print("  failures, also in rec.workflow_errors:")
            for doc_id, stage, message in failures:
                print(f"      [{doc_id}] {stage}: {message[:120]}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
