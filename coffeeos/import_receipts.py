"""Загрузчик реальных чеков из выгрузки кассы/ОФД (CSV).

Поддерживает гибкие названия колонок (Эвотор, ОФД, Poster, 1С экспортируют
по-разному) и разные форматы дат. Устойчив к битым строкам: плохая строка
пропускается и попадает в счётчик, а не роняет весь файл.

Дедуп: чек опознаётся по паре (дата, номер чека). Если номера чека в выгрузке
нет — по содержимому (время + состав позиций). Поэтому повторная загрузка того
же файла ничего не задваивает, а одинаковые номера в разные дни не конфликтуют.

Использование:
    python -m coffeeos import путь/к/выгрузке.csv [--reset]
"""
import csv
import hashlib
import re
import sys
from collections import defaultdict
from datetime import datetime

from . import catalog as catalog_mod
from . import db


class ImportError_(Exception):
    """Ошибка разбора файла выгрузки (не роняет процесс/автозагрузку)."""


# Ключевые слова для распознавания колонок.
COLUMN_HINTS = [
    ("ts",      ["дата и время", "дата/время", "дата-время", "datetime", "date_time", "timestamp"]),
    ("receipt", ["номер чека", "№ чека", "чек №", "receipt", "check_number", "номер документа", "id чека", "фискальный документ"]),
    # Модификаторы кассы («овсяное молоко», «сироп карамель», «450 мл») лежат
    # отдельной колонкой у Poster, iiko и Quick Resto. Без них «Латте» из чека
    # неотличим от «Латте на овсяном» — а это разная себестоимость и разный
    # расход. Поэтому модификатор приклеивается к названию перед разбором.
    ("mods",    ["модификатор", "модификац", "добавк", "опци", "modifier", "options",
                 "комментарий к блюду"]),
    ("size",    ["размер", "объ[её]м", "size", "volume", "порци"]),
    ("barista", ["сотрудник", "кассир", "бариста", "официант", "продавец", "employee",
                 "waiter", "user_name", "пользователь"]),
    ("guest",   ["карта лояльн", "клиент", "гость", "customer", "guest", "loyalty",
                 "номер карты", "телефон клиента"]),
    ("channel", ["тип заказа", "с собой", "способ обслуж", "order type", "заказ тип",
                 "зал/навынос", "место"]),
    ("name",    ["наимен", "товар", "позиц", "name", "product", "номенклат", "item", "блюдо"]),
    ("qty",     ["кол-во", "колич", "кол.", "кол", "qty", "quantity", "штук", "count"]),
    ("price",   ["цена за", "цена,", "цена", "price"]),
    ("sum",     ["сумма", "итог", "total", "amount", "стоимость"]),
    ("payment", ["тип оплаты", "способ оплаты", "оплат", "payment", "форма расчет"]),
    ("op",      ["тип операции", "тип чека", "тип документа", "вид операции", "операц",
                 "признак расчет", "вид чека", "направление", "operation", "doc_type"]),
    ("date",    ["дата", "date", "день"]),
    ("time",    ["время", "time", "час"]),
]

# Значения колонки «тип заказа»
TAKEAWAY_HINTS = ("собой", "вынос", "takeaway", "to go", "togo", "самовывоз", "навынос")
HERE_HINTS = ("зал", "здесь", "на месте", "in", "dine", "тут")

# признаки возврата в колонке операции
RETURN_HINTS = ("возврат", "return", "refund", "расход")

# Значения колонки оплаты. Кассы и API пишут и по-русски, и по-английски;
# раньше распознавалась только кириллица, и «cash» из API уходило в безнал —
# сверка наличной кассы становилась неверной на 100%.
CASH_HINTS = ("наличн", "нал.", "cash", "касса")
CARD_HINTS = ("безнал", "карт", "card", "эквайр", "electron", "electronic", "sbp", "сбп", "qr")

DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M",
    "%Y/%m/%d %H:%M:%S", "%d-%m-%Y %H:%M:%S",
    "%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y/%m/%d",
)


