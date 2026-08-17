"""Каталог меню и списания.

Каталог собирается из чеков сам: заносить позиции руками не нужно. Но
разобрать их автоматически на 100% нельзя — «Тарталетка дня» не опознается
ни как еда, ни как напиток. Поэтому владелец может поправить любую позицию
одной фразой, и его решение важнее автоматики.

Списания сознательно НЕ обязательны. Заказ витрины считается по чекам и
работает без них — это проверяется тестом. Но если бариста отметил вылитое
молоко или оставшиеся сырники, картина становится точнее: списания снимают
ложный признак распроданности и показывают реальную потерю на молоке, которую
иначе не увидеть никак — вылитое молоко в кассе не отражается вообще.
"""
from . import config, menu
from .analytics import day_str

MIN_STEM = 3          # по одной-двум буквам позиция не опознаётся
MAX_WASTE_QTY = 1000  # больше тысячи единиц за день одна точка не спишет


# ---------- разбор и регистрация позиции ----------
def resolve(conn, name, overrides=None):
    """Разобрать позицию чека, уважая решения владельца.

    Автомат ошибается: «Тарталетка дня» не опознаётся ни как еда, ни как
    напиток. Владелец поправляет вид позиции один раз, и с этого момента его
    решение важнее словаря — в том числе при загрузке новых выгрузок. Раньше
    (в пекарной версии) флаг ставился только при первом импорте, и исправить
    его было нельзя.
    """
    parsed = menu.parse(name)
    if overrides is None:
        row = conn.execute("SELECT kind, category FROM menu_items WHERE name=?",
                           (parsed["base"],)).fetchone()
        override = (row["kind"], row["category"]) if row else None
    else:
        override = overrides.get(parsed["base"])
    if override:
        kind, category = override
        if kind:
            parsed["kind"] = kind
        if category:
            parsed["category"] = category
    return parsed


def rescan(conn):
    """Перебрать разбор всех уже загруженных позиций заново.

    Нужно, когда словарь напитков пополнился или владелец поправил вид позиции:
    иначе исправление подействовало бы только на новые чеки, а вся история
    осталась бы разобранной по-старому. Перезагружать выгрузки ради этого
    неправильно — дедупликация есть, но данные могли прийти из кассы по API и
    файла на диске уже не быть.
    """
    overrides = owner_overrides(conn)
    cache, changed = {}, 0
    for r in conn.execute("SELECT DISTINCT name FROM receipt_items"):
        name = r["name"]
        p = resolve(conn, name, overrides)
        cache[name] = p
    for name, p in cache.items():
        cur = conn.execute(
            "UPDATE receipt_items SET base=?, category=?, kind=?, size=?, volume_ml=?, "
            "milk=?, mods=?, iced=? WHERE name=?",
            (p["base"], p["category"], p["kind"], p["size"], p["volume_ml"],
             p["milk"], p["mods"], p["iced"], name))
        changed += cur.rowcount
        register(conn, p)
    conn.commit()
    return {"names": len(cache), "rows": changed}


def owner_overrides(conn):
    """Один запрос вместо запроса на каждую строку выгрузки."""
    return {r["name"]: (r["kind"], r["category"])
            for r in conn.execute("SELECT name, kind, category FROM menu_items")}


def register(conn, parsed):
    """Занести позицию в каталог. Витрина и товары заводятся ещё и как
    закупаемые ингредиенты — с НЕИЗВЕСТНОЙ ценой, пока владелец её не назвал."""
    stocked = 1 if parsed["kind"] == menu.KIND_FOOD else 0
    conn.execute(
        "INSERT OR IGNORE INTO menu_items(name,category,kind,stocked) VALUES(?,?,?,?)",
        (parsed["base"], parsed["category"], parsed["kind"], stocked))
    if parsed["kind"] in (menu.KIND_FOOD, menu.KIND_GOODS):
        from . import costing
        costing.ensure_purchased_item(conn, parsed["base"], parsed["kind"])


