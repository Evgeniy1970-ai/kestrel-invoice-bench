"""WF-04: match invoice lines to the catalogue, price them against the
contract, and record what was found.

Prints a table. Writes only when asked with --write; without it the
database is not touched and the script can be run as often as you like.

What it does per line, in this order:

  1. delivery date  -- line, then invoice, then period_from
  2. catalogue item -- vendor_sku, then sku_map, then normalised description
  3. unit           -- contract unit, or alt_unit with its factor
  4. contract price -- ref.price_on(sku, delivery date)
  5. verdict        -- MATCH, STALE_PRICE, OVERCHARGE or UNDERCHARGE
  6. discount       -- MISSING_DISCOUNT where the volume threshold is met
  7. vat rate       -- printed on the line for layouts A and B; for
                       layout C, through the bases in the totals block

Document level, once per run: DUPLICATE, invoices repeating one already
in the database.

With --write it fills rec.line_checks (one row per line, matches
included), rec.exceptions (one row per finding), and updates the
rec.doc_checks row WF-02 left.

Usage, from 04-kod/python:

    python sverka.py --run 3
    python sverka.py --run 3 --invoice 25
    python sverka.py --run 3 --only-problems
    python sverka.py --run 3 --write
"""

import argparse
import os
import sys
from decimal import Decimal

import psycopg2


# ---------------------------------------------------------------- reading

def read_lines(cur, run_id, invoice_id):
    """Every line of the run, with the invoice header it belongs to.

    Ordered by invoice and line number so the printed table reads like
    the documents themselves.
    """
    where = "il.run_id = %s"
    params = [run_id]
    if invoice_id:
        where += " and il.invoice_id = %s"
        params.append(invoice_id)

    cur.execute(f"""
        select
            il.id, il.invoice_id, il.line_no,
            il.raw_description, il.vendor_sku,
            il.quantity, il.unit, il.unit_price, il.line_net,
            il.delivery_date, il.discount_pct, il.vat_rate,
            i.supplier_code, i.invoice_number, i.layout,
            i.delivery_date, i.period_from, i.extraction_id
        from doc.invoice_lines il
        join doc.invoices i on i.id = il.invoice_id
        where {where}
        order by il.invoice_id, il.line_no
    """, params)
    return cur.fetchall()


def load_settings(cur, run_id):
    """The tolerances this run measures with.

    Read from rec.runs.settings_snapshot, not from ref.settings: the
    snapshot is what the run was started with, and a run has to stay
    comparable with itself even if the live settings move afterwards.
    """
    cur.execute("select settings_snapshot from rec.runs where id = %s",
                (run_id,))
    row = cur.fetchone()
    if row is None:
        sys.exit(f"Run {run_id} does not exist.")

    settings = row[0] or {}
    missing = [key for key in ("tolerance_abs_eur", "tolerance_pct")
               if settings.get(key) is None]
    if missing:
        sys.exit(f"Run {run_id} has no {', '.join(missing)} in its "
                 f"settings snapshot.")

    return (Decimal(str(settings["tolerance_abs_eur"])),
            Decimal(str(settings["tolerance_pct"])))


def load_mismatched_lines(cur, run_id):
    """Lines WF-02 found inconsistent with themselves, this run only.

    The run filter matters: the same document extracted twice leaves an
    exception under each run, and a line from another run has nothing to
    do with this one.

    These are the lines where the printed unit price is not what the
    total was worked out from, so the price to compare is the recovered
    one rather than what the column says.
    """
    cur.execute("""
        select line_id from rec.exceptions
        where run_id = %s
          and type = 'ARITHMETIC_MISMATCH'
          and line_id is not null
          and retracted_at is null
    """, (run_id,))
    return {row[0] for row in cur.fetchall()}


RULE_ROUNDED_PRICE = "rule:printed_price_rounded"


def close_rounded_price(cur, run_id, line_ids):
    """Close WF-02 arithmetic flags the contract price fully explains.

    WF-02 recomputes quantity x printed price and compares that against the
    line total. It has no price list -- by design -- so when a supplier
    prints 0.18 in the price column and works the total out from a contract
    price of 0.1850, WF-02 sees a document contradicting itself. It is not
    wrong: read off the printed column, the line really does not add up.

    Here the price list IS visible, and it says the total is exactly right.
    Run 5: 26 of the 61 false alarms were this, on prices like 0.1850,
    0.1450, 8.2350 -- four contract decimals shown in two.

    The test is: is the printed line total within half a cent of the
    contract price times quantity, less the printed discount. NOT whether
    the recovered price equals the contract one -- that fails on 19 of the
    26, because the total itself is rounded. 5.4 x 8.2350 = 44.469 prints
    as 44.47, and dividing back gives 8.2352.

    No tolerance anywhere, deliberately. Widening the arithmetic tolerance
    was considered and refused: on this pipeline the real price is recovered
    from the total ONLY on lines WF-02 flagged, so a wider tolerance would
    switch the recovery off and hide the overcharge with it. A tolerance
    scaled by quantity would also mean a longer line can be skimmed harder,
    which is a rule that says where to steal. Cent rounding is the single
    liberty, its size does not grow with the line, and a cent over contract
    still breaks the equality.

    The row is not deleted. It gets resolved_by and a resolution, so a human
    can see it was closed and by what. WF-05 tells these apart from its own
    work by resolved_by holding a rule name rather than a person.
    """
    if not line_ids:
        return 0
    cur.execute("""
        update rec.exceptions
           set resolved_by = %s,
               resolved_at = now(),
               resolution  = 'printed price is a rounded display of the '
                             'contract price; the line total matches the '
                             'contract exactly'
         where run_id = %s
           and type = 'ARITHMETIC_MISMATCH'
           and line_id = any(%s)
           and resolved_by is null
           and retracted_at is null
    """, (RULE_ROUNDED_PRICE, run_id, list(line_ids)))
    return cur.rowcount


def load_flags(cur):
    """The flag dictionary: severity, level, whether it is a service flag.

    Needed because rec.line_checks holds one flag per line while a line
    can carry more than one finding -- a price that matches but a
    discount that is missing, for instance. The strongest one goes in the
    column; every one of them goes into rec.exceptions.
    """
    cur.execute("""
        select flag, severity, level, is_service, truth_defect
        from rec.flags
    """)
    return {row[0]: {"severity": row[1], "level": row[2],
                     "is_service": row[3], "truth_defect": row[4]}
            for row in cur.fetchall()}


def load_catalogue(cur):
    """The whole catalogue, keyed two ways.

    Loaded once rather than queried per line: 104 rows is nothing to hold,
    and a query per line would be 108 round trips for no gain.

    by_sku            -- exact code lookup
    by_description    -- normalised description, per supplier
    by_supplier       -- every item of a supplier, for the prefix pass

    The description key is (supplier_code, lower(strip(description))).
    Per supplier on purpose: two suppliers may sell "Whole milk 1L" at
    different contract prices, and the line belongs to one of them.
    """
    cur.execute("""
        select sku, supplier_code, description, unit,
               alt_unit, alt_unit_factor, vat_rate,
               discount_threshold_qty, discount_pct
        from ref.catalog_items
    """)

    by_sku = {}
    by_description = {}
    by_supplier = {}
    for row in cur.fetchall():
        item = {
            "sku": row[0], "supplier_code": row[1], "description": row[2],
            "unit": row[3], "alt_unit": row[4], "alt_unit_factor": row[5],
            "vat_rate": row[6], "discount_threshold_qty": row[7],
            "discount_pct": row[8],
        }
        by_sku[item["sku"]] = item
        key = (item["supplier_code"], normalise(item["description"]))
        by_description[key] = item
        by_supplier.setdefault(item["supplier_code"], []).append(item)

    return by_sku, by_description, by_supplier