def _detect_encoding(path):
    """utf-8 или cp1251 (1С и часть касс выгружают в cp1251).
    Проверяем файл ЦЕЛИКОМ: при проверке по куску граница могла разрезать
    кириллический символ, и файл ошибочно признавался cp1251."""
    with open(path, "rb") as fh:
        raw = fh.read()
    for enc in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            raw.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "cp1251"          # последний шанс: cp1251 декодирует любые байты


def _match(header):
    """Определить, какому полю соответствует заголовок колонки.

    Выигрывает подсказка, которая встретилась РАНЬШЕ в заголовке, а при равной
    позиции — более длинная. Разбор «по первому полю из списка» ошибался на
    типовой выгрузке 1С/Эвотора: «Кол-во товара», «Цена товара» и «Сумма
    товара» опознавались как наименование (из-за слова «товар»), колонки денег
    не находились вовсе, и вся выручка молча становилась нулевой.
    """
    hl = header.strip().lower()
    best = None                       # (позиция, -длина подсказки, индекс поля)
    for order_idx, (_field, hints) in enumerate(COLUMN_HINTS):
        for h in hints:
            pos = hl.find(h)
            if pos < 0:
                continue
            cand = (pos, -len(h), order_idx)
            if best is None or cand < best:
                best = cand
    return COLUMN_HINTS[best[2]][0] if best else None


def _payment_of(raw):
    """Тип оплаты по значению ячейки: наличные или безнал (по умолчанию безнал)."""
    p = (raw or "").strip().lower()
    if not p:
        return "card"
    if any(h in p for h in CARD_HINTS):        # «безнал» проверяем раньше «нал»
        return "card"
    if any(h in p for h in CASH_HINTS):
        return "cash"
    return "card"


def _build_cols(header):
    """Сопоставить заголовки с полями. При повторе выигрывает ПЕРВАЯ колонка,
    чтобы «Цена» не затиралась «Ценой со скидкой», а «Дата» — «Временем»."""
    cols = {}
    for idx, h in enumerate(header):
        field = _match(h)
        if field is not None and field not in cols:
            cols[field] = idx
    return cols


def _channel_of(raw):
    """С собой или в зале. Неизвестно — оставляем пустым, а не гадаем:
    от этого зависит расход стаканов."""
    v = (raw or "").strip().lower()
    if not v:
        return None
    if any(h in v for h in TAKEAWAY_HINTS):
        return "takeaway"
    if any(h in v for h in HERE_HINTS):
        return "here"
    return None


# Кассы кладут в пустую колонку модификаторов не пустоту, а заполнитель.
# Приклеенный к названию, он плодит мнимые позиции: «Латте 350», «Латте 350 -»
# и «Латте 350 нет» выглядят как три разных товара в каталоге и в дедупликации.
_PLACEHOLDERS = {"-", "—", "–", "нет", "без", "none", "null", "n/a", "0", "не выбрано",
                 "отсутствует", "no", "false", "--"}


def _full_name(name, mods, size):
    """Склеить название с модификаторами и размером из отдельных колонок.

    Разбирать имеет смысл только полную фразу: «Латте» + «овсяное молоко» —
    это латте на овсяном, и в себестоимости он заметно дороже.
    """
    parts = [name.strip()]
    for extra in (size, mods):
        e = (extra or "").strip().strip(",;")
        if not e or e.lower() in _PLACEHOLDERS:
            continue
        if e.lower() in parts[0].lower():
            continue
        parts.append(e)
    return " ".join(parts)


