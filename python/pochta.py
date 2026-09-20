#!/usr/bin/env python3
"""
Kestrel Invoice Bench — укладка счетов в почтовый ящик.

Кладёт письма со счетами прямо в папку «Входящие» по IMAP, минуя отправку.
Каждое письмо выглядит как пришедшее от поставщика.

Что делает:
  1. Выбирает документы из эталона (по умолчанию пять разных)
  2. Находит для каждого PDF в папке партии
  3. Собирает письмо: отправитель — поставщик, тема — номер счёта
  4. Кладёт письмо в ящик
  5. Пишет в score.mail_map, какое письмо какому документу отвечает

Имя файла во вложении идёт БЕЗ префикса INV/DUP. В префиксе написан номер
документа из журнала — по нему систему можно было бы опознать документ, не
разбирая счёт, и измерение потеряло бы смысл.

Использование:
    pip install psycopg2-binary

    $env:GMAIL_USER = "neolith2018ai@gmail.com"
    $env:GMAIL_APP_PASSWORD = "пароль приложения, 16 символов"
    $env:DATABASE_URL = "postgresql://postgres.xxx:ПАРОЛЬ@...pooler.supabase.com:5432/postgres"

    # посмотреть, что будет отправлено, ничего не делая
    python pochta.py --pdf "..\..\03-partiya" --dry-run

    # положить пять писем
    python pochta.py --pdf "..\..\03-partiya"

    # положить очередные двадцать, минуя те, что уже в ящике
    python pochta.py --pdf "..\..\03-partiya" --limit 20

    # убрать ранее положенные письма стенда и положить заново
    python pochta.py --pdf "..\..\03-partiya" --clear

Скрипт подключается к базе под владельцем: ему нужно читать truth и писать
в score. Роль bench_pipeline здесь не подходит и не должна подходить.
"""

import argparse
import email.utils
import glob
import hashlib
import imaplib
import os
import ssl
import sys
from datetime import datetime, time, timezone
from email.message import EmailMessage

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993

# Свой заголовок, чтобы потом отличить письма стенда от всех остальных
# и удалить только их. Без него --clear чистил бы весь ящик.
MARKER = "X-Kestrel-Bench"


# ---------------------------------------------------------------------
# Выбор документов
# ---------------------------------------------------------------------
def already_sent(cur):
    """
    Что уже лежит в ящике с прошлых запусков.

    Партия кладётся порциями, по десятку-другому за раз, как приходит у
    живого заказчика. Значит каждый запуск обязан знать, что было до него,
    иначе он снова выберет те же документы: отбор ниже сам по себе
    детерминирован и без этого условия вернёт один и тот же список.

    Источник — score.mail_map: строка там появляется ровно тогда, когда
    письмо легло в ящик. Ключ таблицы — отпечаток файла, а doc_id хранится
    рядом, поэтому оба скрипта укладки видны здесь одинаково.
    """
    cur.execute("select distinct truth_doc_id from score.mail_map")
    return [row[0] for row in cur.fetchall()]


def pick_documents(cur, limit, sent):
    """
    Пять разных трудностей: три макета, скан, многостраничный счёт.
    Если нужно больше пяти — добираем остальные подряд.

    Дубли исключены только из пяти показательных: там нужна разница макетов
    и качества, а дубль по виду не отличается от оригинала. В добор они
    входят наравне со всеми — без них DUPLICATE_INVOICE, двенадцать дефектов
    журнала из девяноста восьми, до системы просто не доедет и будет
    посчитан пропуском, которого она не совершала.
    """
    chosen = list(sent)
    picks = []

    def take(sql, params=()):
        cur.execute(sql, params + (tuple(chosen) if chosen else ("",),))
        row = cur.fetchone()
        if row:
            chosen.append(row[0])
        return row

    for layout in ("A", "B", "C"):
        row = take("""
            select doc_id from truth.documents
            where layout = %s
              and file_kind = 'PDF с текстом'
              and line_count <= 18
              and duplicate_of is null
              and doc_id not in %s
            order by doc_id
            limit 1
        """, (layout,))
        if row:
            picks.append((row[0], f"макет {layout}"))

    row = take("""
        select doc_id from truth.documents
        where file_kind <> 'PDF с текстом'
          and duplicate_of is null
          and doc_id not in %s
        order by readability desc nulls last, doc_id
        limit 1
    """)
    if row:
        picks.append((row[0], "скан"))

    row = take("""
        select doc_id from truth.documents
        where line_count > 18
          and duplicate_of is null
          and doc_id not in %s
        order by line_count desc, doc_id
        limit 1
    """)
    if row:
        picks.append((row[0], "многостраничный"))

    while len(picks) < limit:
        row = take("""
            select doc_id from truth.documents
            where doc_id not in %s
            order by doc_id
            limit 1
        """)
        if not row:
            break
        picks.append((row[0], "дополнительный"))

    return picks[:limit]