def load_sku_map(cur):
    """Decisions a reviewer made earlier, under both keys the table allows.

    Two dictionaries, because a line can be recognised two ways and the
    reviewer's decision has to survive either. by_code is keyed by supplier
    and the supplier's own item code; by_text by supplier and the
    description, normalised the same way match_item normalises it.

    Reading only the code was the original version, and it would have made
    the dashboard's mapping useless: measured on 6 September, all nineteen
    unmatched lines of run 5 carry no vendor code at all. Layouts B and C do
    not print one - which is why those lines reach the reviewer in the first
    place. A decision stored under a key nobody reads is worse than no
    decision: the person sees it accepted and it never works.

    Rows with neither key cannot exist; sku_map_has_key forbids them.
    """
    cur.execute("""
        select supplier_code, vendor_sku, vendor_description, sku
        from ref.sku_map
    """)
    by_code = {}
    by_text = {}
    for supplier_code, vendor_sku, vendor_description, sku in cur.fetchall():
        if vendor_sku:
            by_code[(supplier_code, vendor_sku)] = sku
        if vendor_description:
            by_text[(supplier_code, normalise(vendor_description))] = sku
    return {"by_code": by_code, "by_text": by_text}


# ---------------------------------------------------------------- the four steps

def normalise(text):
    """Lower case, ends trimmed. Nothing more.

    Measured on 108 lines: this alone matched 106. The two misses were
    surcharges absent from the contract, not spelling differences. A
    cleverer rule would be guesswork with no evidence behind it.
    """
    if text is None:
        return ""
    return text.strip().lower()


def delivery_date_for(line_date, invoice_date, period_from):
    """Step 1. Which date the price is looked up on.

    Line, then invoice, then period start. Never the invoice date: that
    is what STALE_PRICE rests on. A supplier billing an old price is
    invisible if we ask the price list for the month the invoice was
    written rather than the day the goods arrived.

    Returns (date, source) or (None, None) when there is nothing to use.
    """
    if line_date is not None:
        return line_date, "line"
    if invoice_date is not None:
        return invoice_date, "invoice"
    if period_from is not None:
        return period_from, "period_from"
    return None, None


def match_item(supplier_code, vendor_sku, description,
               by_sku, by_description, by_supplier, sku_map):
    """Step 2. Which catalogue item this line refers to.

    Four passes, best first. Reported as one of the values the schema
    allows in rec.line_checks.match_method: vendor_sku, normalized,
    manual, none. The prefix pass reports as normalized -- there is no
    better value, and adding one would mean touching a constraint in two
    tables.

    A vendor code is only accepted when it belongs to this supplier.
    Codes are the supplier's own and two suppliers may use the same one.

    sku_map is consulted twice, once under each key it stores: by the
    vendor's code where the invoice prints one, by the description where it
    does not. Both report as manual - the method says who decided, not how
    the row was found.

    The prefix pass exists because invoices annotate descriptions:
    "Espresso machine cleaning tablets (less 8% volume)" is catalogue item
    COF-0607 with a note about the discount stuck on the end. Measured
    across all 104 items, no two become indistinguishable this way.

    When more than one item matches by prefix, nothing is returned. A
    guess here would cost twice: the real defect missed and a false one
    invented. Today no such pair exists; the catalogue may grow.
    """
    if vendor_sku:
        item = by_sku.get(vendor_sku)
        if item and item["supplier_code"] == supplier_code:
            return item, "vendor_sku"

        mapped = sku_map["by_code"].get((supplier_code, vendor_sku))
        if mapped:
            item = by_sku.get(mapped)
            if item:
                return item, "manual"

    text = normalise(description)

    # A person's decision outranks the catalogue passes below. If a reviewer
    # said this description means this item, that settles it - even if the
    # catalogue later grows an item that would match by itself.
    mapped = sku_map["by_text"].get((supplier_code, text))
    if mapped:
        item = by_sku.get(mapped)
        if item:
            return item, "manual"

    item = by_description.get((supplier_code, text))
    if item:
        return item, "normalized"

    if text:
        starts = [candidate
                  for candidate in by_supplier.get(supplier_code, [])
                  if text.startswith(normalise(candidate["description"]))]
        if len(starts) == 1:
            return starts[0], "normalized"

    return None, "none"

def load_unit_synonyms(cur):
    """What a supplier calls a unit, against what the contract calls it.

    Keyed (supplier_code, invoice unit) -> contract unit. Loaded once, like
    the catalogue. A synonym carries no factor: it says two words mean the
    same unit, not that one is a package of the other. Repackaging stays in
    alt_unit, where the factor lives.
    """
    cur.execute("""
        select supplier_code, invoice_unit, contract_unit
        from ref.unit_synonyms
    """)
    return {(row[0], row[1].strip().lower()): row[2]
            for row in cur.fetchall()}


def unit_factor_for(line_unit, item, synonyms=None):
    """Step 3. Bring the line's unit onto the contract's.

    Before the price, not after. A contract price per kg against an
    invoice price per crate differ by the crate size, and comparing them
    would report an overcharge that is really a change of units.

    Returns (factor, note) or (None, reason) when the units do not meet.
    The factor multiplies the CONTRACT price to reach the line's unit.
    """
    if line_unit is None:
        return None, "line has no unit"

    line_unit = line_unit.strip()

    if line_unit == item["unit"]:
        return Decimal(1), "contract unit"

    if synonyms:
        key = (item["supplier_code"], line_unit.lower())
        same_as = synonyms.get(key)
        if same_as == item["unit"]:
            return Decimal(1), f"synonym of {item['unit']}"
        if same_as and same_as == item["alt_unit"]:
            if item["alt_unit_factor"] is None:
                return None, f"alt unit {same_as} has no factor"
            return (item["alt_unit_factor"],
                    f"synonym of alt unit, x{item['alt_unit_factor']}")

    if item["alt_unit"] and line_unit == item["alt_unit"]:
        if item["alt_unit_factor"] is None:
            return None, f"alt unit {line_unit} has no factor"
        return item["alt_unit_factor"], f"alt unit, x{item['alt_unit_factor']}"

    return None, f"unit {line_unit} is neither {item['unit']} nor alt"


def contract_price(cur, sku, on_date):
    """Step 4. What the contract said on that date.

    ref.price_on does the work. Its own comment warns against passing an
    invoice date; we pass what step 1 decided.

    Price versions do not overlap, so at most one row comes back.
    """
    cur.execute("select * from ref.price_on(%s, %s)", (sku, on_date))
    row = cur.fetchone()
    if row is None:
        return None, None, None
    return row[0], row[1], row[2]