def _parse_ts(value, time_value=""):
    """Разобрать дату (при необходимости склеив с отдельной колонкой времени)."""
    v = (value or "").strip()
    if not v:
        return None
    # unix-время: кассовые API часто отдают именно его. Раньше такая выгрузка
    # молча давала 0 позиций, а автозагрузка рапортовала «всё в порядке».
    if re.fullmatch(r"\d{10}|\d{13}", v):
        stamp = int(v)
        if len(v) == 13:
            stamp //= 1000
        try:
            return datetime.fromtimestamp(stamp).isoformat()
        except (OSError, OverflowError, ValueError):
            return None
    t = (time_value or "").strip()
    # если в колонке даты нет времени, а отдельная колонка времени есть — склеиваем
    if t and not re.search(r"\d{1,2}:\d{2}", v):
        v = f"{v} {t}"
    v = v.replace("T", " ")
    v = re.sub(r"(\d{1,2}:\d{2}(?::\d{2})?)\.\d+", r"\1", v)   # миллисекунды только после времени
    v = v.split("+")[0].replace("Z", "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(v[:19], fmt).isoformat()
        except ValueError:
            continue
    return None


def _num(v, default=0.0):
    """Число из ячейки: понимает «1 234,50», «1,234.50», «2 шт», пустое."""
    s = (v or "").replace("\xa0", " ").strip()
    if not s:
        return default
    neg = s.startswith("(") and s.endswith(")")      # (90) = −90 в бухгалтерских выгрузках
    s = re.sub(r"[^\d,.\-]", "", s)
    if not s:
        return default
    if "," in s and "." in s:                      # 1,234.50 -> англ. формат
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    try:
        val = float(s)
        return -val if neg else val
    except ValueError:
        return default


def _has_time(ts):
    """В метке времени есть само время, а не только дата."""
    return bool(ts) and ts[11:] not in ("", "00:00:00")


def _receipt_key(ext_id, ts, kind="S", fingerprint="", seq=0, suffix=""):
    """Стабильный ключ чека: (дата + номер + продажа/возврат).

    База ключа — ДАТА, а не полное время: одна и та же выгрузка может прийти с
    временем («Дата и время») и без него («Дата»), и по ключу со временем чек
    загрузился бы дважды, задвоив выручку.

    Два РАЗНЫХ чека с одинаковым номером в один день (посменная нумерация кассы)
    различаются суффиксом, который подбирается при записи по времени чека.

    Если номера чека нет вовсе, опираемся на состав позиций и порядковый номер
    такого же чека внутри файла: иначе двух покупателей, пробитых в одну минуту,
    вторая выгрузка сочла бы дублем и потеряла.
    """
    if ext_id:
        return f"{ts[:10]}#{ext_id}#{kind}{suffix}"
    raw = f"{ts}|{kind}|{fingerprint}|{seq}"
    return "h#" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def _match_existing(by_daynum, g, kind, key):
    """Найти уже загруженный чек, у которого другая точность времени.

    Одна выгрузка даёт «2026-07-01 08:15:00», другая — только «2026-07-01».
    Это ОДИН чек, и загружать его дважды нельзя. А вот два чека с одинаковым
    номером и РАЗНЫМ временем в один день (посменная нумерация) — разные.
    """
    same_day = by_daynum.get((g["ts"][:10], g["ext_id"], kind[:1]), [])
    if not same_day:
        return None, key
    incoming_timed = _has_time(g["ts"])
    for cand in same_day:
        cand_timed = _has_time(cand["ts"])
        if incoming_timed and cand_timed:
            if cand["ts"][:16] == g["ts"][:16]:      # то же время — тот же чек
                return cand["id"], cand["key"]
        else:
            # хотя бы одна сторона без времени — сопоставить можно только по
            # номеру и дате, считаем это тем же чеком
            return cand["id"], cand["key"]
    # тот же номер в тот же день, но другое время — это ДРУГОЙ чек
    return None, f"{key}@{g['ts'][11:16]}"


def _insert_item(conn, rid, name, qty, price, resolve):
    """Записать позицию чека вместе с разбором (вид, размер, молоко, добавки)."""
    p = resolve(name)
    conn.execute(
        "INSERT INTO receipt_items"
        "(receipt_id,name,base,category,kind,size,volume_ml,milk,mods,iced,qty,price)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, name, p["base"], p["category"], p["kind"], p["size"], p["volume_ml"],
         p["milk"], p["mods"], p["iced"], qty, price))
    return p