def catalog(conn):
    """Каталог с видом позиции — чтобы владелец мог его проверить."""
    return [{"name": r["name"], "category": r["category"], "kind": r["kind"],
             "stocked": bool(r["stocked"])}
            for r in conn.execute(
                "SELECT name, category, kind, stocked FROM menu_items "
                "ORDER BY kind, name")]


def match_names(names, text):
    """Сопоставить фразу со списком названий.

    Возвращает {"name": единственное совпадение либо None, "options": кандидаты}.

    Ключевое здесь — ЧЕСТНАЯ НЕОДНОЗНАЧНОСТЬ. Если в витрине два круассана, а
    бариста написал «списание круассан 3», прежняя версия молча выбирала один
    из них — и не тот. Последствие хуже, чем кажется: списание гасит признак
    распроданности, поэтому по настоящему кончившемуся круассану упущенная
    выручка переставала считаться, а по невиновному росли «остатки».
    Правильный ответ здесь — переспросить.
    """
    phrase = " ".join((text or "").strip().lower().split())
    if len(phrase) < MIN_STEM:
        return {"name": None, "options": []}
    exact = next((n for n in names if n.lower() == phrase), None)
    if exact:
        return {"name": exact, "options": [exact]}
    words = phrase.split()
    hits = [n for n in names if all(_word_in(w, n.lower()) for w in words)]
    if not hits:
        stem = words[0][:5]
        hits = [n for n in names
                if any(w.startswith(stem) for w in n.lower().split()) or stem in n.lower()]
    if len(hits) == 1:
        return {"name": hits[0], "options": hits}
    return {"name": None, "options": sorted(hits)}


def find_item(conn, text, kind=None):
    """Найти позицию меню по куску названия («сырни» -> «Сырники»).

    При неоднозначности возвращает None: выбирать за владельца нельзя.
    Кому нужен список вариантов — зовёт match_menu().
    """
    return match_menu(conn, text, kind)["name"]


def match_menu(conn, text, kind=None):
    sql = "SELECT name FROM menu_items"
    args = ()
    if kind:
        sql += " WHERE kind=?"
        args = (kind,)
    names = [r["name"] for r in conn.execute(sql + " ORDER BY name", args)]
    return match_names(names, text)


# Короткие слова, которые сами по себе неоднозначны. Бариста пишет «молоко»,
# а в справочнике их пять; без явного правила побеждало первое по алфавиту —
# «Молоко безлактозное», и списание уходило не туда.
INGREDIENT_ALIASES = {
    "молоко": "Молоко обычное",
    "молока": "Молоко обычное",
    "зерно": "Зерно кофе",
    "зерна": "Зерно кофе",
    "кофе": "Зерно кофе",
    "стаканы": "Стакан M",
    "крышки": "Крышка",
}


def match_ingredient(conn, text):
    """Найти ингредиент по куску названия («овсяное» -> «Молоко овсяное»).

    Сначала сопоставляется ВСЯ фраза («молоко обычное»), и только потом первое
    слово. Иначе «списал молоко овсяное 2» списывалось с обычного молока —
    ошибка, которую владелец заметил бы только по расхождению в закупке.
    """
    phrase = " ".join((text or "").strip().lower().split())
    names = [r["name"] for r in conn.execute("SELECT name FROM ingredients ORDER BY name")]
    if phrase in INGREDIENT_ALIASES and INGREDIENT_ALIASES[phrase] in names:
        return {"name": INGREDIENT_ALIASES[phrase], "options": [INGREDIENT_ALIASES[phrase]]}
    return match_names(names, phrase)


def find_ingredient(conn, text):
    return match_ingredient(conn, text)["name"]


def _word_in(word, name):
    """Слово встречается в названии хотя бы своей основой."""
    stem = word[:5]
    return any(w.startswith(stem) for w in name.split()) or stem in name