def invoiced_price(printed, quantity, discount, line_net, mismatched):
    """Step 5a. The price the customer was actually charged.

    A consistent line: the printed price. An inconsistent one: the price
    recovered from the line total, because the customer pays the total,
    not the number in the price column. INV-0034 line 20 prints 1.05 and
    charges 1.04901; comparing the printed one overstates the overcharge
    by eight cents.

    Only divided where WF-02 already found the line inconsistent. On a
    consistent line the division returns the printed price anyway, and
    dividing where there is nothing to find is a way to invent errors:
    quantity can be zero or absent.

    Returns (price, source).
    """
    if not mismatched:
        return printed, "printed"

    if not quantity or line_net is None:
        return printed, "printed (cannot recover)"

    share = Decimal(1) - (discount or Decimal(0))
    if share <= 0:
        return printed, "printed (cannot recover)"

    return line_net / (quantity * share), "recovered"


def compare_price(cur, sku, on_date, charged, contract, tol_abs, tol_pct,
                  quantity):
    """Step 5b. The verdict on the price.

    Inside max(tolerance_abs_eur / quantity, tolerance_pct x contract) it
    is a MATCH.

    The absolute part is divided by the quantity, and that division is the
    whole point. It exists because a contract price carries four decimals
    while an invoice prints two, so the price may legitimately differ by
    up to half a cent. That slack belongs to the PRICE. Left undivided it
    silently becomes slack per unit, and the line multiplies it by the
    quantity.

    Run 5 shows the cost. A takeaway cup contracted at 0.0620 was billed
    at 0.0660 -- a 6% overcharge -- and one cent of allowance swallowed
    it, because one cent is a sixth of the price of a cup. Across 310 cups
    that was 1.24 EUR the customer paid and the bench called a MATCH.
    A second line cost 4.20 the same way.

    Dividing turns the rule into "a cent on the line", not "a cent on
    every single unit". On small quantities nothing changes: the
    percentage is the larger term there and still governs. Checked over
    run 5: of 1348 MATCH lines exactly 2 change verdict, and both are
    planted defects. Outside it, the two cases differ in what the customer says to
    the supplier, so they are told apart rather than lumped together:

      STALE_PRICE   -- the charge equals this item's price in another
                       period, to the cent. "You billed an old price."
      OVER/UNDER    -- it matches no period. "You put the price up."

    The other-period check uses ref.price_other_periods and the same
    per-line allowance -- a cent divided by the quantity, never the
    percentage. The percentage is deliberately kept out: STALE_PRICE
    claims the charge IS an old price, and a claim like that has to be
    exact. The cent is there only because the charge is recovered from a
    line total rounded to cents, so it carries an error of half a cent
    spread over the quantity.

    A flat cent was what stood here before, and it was too wide on cheap
    goods for the very reason this docstring already warned about: an
    ordinary overcharge landing near an old price gets reported as the
    wrong kind of problem. Run 5, INV-0217: vacuum pouches contracted at
    0.0580 were billed at 0.0630, the Q1 price was 0.0550, and 0.008
    apart counted as "the same price" because a cent is a fifth of what a
    pouch costs. The customer was overcharged 4.20 and would have been
    told the supplier used an old price list.

    Checked over all 13 STALE_PRICE lines of run 5: the eleven genuine
    ones are exact or sit inside the rounding of their own line total,
    and only the two misnamed ones move.
    """
    if charged is None or contract is None:
        return "NO PRICE", None, None

    gap = charged - contract
    per_line = tol_abs / quantity if quantity else tol_abs
    allowed = max(per_line, contract * tol_pct)
    if abs(gap) <= allowed:
        return "MATCH", gap, None

    cur.execute("select * from ref.price_other_periods(%s, %s)",
                (sku, on_date))
    for price, version_code, valid_from, valid_to in cur.fetchall():
        if abs(charged - price) <= per_line:
            return "STALE_PRICE", gap, version_code

    return ("OVERCHARGE" if gap > 0 else "UNDERCHARGE"), gap, None


def check_discount(quantity, given, item, charged):
    """Step 6. Whether the volume discount the contract promises is there.

    The threshold is per line, not per invoice. Measured on the eight
    discounted lines in the run: every one of them clears its own
    threshold, and none needs quantities from sibling lines added up.
    A monthly accumulation would be a different arrangement, negotiated
    separately, and nothing in ref.catalog_items describes one.

    NULL in the line means the invoice printed a dash, and WF-02 kept
    that apart from a printed zero on purpose. For this comparison both
    come to the same thing -- no discount was applied -- so NULL is read
    as zero here rather than left to poison the comparison.

    Both sides are fractions: 0.08 is eight per cent. Verified across the
    whole catalogue and every line of the run -- no value above 1 on
    either side. Comparing a percentage against a fraction would read as
    working and silently pass an understated discount: 2 < 0.08 is false
    for the same reason 8 < 0.08 is.

    A discount larger than the contract's raises nothing. That is the
    supplier's to give.

    Returns (verdict, impact) where impact is what the missing discount
    cost, or (None, None) when there is nothing to check.
    """
    threshold = item["discount_threshold_qty"]
    owed = item["discount_pct"]
    if threshold is None or owed is None or quantity is None:
        return None, None

    if quantity < threshold:
        return None, None

    given = given or Decimal(0)
    if given >= owed:
        return None, None

    impact = None
    if charged is not None:
        impact = quantity * charged * (owed - given)

    return "MISSING_DISCOUNT", impact


def check_vat_rate(printed, item, line_net):
    """Step 7, layouts A and B: the rate is printed on the line.

    A straight comparison against the catalogue. Both sides are
    fractions -- but the line's rate comes from doc.invoice_lines, where
    WF-02 already normalised it, while the invoice's own summary block
    prints 20 rather than 0.20. That conversion belongs to the summary
    reader below, not here.

    Returns (verdict, impact) or (None, None). Impact is the money the
    wrong rate moved, WITH ITS SIGN: base times charged rate minus owed
    rate. A rate too low is negative -- the client was undercharged tax,
    and that is not a gift: he answers for it to his own tax office. The
    ground truth signs it the same way, and the type sums to a negative
    across the batch. Taking an absolute value here would hide that.
    """
    if printed is None or item is None:
        return None, None
    if printed != item["vat_rate"]:
        impact = None
        if line_net is not None:
            impact = line_net * (printed - item["vat_rate"])
        return "WRONG_VAT_RATE", impact
    return None, None


def read_vat_summary(cur, extraction_id):
    """The rate summary printed in the totals block, as the model read it.

    Lives in doc.extractions.raw_json; there is no table for it. Present
    on every invoice of the run, on all three layouts, never empty.

    The model only copied what the document prints. It added nothing up
    and checked nothing -- the arithmetic below is the code's.

    Rates arrive as whole numbers: 20 means twenty per cent. The
    catalogue stores 0.20. Converting here rather than at the comparison
    keeps the two representations from ever meeting: 20 == 0.20 is false,
    and it is false quietly.

    Returns {rate as a fraction: base} or {} when there is no summary.
    """
    cur.execute("""
        select coalesce(raw_json -> 'vat_breakdown', '[]'::jsonb)
        from doc.extractions where id = %s
    """, (extraction_id,))
    row = cur.fetchone()
    if row is None:
        return {}

    summary = {}
    for group in row[0]:
        rate = group.get("rate")
        base = group.get("base")
        if rate is None or base is None:
            continue
        summary[Decimal(str(rate)) / 100] = Decimal(str(base))
    return summary