def _merge_items(conn, rid, incoming, resolve, seen):
    """Свести позиции повторно пришедшего чека с уже загруженными.

    Точечно править строки нельзя: если новая выгрузка схлопывает две строки в
    одну («Латте 1 + Латте 1» -> «Латте 2»), правка первой строки оставляла
    вторую на месте и завышала выручку. Поэтому:
      • состав совпал — ничего не делаем;
      • пришло не меньше строк, чем есть — состав чека перезаписываем целиком;
      • пришло меньше (частичная выгрузка) — только дописываем недостающее,
        ничего не удаляя.
    """
    have = [(r["name"], r["qty"], r["price"]) for r in conn.execute(
        "SELECT name, qty, price FROM receipt_items WHERE receipt_id=?", (rid,))]

    def norm(rows):
        return sorted((n, round(float(q), 6), round(float(p), 6)) for n, q, p in rows)

    if norm(have) == norm(incoming):
        return {"added": 0, "updated": 0}

    if len(incoming) >= len(have):
        conn.execute("DELETE FROM receipt_items WHERE receipt_id=?", (rid,))
        for name, qty, price in incoming:
            seen.add(_insert_item(conn, rid, name, qty, price, resolve)["base"])
        return {"added": 0, "updated": 1}

    # Частичная выгрузка: дописываем ТОЛЬКО позиции, которых в чеке ещё нет.
    # Сверять построчно нельзя — выгрузка могла схлопнуть «Латте 1 + Латте 1»
    # в «Латте 2», и такая строка выглядела бы новой, задваивая выручку.
    present = {h[0] for h in have}
    added = 0
    for name, qty, price in incoming:
        if name in present:
            continue
        seen.add(_insert_item(conn, rid, name, qty, price, resolve)["base"])
        present.add(name)
        added += 1
    return {"added": added, "updated": 0}