def document_details(cur, doc_ids):
    cur.execute("""
        select d.doc_id, d.invoice_number, d.supplier, d.venue, d.layout,
               d.received_date, d.line_count, d.total_eur, d.file_kind,
               s.supplier_code, s.billing_email, s.name
        from truth.documents d
        left join ref.suppliers s on s.name = d.supplier
        where d.doc_id in %s
    """, (tuple(doc_ids),))
    return {r[0]: r for r in cur.fetchall()}


# ---------------------------------------------------------------------
# Сборка письма
# ---------------------------------------------------------------------
def find_pdf(folder, doc_id):
    """
    Файлы названы «INV-0003_LD-104211.pdf». Ищем по префиксу, а не собираем
    имя из номера счёта: у части поставщиков в номере есть косые черты
    (HMF/26/04/0421), и в имени файла они заменены на дефисы.
    """
    hits = glob.glob(os.path.join(folder, f"{doc_id}_*.pdf"))
    if not hits:
        return None
    return hits[0]


def attachment_name(path, doc_id):
    """INV-0003_LD-104211.pdf -> LD-104211.pdf"""
    return os.path.basename(path).replace(f"{doc_id}_", "", 1)


def build_message(details, pdf_path, to_addr):
    (doc_id, invoice_number, supplier, venue, layout, received_date,
     line_count, total_eur, file_kind, supplier_code, billing_email, name) = details

    if not billing_email:
        raise SystemExit(
            f"{doc_id}: у поставщика «{supplier}» не найден адрес в ref.suppliers.\n"
            f"Скорее всего название в эталоне и в справочнике записано по-разному."
        )

    with open(pdf_path, "rb") as f:
        data = f.read()

    fname = attachment_name(pdf_path, doc_id)

    msg = EmailMessage()
    msg["From"] = email.utils.formataddr((name or supplier, billing_email))
    msg["To"] = to_addr
    msg["Subject"] = f"Invoice {invoice_number}"
    msg["Message-ID"] = email.utils.make_msgid(domain=billing_email.split("@")[-1])
    msg["Date"] = email.utils.format_datetime(
        datetime.combine(received_date, time(9, 30), tzinfo=timezone.utc)
    )
    msg[MARKER] = "1"

    msg.set_content(
        f"Dear {venue},\n\n"
        f"Please find attached invoice {invoice_number} for the amount of "
        f"EUR {total_eur}.\n\n"
        f"Kind regards,\n"
        f"Accounts Receivable\n"
        f"{name or supplier}\n"
    )
    msg.add_attachment(data, maintype="application", subtype="pdf", filename=fname)

    return msg, fname, hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------
# Почта
# ---------------------------------------------------------------------
def connect_imap(user, password):
    imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=ssl.create_default_context())
    try:
        imap.login(user, password)
    except imaplib.IMAP4.error as e:
        raise SystemExit(
            f"Не пускает в ящик: {e}\n"
            f"Проверьте, что используется пароль приложения (16 символов), "
            f"а не обычный пароль от аккаунта."
        )
    return imap


def clear_bench_messages(imap):
    """Удаляет только письма с нашим заголовком."""
    imap.select("INBOX")
    typ, data = imap.search(None, f'HEADER {MARKER} "1"')
    ids = data[0].split() if data and data[0] else []
    if not ids:
        print("  прежних писем стенда не найдено")
        return 0
    for num in ids:
        imap.store(num, "+FLAGS", "\\Deleted")
    imap.expunge()
    print(f"  удалено писем стенда: {len(ids)}")
    return len(ids)