def check_vat_bases(summary, lines_with_items, tol_abs):
    """Step 7, layout C: no rate on the lines, only bases in the totals.

    Rather than searching for a subset of lines that adds up to a base --
    a million combinations on twenty lines, a billion on thirty, and no
    guarantee the answer is unique -- add the lines up by the rate the
    catalogue gives each item and see whether the totals agree.

    What this does NOT do is write the catalogue's rate onto the line.
    That would leave the check comparing the catalogue against itself:
    always equal, and the planted defect gone without trace. A supplier
    charging 20% on coffee would pass silently, because we would have
    quietly corrected him first.

    So the defect shows up as a gap between our sum and the printed base,
    and the line is found through that gap:

      the sums agree            -> nothing
      gap equals one line, and
        moving that line makes
        sense                   -> WRONG_VAT_RATE on it
      anything else             -> LOW_CONFIDENCE

    Charging the wrong rate does not change what the line costs -- only
    which basket the cost lands in. So one basket is heavier by exactly
    what the other is lighter, and that amount is the line that moved.

    A gap that equals one line could still be two lines erring in
    opposite directions. That risk is not measured. Where it happens the
    flag lands on an innocent line -- which is why "moving it makes
    sense" is checked at all: it throws out the versions that cannot be.

    Returns a list of (line_id, verdict, note, impact). Impact is filled
    only for WRONG_VAT_RATE, with its sign: the moved amount times the
    charged rate minus the owed one. On layout C the charged rate is not
    printed anywhere on the line -- it is known only here, from which
    basket came up heavy. So this is the only place that can price it.
    """
    if not summary:
        return []

    ours = {}
    total_by_line = {}
    for line_id, line_net, item in lines_with_items:
        if item is None or line_net is None:
            # An unmatched line has no catalogue rate, so no basket to
            # go in. Its SKU_UNMATCHED is the finding; guessing a rate
            # for it would only invent a second one.
            return [(None, "LOW_CONFIDENCE",
                     "a line has no catalogue rate, bases cannot be checked",
                     None)]
        rate = item["vat_rate"]
        ours[rate] = ours.get(rate, Decimal(0)) + line_net
        total_by_line[line_id] = (line_net, rate)

    gaps = {rate: summary.get(rate, Decimal(0)) - ours.get(rate, Decimal(0))
            for rate in set(summary) | set(ours)}

    if all(abs(gap) <= tol_abs for gap in gaps.values()):
        return []

    heavier = [rate for rate, gap in gaps.items() if gap > tol_abs]
    lighter = [rate for rate, gap in gaps.items() if gap < -tol_abs]

    if len(heavier) != 1 or len(lighter) != 1:
        return [(None, "LOW_CONFIDENCE",
                 "vat bases do not balance between two rates", None)]

    moved_to, moved_from = heavier[0], lighter[0]
    amount = gaps[moved_to]

    # The line that moved is the one whose total equals the gap, and
    # whose catalogue rate is the basket that came up short.
    suspects = [line_id for line_id, (net, rate) in total_by_line.items()
                if rate == moved_from and abs(net - amount) <= tol_abs]

    if len(suspects) == 1:
        return [(suspects[0], "WRONG_VAT_RATE",
                 f"charged at {moved_to * 100:g}% instead of "
                 f"{moved_from * 100:g}%, {amount:.2f} eur moved",
                 amount * (moved_to - moved_from))]

    if len(suspects) > 1:
        return [(None, "LOW_CONFIDENCE",
                 f"{len(suspects)} lines could explain a "
                 f"{amount:.2f} eur gap")]

    return [(None, "LOW_CONFIDENCE",
             f"no single line explains a {amount:.2f} eur gap")]


def find_duplicates(cur, run_id):
    """Document level: invoices that repeat one already in the database.

    Searched across the whole database, not within the run. A real
    duplicate arrives days later and lands in a different run -- ours are
    a week apart, 5 and 11 April.

    Two tests, and they are not equals:

      invoice_number -- same supplier, same invoice number. Near proof.
      amount_date    -- same supplier, date and total under a different
                        number. A suspicion, and the one that produces
                        false alarms: a supplier delivering daily can
                        honestly bill twice on one date for one amount.

    Reported apart for that reason, and stored rather than derived: both
    tests can hold on one pair, so which caught it depends on the order
    the checks run in.

    The earlier document is the original and carries no flag. Flagging
    both would block the original too, and a duplicate is exactly the
    case where the customer needs to pay one and refuse the other.

    Earlier means by doc.documents.received_at; where those are equal,
    by the lower document id. The id records the actual order of arrival
    in the database, which is the only real evidence left when the mail
    dates match -- and it always exists.

    A repeated file never reaches here: WF-01 stops it by SHA-256. What
    reaches here is the same invoice in a rebuilt PDF.

    Returns a list of (invoice_id, original_invoice_id, matched_by,
    supplier_code, invoice_number, total, impact).
    """
    cur.execute("""
        with mine as (
            select i.id, i.supplier_code, i.invoice_number,
                   i.invoice_date, i.total,
                   d.id as document_id, d.received_at
            from doc.invoices i
            join doc.documents d on d.id = i.document_id
            where i.run_id = %s
        ),
        every_invoice as (
            select i.id, i.supplier_code, i.invoice_number,
                   i.invoice_date, i.total,
                   d.id as document_id, d.received_at
            from doc.invoices i
            join doc.documents d on d.id = i.document_id
        )
        select m.id, o.id, 'invoice_number', m.supplier_code,
               m.invoice_number, m.total
        from mine m
        join every_invoice o
          on o.supplier_code = m.supplier_code
         and o.invoice_number = m.invoice_number
         and o.id <> m.id
         and (o.received_at, o.document_id) < (m.received_at, m.document_id)

        union all

        select m.id, o.id, 'amount_date', m.supplier_code,
               m.invoice_number, m.total
        from mine m
        join every_invoice o
          on o.supplier_code = m.supplier_code
         and o.invoice_date = m.invoice_date
         and o.total = m.total
         and o.invoice_number <> m.invoice_number
         and o.id <> m.id
         and (o.received_at, o.document_id) < (m.received_at, m.document_id)

        order by 1, 3
    """, (run_id, ))

    # One finding per invoice. Both tests can fire on the same pair, and
    # the invoice_number one is the stronger claim, so it wins -- the
    # union above puts it first for each invoice.
    seen = set()
    findings = []
    for invoice_id, original_id, matched_by, supplier, number, total in \
            cur.fetchall():
        if invoice_id in seen:
            continue
        seen.add(invoice_id)
        # Paying a duplicate costs the whole invoice, not a difference.
        findings.append((invoice_id, original_id, matched_by,
                         supplier, number, total, total))

    return findings