def set_stocked(conn, name, stocked):
    """Отметить, стоит ли позиция в витрине с конечным запасом.

    Только такие позиции попадают в заказ на завтра и в поиск упущенной
    выручки. Напиток сюда попасть не может: он делается на заказ.
    """
    m = match_menu(conn, name)
    if not m["name"] and len(m["options"]) > 1:
        return {"ambiguous": m["options"]}
    nm = m["name"]
    if nm is None:
        return None
    row = conn.execute("SELECT kind FROM menu_items WHERE name=?", (nm,)).fetchone()
    if stocked and row and row["kind"] != menu.KIND_FOOD:
        return {"name": nm, "error": "not_food", "kind": row["kind"]}
    conn.execute("UPDATE menu_items SET stocked=? WHERE name=?", (1 if stocked else 0, nm))
    conn.commit()
    return {"name": nm, "stocked": bool(stocked)}


def set_kind(conn, name, kind):
    """Переопределить вид позиции (напиток / витрина / товар / служебное)."""
    if kind not in (menu.KIND_DRINK, menu.KIND_FOOD, menu.KIND_GOODS,
                    menu.KIND_ADDON, menu.KIND_SERVICE):
        return None
    nm = find_item(conn, name)
    if nm is None:
        return None
    stocked = 1 if kind == menu.KIND_FOOD else 0
    conn.execute("UPDATE menu_items SET kind=?, stocked=? WHERE name=?", (kind, stocked, nm))
    conn.execute("UPDATE receipt_items SET kind=? WHERE base=?", (kind, nm))
    conn.commit()
    return {"name": nm, "kind": kind}


def set_category(conn, name, category):
    """Переназначить категорию (и в каталоге, и в уже загруженных чеках)."""
    nm = find_item(conn, name)
    if nm is None:
        return None
    conn.execute("UPDATE menu_items SET category=? WHERE name=?", (category, nm))
    conn.execute("UPDATE receipt_items SET category=? WHERE base=?", (category, nm))
    conn.commit()
    return nm


# ---------- списания ----------
def _unit_for_ingredient(row):
    """В чём владелец называет количество: литры, килограммы или штуки."""
    return {"ml": ("л", 1000.0), "g": ("кг", 1000.0)}.get(row["unit"], ("шт", 1.0))


def add_waste(conn, name, qty, day=None):
    """Записать списание: витрины (в штуках) или сырья (в литрах/килограммах).

    Возвращает None, если позиция не опознана или количество неправдоподобно:
    мусорная строка не безобидна — по ней система считает день «не
    распроданным» и перестаёт видеть упущенную выручку.
    """
    day = day or config.today()
    try:
        qty = float(qty)
    except (TypeError, ValueError):
        return None
    if not (0 < qty <= MAX_WASTE_QTY):
        return None
    phrase = " ".join(name.strip().lower().split())
    if len(phrase) < MIN_STEM:
        return None

    # 1) витрина: позиция меню. Неоднозначность — не повод угадывать: вернём
    # варианты, и бот переспросит.
    m = match_menu(conn, phrase, kind=menu.KIND_FOOD)
    if not m["name"] and len(m["options"]) > 1:
        return {"ambiguous": m["options"]}
    nm = m["name"]
    if nm is not None:
        price = conn.execute(
            "SELECT price FROM receipt_items WHERE base=? AND qty>0 ORDER BY id DESC LIMIT 1",
            (nm,)).fetchone()
        unit_price = price["price"] if price else 0
        conn.execute(
            "INSERT INTO waste(date,name,qty,unit,amount,kind) VALUES(?,?,?,'шт',?,'food')",
            (day_str(day), nm, qty, qty * unit_price))
        conn.commit()
        return {"name": nm, "qty": qty, "unit": "шт", "amount": qty * unit_price,
                "kind": "food"}

    # 2) сырьё: молоко и прочие ингредиенты — по себестоимости
    mi = match_ingredient(conn, phrase)
    if not mi["name"] and len(mi["options"]) > 1:
        return {"ambiguous": mi["options"]}
    row = conn.execute("SELECT * FROM ingredients WHERE name=?", (mi["name"],)).fetchone() \
        if mi["name"] else None
    if row is not None:
        unit_name, per_unit = _unit_for_ingredient(row)
        base_qty = qty * per_unit                       # в единицах хранения (мл, г, шт)
        pack = row["pack_qty"] or 1
        amount = base_qty * (row["pack_price"] or 0) / pack
        kind = "milk" if (row["category"] == "dairy" and "олок" in row["name"].lower()) \
            else "ingredient"
        conn.execute(
            "INSERT INTO waste(date,name,qty,unit,amount,kind) VALUES(?,?,?,?,?,?)",
            (day_str(day), row["name"], qty, unit_name, amount, kind))
        conn.commit()
        return {"name": row["name"], "qty": qty, "unit": unit_name, "amount": amount,
                "kind": kind, "estimated": row["price_src"] != "owner"}
    return None