def import_csv(path, reset=False):
    conn = db.get_conn()
    try:
        db.init_db(conn)

        # ---------- 1. читаем файл и группируем строки по чекам ----------
        enc = _detect_encoding(path)
        with open(path, encoding=enc, newline="") as fh:
            sample = fh.read(8192)
            fh.seek(0)
            if not sample.strip():
                raise ImportError_("Файл пустой.")
            # Разделитель ищем по СТРОКЕ ЗАГОЛОВКА: в данных попадаются
            # десятичные запятые («90,00»), и подсчёт по всему куску файла
            # выбирал запятую там, где на деле точка с запятой. Табуляцию
            # раньше не знали вовсе — выгрузка из Excel не грузилась.
            head_line = sample.splitlines()[0] if sample.splitlines() else sample
            delim = max((";", ",", "\t", "|"), key=head_line.count)
            if head_line.count(delim) == 0:
                delim = ";"
            reader = csv.reader(fh, delimiter=delim)
            try:
                header = next(reader)
            except StopIteration:
                raise ImportError_("Файл пустой.")
            cols = _build_cols(header)
            if "name" not in cols:
                raise ImportError_(f"Не нашёл колонку с наименованием товара. Заголовки: {header}")
            if "ts" not in cols and "date" not in cols:
                raise ImportError_(f"Не нашёл колонку с датой. Заголовки: {header}")

            def cell(row, key):
                idx = cols.get(key)
                return row[idx].strip() if idx is not None and idx < len(row) else ""

            has_money_col = ("price" in cols) or ("sum" in cols)
            groups = {}          # gkey -> {ts, payment, items:[(name,qty,price)]}
            order = []           # порядок появления чеков
            bad_date = bad_row = returns = 0
            for row in reader:
                try:
                    name = _full_name(cell(row, "name"), cell(row, "mods"), cell(row, "size"))
                    if not name:
                        continue
                    price_cell = cell(row, "price")
                    sum_cell = cell(row, "sum")
                    # Обрезанная строка: наименование есть, а денег нет вовсе.
                    # Раньше такая строка молча вставлялась с ценой 0 и занижала
                    # выручку, а счётчик «битых строк» всегда показывал ноль.
                    if has_money_col and not price_cell and not sum_cell:
                        bad_row += 1
                        continue
                    ts = _parse_ts(cell(row, "ts") or cell(row, "date"), cell(row, "time"))
                    if not ts:
                        bad_date += 1
                        continue
                    ext_id = cell(row, "receipt") or None
                    payment = _payment_of(cell(row, "payment"))
                    qty = _num(cell(row, "qty"), 1) or 1
                    raw_sum = _num(sum_cell) if sum_cell else None
                    price = _num(price_cell) if price_cell else None
                    # Цена «0» при заполненной сумме — штатный формат ОФД для
                    # скидок и округлений. Проверять непустоту ЯЧЕЙКИ мало:
                    # строка «0» непустая, и вся сумма терялась.
                    if not price and raw_sum is not None:
                        price = raw_sum / qty if qty else 0.0
                    price = price or 0.0
                    # возврат по документу — отдельный чек; отрицательная строка внутри
                    # обычного чека (скидка, корректировка) остаётся в том же чеке
                    is_return_doc = any(h in cell(row, "op").lower() for h in RETURN_HINTS)
                    negative = qty < 0 or price < 0 or (raw_sum is not None and raw_sum < 0)
                    sign = -1 if (is_return_doc or negative) else 1
                    qty, price = abs(qty), abs(price)
                    if sign < 0:
                        qty = -qty
                        returns += 1
                    # ключ группировки внутри файла
                    # отдельным чеком считаем только возврат по документу
                    kind = "R" if is_return_doc else "S"
                    if ext_id:
                        gkey = f"{ts[:16]}#{ext_id}#{kind}"
                    elif ts.endswith("T00:00:00"):
                        # дата без времени и без номера чека: считать весь день
                        # одним чеком нельзя — средний чек и число чеков уедут
                        # в разы. Каждая строка становится отдельным чеком.
                        gkey = f"row#{len(order)}#{kind}"
                    else:
                        gkey = f"ts#{ts}#{kind}"
                    if gkey not in groups:
                        groups[gkey] = {"ts": ts, "payment": payment, "ext_id": ext_id,
                                        "kind": kind, "items": [],
                                        "barista": cell(row, "barista") or None,
                                        "guest": cell(row, "guest") or None,
                                        "channel": _channel_of(cell(row, "channel"))}
                        order.append(gkey)
                    groups[gkey]["items"].append((name, qty, price))
                except Exception:
                    bad_row += 1
                    continue

        # ---------- 2-3. чистка и запись — ОДНОЙ транзакцией ----------
        # Раньше очистка коммитилась отдельно, до вставки: сбой в середине
        # импорта (нет места на диске, база занята) оставлял кофейню вообще
        # без истории продаж, откатывать было нечего.
        rows_added = items_added = dupes = updated = 0
        seen_bases = set()
        # Разбор позиции кэшируется: в выгрузке за месяц одна и та же фраза
        # «Латте 350 мл на овсяном» встречается тысячи раз.
        overrides = catalog_mod.owner_overrides(conn)
        parse_cache = {}

        def resolve(name):
            p = parse_cache.get(name)
            if p is None:
                p = catalog_mod.resolve(conn, name, overrides)
                parse_cache[name] = p
            return p

        try:
            # Первая загрузка реальных чеков поверх демо стирает демо, иначе сводки
            # смешают выдуманные цифры с настоящими. Ручные списания персонала при
            # этом сохраняются — их вводили люди, это реальные данные.
            demo_now = db.kv_get(conn, "demo_data") == "1"
            if groups and (reset or demo_now):
                for t in ("receipt_items", "receipts", "menu_items"):
                    conn.execute(f"DELETE FROM {t}")
                conn.execute("DELETE FROM waste WHERE src='demo'")
                conn.execute("DELETE FROM recipes WHERE src='auto'")
                conn.execute("DELETE FROM ingredients WHERE category IN ('case','goods')")
                overrides.clear()
                parse_cache.clear()
                conn.execute("INSERT INTO kv(key,value) VALUES('demo_data','0') "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value")

            # Существующие чеки: и по точному ключу, и сгруппированные по
            # (дата, номер, вид) — чтобы сопоставить выгрузки, где у одного и
            # того же чека разная точность времени.
            known, by_daynum = {}, defaultdict(list)
            for r in conn.execute(
                    "SELECT ext_id, id, ts FROM receipts WHERE ext_id IS NOT NULL"):
                known[r["ext_id"]] = r["id"]
                head = r["ext_id"].split("#")
                if len(head) >= 3 and not r["ext_id"].startswith("h#"):
                    by_daynum[(head[0], head[1], head[2][:1])].append(
                        {"key": r["ext_id"], "id": r["id"], "ts": r["ts"]})
            seen_hash = {}          # для чеков без номера: сколько одинаковых уже было
            for gkey in order:
                g = groups[gkey]
                kind = g.get("kind", "S")
                fingerprint = seq = ""
                if not g["ext_id"]:
                    fingerprint = "|".join(sorted(f"{n}:{q}:{p}" for n, q, p in g["items"]))
                    seq = seen_hash.get(fingerprint, 0)
                    seen_hash[fingerprint] = seq + 1
                key = _receipt_key(g["ext_id"], g["ts"], kind, fingerprint, seq)
                if g["ext_id"]:
                    # для чека с номером решение всегда принимает _match_existing:
                    # только оно умеет сравнивать время разной точности
                    rid, key = _match_existing(by_daynum, g, kind, key)
                else:
                    rid = known.get(key)
                if rid is not None:
                    touched = _merge_items(conn, rid, g["items"], resolve, seen_bases)
                    items_added += touched["added"]
                    updated += touched["updated"]
                    if not touched["added"] and not touched["updated"]:
                        dupes += 1
                    continue
                cur = conn.execute(
                    "INSERT INTO receipts(ts,payment,ext_id,barista,guest,channel) "
                    "VALUES(?,?,?,?,?,?)",
                    (g["ts"], g["payment"], key, g.get("barista"), g.get("guest"),
                     g.get("channel")))
                rid = cur.lastrowid
                known[key] = rid
                if g["ext_id"]:
                    head = key.split("#")
                    by_daynum[(head[0], head[1], head[2][:1])].append(
                        {"key": key, "id": rid, "ts": g["ts"]})
                rows_added += 1
                for name, qty, price in g["items"]:
                    seen_bases.add(_insert_item(conn, rid, name, qty, price, resolve)["base"])
                    items_added += 1
            # Каталог собирается из чеков сам. Витрина и товары заводятся ещё и
            # как закупаемые позиции — с неизвестной ценой, пока владелец её не
            # назвал: маржа «100%» была бы враньём.
            for p in parse_cache.values():
                catalog_mod.register(conn, p)
            conn.commit()
        except Exception:
            conn.rollback()          # либо загрузилось всё, либо база как была
            raise
        service = sum(1 for p in parse_cache.values()
                      if p["kind"] == "service")
        # без времени продажи не считаются час пик, распроданность и упущенная
        # выручка — об этом надо предупредить, а не молча показывать нули
        no_time = all(not _has_time(groups[k]["ts"]) for k in order) if order else False
        return {"receipts": rows_added, "items": items_added, "dupes": dupes,
                "updated": updated, "skipped": bad_row, "bad_date": bad_date,
                "returns": returns, "service_items": service,
                "no_payment_column": "payment" not in cols,
                "no_receipt_column": "receipt" not in cols,
                "no_time_column": no_time, "encoding": enc}
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Использование: python -m coffeeos.import_receipts выгрузка.csv [--reset]")
        sys.exit(1)
    try:
        res = import_csv(sys.argv[1], reset="--reset" in sys.argv)
    except ImportError_ as e:
        print("Ошибка:", e)
        sys.exit(1)
    print(f"Загружено чеков: {res['receipts']}, позиций: {res['items']}, "
          f"уточнено чеков: {res['updated']}, пропущено дублей: {res['dupes']}, "
          f"битых строк: {res['skipped']}, строк с непонятной датой: {res['bad_date']}")