def check_line(cur, row, by_sku, by_description, by_supplier, sku_map,
               mismatched, tol_abs, tol_pct, synonyms=None):
    """Everything that can be decided about one invoice line.

    Lifted out of main() on 6 September, unchanged in meaning, so that WF-05
    can ask the same question about one line that a run asks about all of
    them. A person who maps an unrecognised item in the dashboard needs the
    line checked again straight away, and checking it there by hand would
    mean two sets of rules drifting apart.

    Reads the price list; writes nothing.

    Layout C is the one thing not settled here. Those invoices print no rate
    on the line, so the rate is checked once the whole basket is known -- the
    caller collects them and calls check_vat_bases.

    Returns a dict. The caller counts, prints and stores; none of that
    belongs to a single line.
    """
    (line_id, invoice_id, line_no, description, vendor_sku,
     quantity, line_unit, unit_price, line_net, line_delivery,
     discount_pct, line_vat_rate,
     supplier_code, invoice_number, layout,
     invoice_delivery, period_from, extraction_id) = row

    on_date, date_source = delivery_date_for(
        line_delivery, invoice_delivery, period_from)

    item, how = match_item(
        supplier_code, vendor_sku, description,
        by_sku, by_description, by_supplier, sku_map)

    matched_sku = item["sku"] if item else ""
    contract_text = charged_text = gap_text = ""
    source_text = version = ""
    verdict = ""
    discount_verdict = None
    discount_impact = None
    contract = gap = factor = charged = price = None
    explained = False

    if on_date is None:
        verdict = "NO_DELIVERY_DATE"
    elif item is None:
        verdict = "SKU_UNMATCHED"
    else:
        factor, _ = unit_factor_for(line_unit, item, synonyms)
        if factor is None or factor != 1:
            # Two different situations, one verdict.
            #
            # factor is None: the unit cannot be converted at all.
            #
            # factor != 1: it can, but the invoice bills in a unit
            # the contract does not use -- boxes against a price
            # per kilo. The conversion rate lives in the catalogue,
            # NOT in the invoice, so applying it silently turns our
            # own assumption into a verified fact. Run 5 shows what
            # that costs: INV-0195 billed one box at 71.60, the
            # catalogue says a box is 4 kg, 17.90 x 4 came to
            # exactly 71.60, and the line was called MATCH. The
            # delivery was 2.4 kg. The client overpaid 28.64 and
            # the bench stayed silent.
            #
            # Whatever quantity you call a box, the arithmetic
            # agrees with itself. That is precisely why the unit
            # itself has to be reported.
            #
            # No price verdict is issued on these lines. The money
            # would be computed through the same assumption that is
            # under question, and naming a sum we cannot stand
            # behind is worse than naming none: a person will open
            # the document either way.
            verdict = "UNIT_MISMATCH"
        else:
            price, _, version_code = contract_price(
                cur, item["sku"], on_date)
            if price is None:
                verdict = "NO_PRICE_FOUND"
            else:
                contract = price * factor
                version = version_code
                contract_text = f"{contract:.4f}"

                if line_id in mismatched and quantity \
                        and line_net is not None:
                    by_contract = (
                        quantity * contract
                        * (Decimal(1) - (discount_pct
                                         or Decimal(0))))
                    # Half a cent, not a rounded comparison.
                    # Suppliers do not agree on which way to
                    # round a half: 1625 x 0.1850 less 8% is
                    # exactly 276.575 and the invoice prints
                    # 276.57, while rounding half up gives
                    # 276.58. Five lines of run 5 sit on that
                    # boundary. Asking "is the printed total
                    # this same number shown to cents" works
                    # whichever way the supplier rounds.
                    #
                    # Half a cent is fixed and does not grow
                    # with the line, so it is not a tolerance
                    # in the dangerous sense: one cent over
                    # contract and the line stays flagged.
                    if abs(by_contract - line_net) \
                            <= Decimal("0.005"):
                        explained = True

                charged, source = invoiced_price(
                    unit_price, quantity, discount_pct,
                    line_net, line_id in mismatched)
                if charged is not None:
                    charged_text = f"{charged:.4f}"
                source_text = "rec" if source == "recovered" \
                    else "prn"

                verdict, gap, stale_version = compare_price(
                    cur, item["sku"], on_date, charged,
                    contract, tol_abs, tol_pct, quantity)
                if gap is not None:
                    gap_text = f"{gap:+.4f}"
                if stale_version:
                    version = stale_version

                discount_verdict, discount_impact = \
                    check_discount(quantity, discount_pct,
                                   item, charged)


    # Layouts A and B print the rate on the line, so it is
    # compared straight away. Layout C prints no line rate at
    # all -- those invoices are gathered and checked below.
    vat_verdict = None
    vat_impact = None
    if layout != "C":
        vat_verdict, vat_impact = check_vat_rate(
            line_vat_rate, item, line_net)

    # What gets written. Findings are collected as a list --
    # a line can carry more than one, and only the strongest
    # goes into rec.line_checks.flag while all of them go to
    # rec.exceptions.
    found = []
    if verdict:
        impact = None
        if verdict == "SKU_UNMATCHED":
            # The whole line is at stake, not a difference.
            # Nothing was agreed for this item, so there is no
            # contract value to subtract from -- the ground truth
            # signs it the same way, with correct_value empty.
            #
            # This is filled for EVERY unmatched line, not only for
            # the surcharges. The two cannot be told apart: checked
            # on run 3, Fuel levy and Delivery surcharge carry a
            # quantity, a unit and a price exactly like real goods,
            # while a genuine catalogue item (Entrance mat exchange)
            # reads as 1 pcs and looks like a surcharge. Any rule
            # separating them would be a word list, and word lists
            # were rejected on 1 September: a real fraud under an
            # unfamiliar name would sail straight through.
            #
            # So the number means "money awaiting a decision", not
            # "money overcharged". Two consequences for the report,
            # both mandatory: this sum is NEVER added to the sum
            # over matched lines, and it is always shown next to how
            # many lines of the invoice went unmatched. Without the
            # second number a bad scan is indistinguishable from a
            # dishonest supplier.
            impact = line_net
        elif gap is not None and quantity is not None \
                and verdict != "MATCH":
            impact = gap * quantity
        found.append((verdict, impact, describe(
            verdict, line_no, contract, charged, gap,
            source_text, version, line_unit,
            item["unit"] if item else None)))
    if discount_verdict:
        found.append((
            discount_verdict, discount_impact,
            f"Line {line_no}: {quantity:g} over threshold "
            f"{item['discount_threshold_qty']:g}, "
            f"{item['discount_pct'] * 100:g}% owed, "
            f"{(discount_pct or Decimal(0)) * 100:g}% given."))
    if vat_verdict:
        found.append((
            vat_verdict, vat_impact,
            f"Line {line_no}: charged "
            f"{line_vat_rate * 100:g}%, catalogue says "
            f"{item['vat_rate'] * 100:g}%."))

    written = {
        "invoice_id": invoice_id,
        "matched_sku": item["sku"] if item else None,
        "match_method": how,
        "price_date": on_date,
        "price_version": version or None,
        "expected_price": price if item and factor else None,
        "expected_discount": item["discount_pct"] if item
                             else None,
        "expected_vat": item["vat_rate"] if item else None,
        "unit_factor": factor,
        "variance_abs": gap,
        "variance_pct": (gap / contract) if gap is not None
                        and contract else None,
        "found": found,
    }

    return {
        "line_id": line_id,
        "invoice_id": invoice_id,
        "line_no": line_no,
        "description": description,
        "layout": layout,
        "extraction_id": extraction_id,
        "quantity": quantity,
        "discount_pct": discount_pct,
        "line_vat_rate": line_vat_rate,
        "line_net": line_net,
        "item": item,
        "how": how,
        "on_date": on_date,
        "verdict": verdict,
        "matched_sku": matched_sku,
        "contract_text": contract_text,
        "charged_text": charged_text,
        "gap_text": gap_text,
        "source_text": source_text,
        "version": version,
        "discount_verdict": discount_verdict,
        "discount_impact": discount_impact,
        "vat_verdict": vat_verdict,
        "vat_impact": vat_impact,
        "explained": explained,
        "written": written,
    }