def waste_report(conn, day):
    """Списания за день, раздельно по витрине и по сырью.

    Разделение не косметическое: витрина считается по ЦЕННИКУ (это
    непроданная выручка), а вылитое молоко — по СЕБЕСТОИМОСТИ (это прямой
    расход). Сложить их в одну сумму значило бы сравнить разные вещи.
    """
    ds = day_str(day)
    rows = conn.execute(
        "SELECT name, kind, unit, SUM(qty) qty, SUM(amount) amount FROM waste WHERE date=? "
        "GROUP BY name, kind, unit ORDER BY amount DESC", (ds,)).fetchall()
    items = [{"name": x["name"], "kind": x["kind"], "unit": x["unit"],
              "qty": x["qty"], "amount": x["amount"] or 0} for x in rows]
    case = [i for i in items if i["kind"] == "food"]
    raw = [i for i in items if i["kind"] != "food"]
    case_total = sum(i["amount"] for i in case)
    raw_total = sum(i["amount"] for i in raw)
    from .analytics import sales_summary
    sales = sales_summary(conn, day)
    rev = sales["revenue"] or 0
    return {"items": items, "case": case, "raw": raw,
            "case_total": case_total, "raw_total": raw_total,
            "total": case_total + raw_total,
            "case_pct": (case_total / rev * 100) if rev else 0,
            "raw_pct": (raw_total / rev * 100) if rev else 0,
            # совместимость с прежним именем поля в интерфейсах
            "pct": ((case_total + raw_total) / rev * 100) if rev else 0}


def milk_waste(conn, days=56, upto=None):
    """Сколько молока выливается — деньги, которых нет ни в кассе, ни в ОФД.

    В кофейне это одна из двух главных утечек (вторая — пустая витрина):
    недопенил, перегрел, вспенил на латте вместо капучино — литр за смену это
    обычное дело, а за месяц набегает заметная сумма.
    """
    from datetime import timedelta

    from .analytics import last_day_with_data
    upto = upto or last_day_with_data(conn)
    start = upto - timedelta(days=days - 1)
    row = conn.execute(
        "SELECT SUM(qty) q, SUM(amount) a, COUNT(DISTINCT date) d FROM waste "
        "WHERE date BETWEEN ? AND ? AND kind='milk'",
        (day_str(start), day_str(upto))).fetchone()
    ndays = row["d"] or 0
    if not ndays:
        return {"tracked": False,
                "note": "вылитое молоко не отмечают — эта потеря не видна нигде, "
                        "её можно отметить одной фразой: «вылил молоко 1,5»"}
    return {"tracked": True, "days": ndays,
            "litres_per_day": round((row["q"] or 0) / ndays, 2),
            "money_per_day": round((row["a"] or 0) / ndays),
            "money_per_month": round((row["a"] or 0) / ndays * 30)}