# ---------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True, help="папка с PDF партии")
    ap.add_argument("--limit", type=int, default=5, help="сколько писем положить")
    ap.add_argument("--clear", action="store_true", help="сначала убрать прежние письма стенда")
    ap.add_argument("--dry-run", action="store_true", help="показать план, ничего не делать")
    args = ap.parse_args()

    if not os.path.isdir(args.pdf):
        raise SystemExit(f"Папка не найдена: {args.pdf}")

    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("Не задана переменная DATABASE_URL")

    import psycopg2

    conn = psycopg2.connect(url)
    cur = conn.cursor()

    sent = already_sent(cur)
    cur.execute("select count(*) from truth.documents")
    in_batch = cur.fetchone()[0]
    print(f"\nВ партии {in_batch}, уже положено {len(sent)}, "
          f"осталось {in_batch - len(sent)}")

    picks = pick_documents(cur, args.limit, sent)
    if not picks:
        raise SystemExit(
            "Класть больше нечего: вся партия уже в ящике.\n"
            "Если нужно начать заново, запустите с --clear.")

    details = document_details(cur, [p[0] for p in picks])

    plan = []
    print()
    for doc_id, why in picks:
        d = details.get(doc_id)
        if not d:
            raise SystemExit(f"{doc_id}: нет в truth.documents")
        pdf = find_pdf(args.pdf, doc_id)
        if not pdf:
            raise SystemExit(f"{doc_id}: файл {doc_id}_*.pdf в папке не найден")
        fname = attachment_name(pdf, doc_id)
        print(f"  {doc_id}  {why:<16} {d[2]:<28} вложение: {fname}")
        plan.append((doc_id, d, pdf))

    if args.dry_run:
        print("\nЭто план. Ящик и база не тронуты.")
        return

    user = os.environ.get("GMAIL_USER")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not user or not password:
        raise SystemExit("Не заданы GMAIL_USER и GMAIL_APP_PASSWORD")

    print("\nПодключаюсь к ящику")
    imap = connect_imap(user, password)

    try:
        if args.clear:
            # Чистит ВЕСЬ mail_map, без условия на прогон или дату. Это мост
            # между нашими документами и журналом: без него ни одна метрика
            # не считается. Стёртое восстанавливается только повторной
            # укладкой всей партии, а она стоит времени и снова платного
            # извлечения. Ключ нужен ровно один раз - когда партия кладётся
            # с нуля.
            print("\n  --clear: удаляю письма стенда и ВЕСЬ score.mail_map")
            clear_bench_messages(imap)
            cur.execute("delete from score.mail_map")

        imap.select("INBOX")
        print()
        for doc_id, d, pdf in plan:
            msg, fname, sha = build_message(d, pdf, user)
            typ, resp = imap.append("INBOX", "", None, msg.as_bytes())
            if typ != "OK":
                raise SystemExit(f"{doc_id}: письмо не легло в ящик: {resp}")

            cur.execute("""
                insert into score.mail_map (file_sha256, truth_doc_id, attachment_name,
                                            message_id, from_email, subject, sent_at)
                values (%s, %s, %s, %s, %s, %s, %s)
                on conflict (file_sha256) do update set
                    truth_doc_id = excluded.truth_doc_id,
                    attachment_name = excluded.attachment_name,
                    message_id = excluded.message_id,
                    from_email = excluded.from_email,
                    subject = excluded.subject,
                    sent_at = excluded.sent_at
            """, (sha, doc_id, fname, msg["Message-ID"],
                  email.utils.parseaddr(msg["From"])[1], msg["Subject"],
                  email.utils.parsedate_to_datetime(msg["Date"])))

            print(f"  положено: {doc_id}  {msg['Subject']}")

        conn.commit()
        print(f"\nГотово. Писем в ящике: {len(plan)}. Соответствие записано в score.mail_map.")

    except Exception:
        conn.rollback()
        print("Откат записи в базу. Письма, успевшие лечь в ящик, остались — "
              "уберите их запуском с --clear", file=sys.stderr)
        raise
    finally:
        try:
            imap.logout()
        except Exception:
            pass
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