# ---------------------------------------------------------------- writing

# Order the checks run in. Used only to break ties when two findings on
# one line carry the same severity: the earlier step wins. Declared here
# rather than left to whatever order the loop happens to take, so the
# choice is a decision and not an accident.
STEP_ORDER = ("SKU_UNMATCHED", "UNIT_MISMATCH", "NO_PRICE_FOUND",
              "OVERCHARGE", "UNDERCHARGE", "STALE_PRICE",
              "MISSING_DISCOUNT", "WRONG_VAT_RATE",
              "LOW_CONFIDENCE", "MATCH")

SEVERITY_RANK = {"high": 3, "medium": 2, "none": 1}


def main_flag(found, flags_meta):
    """Which of a line's findings goes into rec.line_checks.flag.

    Strongest severity first; ties broken by the order the steps run in.
    The others are not lost -- they are written to rec.exceptions, which
    has no one-per-line limit.
    """
    def rank(entry):
        flag = entry[0]
        severity = flags_meta.get(flag, {}).get("severity", "medium")
        order = STEP_ORDER.index(flag) if flag in STEP_ORDER else 99
        return (-SEVERITY_RANK.get(severity, 2), order)

    return sorted(found, key=rank)[0][0]


def write_line_checks(cur, run_id, results, flags_meta):
    """One row per invoice line, findings and matches alike.

    Every line, not just the problem ones: without the clean rows there
    is no denominator, and "six found" is not a measurement.

    unique (run_id, invoice_line_id) makes this restartable -- a second
    pass over the same run updates rather than duplicates.
    """
    written = 0
    for line_id, r in results.items():
        flag = main_flag(r["found"], flags_meta)
        cur.execute("""
            insert into rec.line_checks
                (run_id, invoice_line_id, matched_sku, match_method,
                 price_date, price_version_used, expected_unit_price,
                 expected_discount_pct, expected_vat_rate,
                 unit_factor_applied, variance_abs, variance_pct, flag)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (run_id, invoice_line_id) do update set
                matched_sku           = excluded.matched_sku,
                match_method          = excluded.match_method,
                price_date            = excluded.price_date,
                price_version_used    = excluded.price_version_used,
                expected_unit_price   = excluded.expected_unit_price,
                expected_discount_pct = excluded.expected_discount_pct,
                expected_vat_rate     = excluded.expected_vat_rate,
                unit_factor_applied   = excluded.unit_factor_applied,
                variance_abs          = excluded.variance_abs,
                variance_pct          = excluded.variance_pct,
                flag                  = excluded.flag
        """, (run_id, line_id, r["matched_sku"], r["match_method"],
              r["price_date"], r["price_version"], r["expected_price"],
              r["expected_discount"], r["expected_vat"],
              r["unit_factor"], r["variance_abs"], r["variance_pct"],
              flag))
        written += 1
    return written


def write_exceptions(cur, run_id, results, duplicates, flags_meta):
    """One row per finding that a person should look at.

    MATCH does not go here -- nothing to review. Everything else does,
    including the service flags, because "could not read this" is also
    something a person has to resolve.

    A finding does have a natural key - run, invoice, line, type - and
    exceptions_natural_key enforces it. So a rerun updates the wording and
    the amount in place and leaves the decision columns alone.

    Until 6 September this cleared every WF-04 row and wrote them afresh,
    on the grounds that they had no natural key. They did, and the cost of
    saying otherwise was measured that day: of eight decisions a reviewer
    had made, five went in a single rerun. Only the three sitting on WF-02's
    arithmetic flags survived, and only because the delete never named them.

    What still gets deleted: rows nobody has decided on, and rows a rule
    closed - close_rounded_price puts those back on every run, so they cost
    nothing to lose. A rerun with changed rules must not leave yesterday's
    findings lying beside today's.

    What survives: anything a person settled. If the rules no longer raise
    it, the row stays as they left it rather than vanishing; a decision is
    not ours to withdraw.

    WF-02's rows are left alone throughout -- the delete names only the
    flags WF-04 raises.
    """
    ours = tuple(f for f in flags_meta if f != "MATCH"
                 and f != "ARITHMETIC_MISMATCH")
    cur.execute("""
        update rec.exceptions
           set retracted_at = now()
         where run_id = %s and type = any(%s)
           and (resolved_by is null or resolved_by like 'rule:%%')
           and retracted_at is null
    """, (run_id, list(ours)))

    written = 0
    for line_id, r in results.items():
        for flag, impact, message in r["found"]:
            if flag == "MATCH":
                continue
            cur.execute("""
                insert into rec.exceptions
                    (run_id, invoice_id, line_id, type, severity,
                     impact_eur, message, origin)
                values (%s, %s, %s, %s, %s, %s, %s, 'run')
                on conflict (run_id, invoice_id, coalesce(line_id, 0), type)
                    where type <> 'ARITHMETIC_MISMATCH'
                do update set
                    severity     = excluded.severity,
                    impact_eur   = excluded.impact_eur,
                    message      = excluded.message,
                    origin       = 'run',
                    retracted_at = null
            """, (run_id, r["invoice_id"], line_id, flag,
                  flags_meta.get(flag, {}).get("severity", "medium"),
                  impact, message))
            written += 1

    for (invoice_id, original_id, matched_by,
         supplier, number, total, impact) in duplicates:
        cur.execute("""
            insert into rec.exceptions
                (run_id, invoice_id, line_id, type, severity,
                 impact_eur, message, origin)
            values (%s, %s, null, 'DUPLICATE', %s, %s, %s, 'run')
            on conflict (run_id, invoice_id, coalesce(line_id, 0), type)
                where type <> 'ARITHMETIC_MISMATCH'
            do update set
                severity     = excluded.severity,
                impact_eur   = excluded.impact_eur,
                message      = excluded.message,
                retracted_at = null
        """, (run_id, invoice_id,
              flags_meta.get("DUPLICATE", {}).get("severity", "high"),
              impact,
              f"Invoice {number} from {supplier} repeats invoice id "
              f"{original_id}, same total {total}. Paying both costs "
              f"the whole invoice."))
        written += 1

    return written


def write_doc_checks(cur, run_id, results, duplicates, flags_meta):
    """The verdict on each document.

    WF-02 already put a row here with arithmetic_ok filled in. WF-04
    updates that row rather than adding a second: the table holds one row
    per document by design, with columns for each stage. arithmetic_ok
    stays where it is, so the earlier finding is not lost.

    Status:
      BLOCKED       -- a duplicate. Pay the original, refuse this one.
      NEEDS_REVIEW  -- any non-service flag, or a service flag saying
                       the document could not be read properly, or an
                       arithmetic flag WF-02 raised and nothing explained.
      AUTO_APPROVED -- nothing open against the document at all.

    The arithmetic part was missing until 6 September, and six documents of
    run 5 were called clean while carrying nine unresolved findings between
    them - MES-26-0385 among them, whose lines add up to 229.50 against a
    printed subtotal of 249.93. The verdict was computed from WF-04's own
    findings only, which is not "the document from every side": WF-02 writes
    into the same table and its rows were never looked at.

    duplicate_kind uses the schema's own vocabulary: rebuilt_file for the
    same invoice number in a different file, new_number for the same
    supplier, date and total under a different number. same_file never
    reaches here -- WF-01 stops it by SHA-256.
    """
    by_invoice = {}
    for r in results.values():
        slot = by_invoice.setdefault(r["invoice_id"], set())
        for flag, _, _ in r["found"]:
            if flag != "MATCH":
                slot.add(flag)

    # Findings this pass did not raise and cannot clear: WF-02's arithmetic
    # flags. Only the ones still open count - the rounded-printed-price rule
    # has already closed the ones it can explain.
    cur.execute("""
        select invoice_id, type
        from rec.exceptions
        where run_id = %s
          and type = 'ARITHMETIC_MISMATCH'
          and resolved_at is null
          and retracted_at is null
    """, (run_id,))
    for invoice_id, flag in cur.fetchall():
        by_invoice.setdefault(invoice_id, set()).add(flag)

    duplicate_by_invoice = {
        invoice_id: (original_id,
                     "rebuilt_file" if matched_by == "invoice_number"
                     else "new_number")
        for (invoice_id, original_id, matched_by,
             _s, _n, _t, _i) in duplicates}

    written = 0
    for invoice_id, found in by_invoice.items():
        duplicate = duplicate_by_invoice.get(invoice_id)
        if duplicate:
            found = found | {"DUPLICATE"}

        if duplicate:
            status = "BLOCKED"
        elif found:
            status = "NEEDS_REVIEW"
        else:
            status = "AUTO_APPROVED"

        cur.execute("""
            update rec.doc_checks
               set status = %s,
                   flags = %s,
                   duplicate_of = %s,
                   duplicate_kind = %s
             where run_id = %s and invoice_id = %s
        """, (status, sorted(found),
              duplicate[0] if duplicate else None,
              duplicate[1] if duplicate else None,
              run_id, invoice_id))
        written += cur.rowcount
    return written


# ---------------------------------------------------------------- printing

def shorten(text, width):
    if text is None:
        return ""
    text = str(text)
    return text if len(text) <= width else text[:width - 1] + "\u2026"


def describe(verdict, line_no, contract, charged, gap, source, version,
             line_unit=None, contract_unit=None):
    """The sentence a person reads in rec.exceptions.

    Written for someone holding the invoice, not for the code: what was
    charged, what the contract said, and where the charged figure came
    from. "rec" means the price was recovered from the line total because
    the printed one did not produce it.
    """
    if verdict == "MATCH":
        return None
    if verdict == "SKU_UNMATCHED":
        return f"Line {line_no}: not in the supplier's catalogue."
    if verdict == "UNIT_MISMATCH":
        if line_unit and contract_unit:
            return (f"Line {line_no}: charged per {line_unit}, contract "
                    f"prices per {contract_unit}. Price not checked - the "
                    f"conversion is ours, not the supplier's.")
        return (f"Line {line_no}: unit differs from the contract's. Price "
                f"not checked - the conversion is ours, not the supplier's.")
    if verdict == "NO_PRICE_FOUND":
        return f"Line {line_no}: in the catalogue, no price on that date."
    if verdict == "NO_DELIVERY_DATE":
        return f"Line {line_no}: no delivery date anywhere on the document."

    where = "recovered from the line total" if source == "rec" \
        else "as printed"
    text = (f"Line {line_no}: charged {charged:.4f} ({where}), "
            f"contract {contract:.4f}, difference {gap:+.4f}.")
    if verdict == "STALE_PRICE":
        text += f" Matches the price in {version} to the cent."
    return text


def main():
    parser = argparse.ArgumentParser(
        description="WF-04 parts one and two: match lines, compare prices")
    parser.add_argument("--run", type=int, required=True,
                        help="rec.runs.id whose lines to read")
    parser.add_argument("--invoice", type=int,
                        help="one doc.invoices.id instead of the whole run")
    parser.add_argument("--only-problems", action="store_true",
                        help="print only the lines that are not a MATCH")
    parser.add_argument("--write", action="store_true",
                        help="write the results to rec.*; without it "
                             "nothing is written")
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        sys.exit("DATABASE_URL is not set in this window.")

    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            tol_abs, tol_pct = load_settings(cur, args.run)
            by_sku, by_description, by_supplier = load_catalogue(cur)
            sku_map = load_sku_map(cur)
            synonyms = load_unit_synonyms(cur)
            flags_meta = load_flags(cur)
            mismatched = load_mismatched_lines(cur, args.run)
            explained = set()
            lines = read_lines(cur, args.run, args.invoice)

            print()
            print(f"  run {args.run}"
                  f"{f', invoice {args.invoice}' if args.invoice else ''}")
            print(f"  catalogue:  {len(by_sku)} items, "
                  f"sku_map: {len(sku_map['by_code']) + len(sku_map['by_text'])}"
                  f" rows")
            print(f"  tolerance:  {tol_abs} eur or {tol_pct * 100}%, "
                  f"whichever is larger")
            print(f"  lines:      {len(lines)}, "
                  f"{len(mismatched)} of them inconsistent in WF-02")
            print()

            header = (f"  {'inv':>4} {'ln':>3}  {'description':<28} "
                      f"{'sku':<9} {'date':<10} "
                      f"{'contract':>9} {'charged':>9} {'src':<4} "
                      f"{'verdict':<12} {'gap':>8}  {'version':<11}")
            print(header)
            print("  " + "-" * (len(header) - 2))

            counts = {"vendor_sku": 0, "normalized": 0,
                      "manual": 0, "none": 0}
            verdicts = {}
            no_date = no_price = no_unit = 0
            printed_rows = 0
            # Layout C is checked per invoice, not per line: the bases
            # only mean something once every line is in a basket.
            by_invoice = {}
            # What will be written, keyed by invoice line id.
            results = {}

            for row in lines:
                checked = check_line(
                    cur, row, by_sku, by_description, by_supplier,
                    sku_map, mismatched, tol_abs, tol_pct, synonyms)

                line_id = checked["line_id"]
                invoice_id = checked["invoice_id"]
                line_no = checked["line_no"]
                description = checked["description"]
                quantity = checked["quantity"]
                discount_pct = checked["discount_pct"]
                line_vat_rate = checked["line_vat_rate"]
                item = checked["item"]
                verdict = checked["verdict"]
                matched_sku = checked["matched_sku"]
                on_date = checked["on_date"]
                contract_text = checked["contract_text"]
                charged_text = checked["charged_text"]
                gap_text = checked["gap_text"]
                source_text = checked["source_text"]
                version = checked["version"]
                discount_verdict = checked["discount_verdict"]
                discount_impact = checked["discount_impact"]
                vat_verdict = checked["vat_verdict"]
                vat_impact = checked["vat_impact"]

                # Counting, printing and storing are the run's business, not
                # the line's. Everything below stayed in main() when the line
                # itself moved out.
                counts[checked["how"]] += 1
                verdicts[verdict] = verdicts.get(verdict, 0) + 1
                if discount_verdict:
                    verdicts[discount_verdict] = \
                        verdicts.get(discount_verdict, 0) + 1
                if vat_verdict:
                    verdicts[vat_verdict] = \
                        verdicts.get(vat_verdict, 0) + 1

                if verdict == "NO_DELIVERY_DATE":
                    no_date += 1
                elif verdict == "UNIT_MISMATCH":
                    no_unit += 1
                elif verdict == "NO_PRICE_FOUND":
                    no_price += 1

                if checked["explained"]:
                    explained.add(line_id)

                if checked["layout"] == "C":
                    slot = by_invoice.setdefault(
                        invoice_id,
                        {"extraction_id": checked["extraction_id"],
                         "lines": []})
                    slot["lines"].append(
                        (line_id, checked["line_net"], item))

                results[line_id] = checked["written"]

                if args.only_problems and verdict == "MATCH" \
                        and not discount_verdict and not vat_verdict:
                    continue
                printed_rows += 1

                print(f"  {invoice_id:>4} {line_no:>3}  "
                      f"{shorten(description, 28):<28} "
                      f"{matched_sku:<9} "
                      f"{str(on_date) if on_date else '':<10} "
                      f"{contract_text:>9} {charged_text:>9} "
                      f"{source_text:<4} "
                      f"{shorten(verdict, 12):<12} {gap_text:>8}  "
                      f"{shorten(version, 11):<11}")

                # A second finding on the same line goes underneath rather
                # than into another column: the table is wide enough, and
                # a wrapped row is harder to read than an indented note.
                if discount_verdict:
                    owed = item["discount_pct"] * 100
                    given = (discount_pct or Decimal(0)) * 100
                    lost = f"{discount_impact:.2f} eur" \
                        if discount_impact is not None else "?"
                    print(f"  {'':>4} {'':>3}  -> MISSING_DISCOUNT: "
                          f"{quantity:g} over threshold "
                          f"{item['discount_threshold_qty']:g}, "
                          f"{owed:g}% owed, {given:g}% given, {lost}")

                if vat_verdict:
                    print(f"  {'':>4} {'':>3}  -> WRONG_VAT_RATE: "
                          f"charged {line_vat_rate * 100:g}%, "
                          f"catalogue says {item['vat_rate'] * 100:g}%")

            if args.only_problems:
                print()
                print(f"  {printed_rows} lines shown, "
                      f"{len(lines) - printed_rows} matched and hidden")

            # Layout C, after every line has been seen.
            if by_invoice:
                print()
                print("  vat bases, layout C")
                print("  " + "-" * 68)
                for invoice_id in sorted(by_invoice):
                    slot = by_invoice[invoice_id]
                    summary = read_vat_summary(cur, slot["extraction_id"])
                    findings = check_vat_bases(summary, slot["lines"],
                                               tol_abs)
                    if not findings:
                        rates = ", ".join(
                            f"{rate * 100:g}%" for rate in sorted(summary))
                        print(f"  invoice {invoice_id}: bases agree "
                              f"({rates})")
                        continue
                    for line_id, vat_verdict, note, vat_impact in findings:
                        verdicts[vat_verdict] = \
                            verdicts.get(vat_verdict, 0) + 1
                        where = f"line id {line_id}" if line_id else "invoice"
                        print(f"  invoice {invoice_id}: {vat_verdict} "
                              f"on {where}")
                        print(f"  {'':>12} {note}")

                        # A finding with no line of its own is attached
                        # to the first line of the invoice: rec.exceptions
                        # needs somewhere to hang it, and the note says
                        # it is about the document.
                        target = line_id or slot["lines"][0][0]
                        if target in results:
                            results[target]["found"].append(
                                (vat_verdict, vat_impact, note))

            # Document level, so it goes under the line table rather than
            # into it. Skipped when looking at a single invoice: a
            # duplicate is a statement about a pair.
            duplicates = []
            if not args.invoice:
                duplicates = find_duplicates(cur, args.run)
                print()
                if duplicates:
                    print("  duplicates")
                    print("  " + "-" * 60)
                    for (invoice_id, original_id, matched_by,
                         supplier, number, total, impact) in duplicates:
                        print(f"  invoice {invoice_id} repeats "
                              f"invoice {original_id}: "
                              f"{supplier} {number}, {total} eur")
                        print(f"  {'':>10} caught by {matched_by}, "
                              f"paying it twice costs {impact} eur")
                else:
                    print("  duplicates: none")

            print()
            print(f"  matched by code:        {counts['vendor_sku']:>4}")
            print(f"  matched by description: {counts['normalized']:>4}")
            print(f"  matched by sku_map:     {counts['manual']:>4}")
            print(f"  not matched:            {counts['none']:>4}")
            print()
            for verdict in sorted(verdicts):
                print(f"  {verdict + ':':<24}{verdicts[verdict]:>4}")
            if duplicates:
                print(f"  {'DUPLICATE:':<24}{len(duplicates):>4}")
            print()
            if explained:
                print(f"  explained by rounded printed price: "
                      f"{len(explained)} WF-02 arithmetic flags")
            if not args.write:
                print("  Nothing was written. Add --write to store this.")
            elif args.invoice:
                print("  Refusing to write a single invoice: the run's "
                      "other lines would be left as they were.")
            else:
                checks = write_line_checks(cur, args.run, results,
                                           flags_meta)
                exceptions = write_exceptions(cur, args.run, results,
                                              duplicates, flags_meta)
                # Closing comes before the document verdict, not after:
                # write_doc_checks now asks which arithmetic flags are still
                # open, and a flag explained by a rounded printed price is
                # not one of them.
                closed = close_rounded_price(cur, args.run, explained)
                docs = write_doc_checks(cur, args.run, results,
                                        duplicates, flags_meta)
                conn.commit()
                print(f"  written: {checks} line checks, "
                      f"{exceptions} exceptions, {docs} document verdicts")
                if closed:
                    print(f"  closed as rounded printed price: {closed} "
                          f"WF-02 arithmetic flags")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
