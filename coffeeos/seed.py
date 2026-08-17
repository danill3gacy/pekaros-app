"""Генератор реалистичных данных кофейни (90 дней чеков).

Нужен, чтобы система работала «из коробки» и чтобы каждую считалку можно было
проверить на данных, у которых известен правильный ответ. В боевом режиме
заменяется реальной выгрузкой: `python -m coffeeos import файл.csv`.

Что здесь моделируется всерьёз — и почему это важно:

1. **Два разных механизма продаж.** Напитки делаются на заказ и кончиться не
   могут. Витрина ставится конечной партией с утра, и когда партия кончилась,
   спрос уходит в никуда. Генератор с бесконечной витриной делает задачу
   прогноза искусственно лёгкой и прячет главную ошибку — недозаказ.

2. **Утренний пик.** Кофейня живёт с 8:00 до 10:00; там же она упирается в
   руки бариста. Поэтому поток в пик ограничен пропускной способностью —
   ровно так, как в жизни: очередь есть, а чеков больше не становится.

3. **Размеры, молоко, модификаторы, «с собой / в зале»** — иначе нечего
   разбирать и нечему считать себестоимость.

4. **Сезонность холодных напитков** — доля айса зависит от месяца.

5. **«Чутьё владельца» для витрины** — он реагирует на остатки резче, чем на
   распроданность (остатки лежат на столе и раздражают, а ушедший гость
   невидим). Равновесие устанавливается с систематическим недозаказом. Это и
   есть та потеря, которую продукт должен находить.
"""

import random
from datetime import datetime, timedelta

from . import catalog, config, db, menu

# ---------- меню демо-кофейни ----------
# (имя, вид, цена S/M/L либо одна цена, вес спроса)
DRINK_MENU = [
    ("Латте", (220, 260, 300), 100),
    ("Капучино", (200, 240, 280), 85),
    ("Американо", (150, 170, 190), 55),
    ("Флэт уайт", (250, 280, 310), 28),
    ("Раф", (280, 320, 360), 26),
    ("Эспрессо", (120, 120, 120), 18),
    ("Матча", (280, 320, 360), 16),
    ("Какао", (200, 230, 260), 12),
    ("Чай", (150, 170, 190), 22),
    ("Мокка", (270, 300, 340), 10),
    ("Колд брю", (260, 290, 320), 9),
    ("Горячий шоколад", (240, 270, 300), 8),
]

FOOD_MENU = [
    ("Круассан классический", 180, 60),
    ("Круассан миндальный", 240, 26),
    ("Синнабон", 260, 30),
    ("Чизкейк Нью-Йорк", 320, 22),
    ("Сырники", 340, 24),
    ("Сэндвич с курицей", 360, 20),
    ("Овсяная каша", 220, 14),
    ("Печенье овсяное", 90, 28),
    ("Брауни", 230, 16),
]

GOODS_MENU = [
    ("Кофе в зёрнах 250 г", 950, 3),
    ("Вода 0,5", 90, 12),
]

SIZE_WEIGHTS = [("S", 22), ("M", 52), ("L", 26)]
MILK_WEIGHTS = [
    (menu.MILK_REGULAR, 68),
    ("овсяное", 20),
    ("безлактозное", 6),
    ("миндальное", 4),
    ("кокосовое", 2),
]
MOD_WEIGHTS = [("", 74), ("сироп", 16), ("доп. шот", 6), ("взбитые сливки", 4)]

# Профиль часа: кофейня 07:00–21:00, утренний пик и обеденная волна
HOUR_WEIGHTS = {
    7: 0.35,
    8: 1.00,
    9: 0.92,
    10: 0.55,
    11: 0.45,
    12: 0.62,
    13: 0.68,
    14: 0.48,
    15: 0.40,
    16: 0.44,
    17: 0.52,
    18: 0.46,
    19: 0.30,
    20: 0.18,
}
DOW_FACTOR = [1.06, 1.04, 1.02, 1.05, 1.12, 0.86, 0.74]  # Пн..Вс: будни рабочего района

# Сколько чеков в час физически успевает пробить один бариста.
# Утром поток упирается в это число, а не в спрос — и в реальности тоже.
CAPACITY_PER_HOUR = 34

# Доля холодных напитков по месяцам (Янв..Дек)
ICED_BY_MONTH = [0.06, 0.06, 0.09, 0.14, 0.24, 0.36, 0.44, 0.42, 0.28, 0.15, 0.08, 0.06]

BARISTAS = ["Аня", "Марк", "Лиза"]

# «Чутьё владельца» при заказе витрины
CASE_UP_ON_SELLOUT = 1.03  # кончилось — завтра берём чуть больше
CASE_DOWN_ON_LEFTOVER = 0.94  # много осталось — завтра берём заметно меньше
LEFTOVER_TOLERANCE = 0.05


def _pick(weighted, rnd):
    names = [x[0] for x in weighted]
    weights = [x[1] for x in weighted]
    return rnd.choices(names, weights=weights, k=1)[0]


def _drink_name(base, size, milk, mods, iced):
    parts = []
    if iced:
        parts.append("Айс")
    parts.append(base if not iced else base.lower())
    parts.append({"S": "250", "M": "350", "L": "450"}[size])
    name = " ".join(parts) + " мл"
    if milk and milk != menu.MILK_REGULAR:
        name += f" на {milk[:-2]}ом" if milk.endswith("ое") else f" на {milk}"
    if mods == "сироп":
        name += " + сироп карамель"
    elif mods == "доп. шот":
        name += " + доп. шот"
    elif mods == "взбитые сливки":
        name += " + взбитые сливки"
    return name


def seed(days=90, base_checks=210, seed_val=42, reset=True):
    rnd = random.Random(seed_val)
    conn = db.get_conn()
    db.init_db(conn)
    if reset:
        for t in ("receipt_items", "receipts", "menu_items", "case_order_override", "stock_counts"):
            conn.execute(f"DELETE FROM {t}")
        # Ручные списания персонала не трогаем: их вводили люди, это реальные
        # данные. Импорт реальных чеков бережёт их точно так же.
        conn.execute("DELETE FROM waste WHERE src='demo'")
        # позиции витрины и товаров, заведённые автоматически прошлым прогоном
        conn.execute("DELETE FROM recipes WHERE src='auto'")
        conn.execute("DELETE FROM ingredients WHERE category IN ('case','goods')")
        conn.commit()

    today = config.today()
    drink_price = {n: p for n, p, _w in DRINK_MENU}
    drink_weight = [(n, w) for n, _p, w in DRINK_MENU]
    parse_cache = {}
    food_price = {n: p for n, p, _ in FOOD_MENU}
    # сколько владелец в принципе ставит этой позиции в средний день
    target = {n: float(w) for n, _p, w in FOOD_MENU}
    overrides = None

    for d in range(days, 0, -1):
        day = today - timedelta(days=d)
        iced_p = ICED_BY_MONTH[day.month - 1]
        factor = DOW_FACTOR[day.weekday()] * rnd.uniform(0.9, 1.1)
        n_checks = int(base_checks * factor)
        barista = BARISTAS[(day.toordinal() // 2) % len(BARISTAS)]

        # ---- сколько владелец решил поставить в витрину ----
        stock, placed = {}, {}
        for name, _price, _w in FOOD_MENU:
            want = (
                target[name]
                * (DOW_FACTOR[day.weekday()] / (sum(DOW_FACTOR) / 7))
                * rnd.uniform(0.92, 1.06)
            )
            stock[name] = placed[name] = max(1, int(round(want)))

        # ---- поток гостей: время генерируем, затем режем по пропускной способности ----
        hours = list(HOUR_WEIGHTS)
        hweights = list(HOUR_WEIGHTS.values())
        events = sorted(
            (rnd.choices(hours, weights=hweights, k=1)[0], rnd.randint(0, 59))
            for _ in range(n_checks)
        )
        served, per_hour = [], {}
        for hour, minute in events:
            if per_hour.get(hour, 0) >= CAPACITY_PER_HOUR:
                continue  # очередь: гость не дождался и ушёл, чека нет
            per_hour[hour] = per_hour.get(hour, 0) + 1
            served.append((hour, minute))

        for hour, minute in served:
            ts = datetime(day.year, day.month, day.day, hour, minute)
            bought = []

            # напиток берут почти всегда — за ним и пришли
            if rnd.random() < 0.94:
                base = _pick(drink_weight, rnd)
                # эспрессо не наливают в 450 мл — размер у него всегда один
                size = "S" if base == "Эспрессо" else _pick(SIZE_WEIGHTS, rnd)
                row = menu.DRINK_BY_NAME.get(base)
                milk = _pick(MILK_WEIGHTS, rnd) if (row and row[3]) else None
                mods = _pick(MOD_WEIGHTS, rnd)
                iced = 1 if rnd.random() < iced_p else 0
                price = drink_price[base][{"S": 0, "M": 1, "L": 2}[size]]
                if milk and milk != menu.MILK_REGULAR:
                    price += 60  # наценка за альтернативное молоко
                if mods:
                    price += 50
                bought.append((_drink_name(base, size, milk, mods, iced), 1, price))
                if rnd.random() < 0.06:  # второй напиток «на двоих»
                    bought.append((_drink_name(base, size, None, "", iced), 1, price))

            # еда — только если есть в витрине; утром берут заметно охотнее
            attach_p = 0.34 if hour <= 10 else 0.19
            if rnd.random() < attach_p:
                choices = [(n, w) for n, _p, w in FOOD_MENU if stock.get(n, 0) > 0]
                if choices:
                    name = _pick(choices, rnd)
                    stock[name] -= 1
                    bought.append((name, 1, food_price[name]))

            if rnd.random() < 0.03:
                name, price, _w = rnd.choices(GOODS_MENU, weights=[g[2] for g in GOODS_MENU])[0]
                bought.append((name, 1, price))

            if not bought:
                continue

            payment = "cash" if rnd.random() < 0.18 else "card"
            channel = "takeaway" if rnd.random() < 0.72 else "here"
            guest = f"card-{rnd.randint(1, 420):04d}" if rnd.random() < 0.35 else None
            cur = conn.execute(
                "INSERT INTO receipts(ts,payment,barista,guest,channel) VALUES(?,?,?,?,?)",
                (ts.isoformat(), payment, barista, guest, channel),
            )
            rid = cur.lastrowid
            if overrides is None:
                overrides = catalog.owner_overrides(conn)
            for name, qty, price in bought:
                p = parse_cache.get(name)
                if p is None:
                    p = catalog.resolve(conn, name, overrides)
                    catalog.register(conn, p)
                    overrides.setdefault(p["base"], (p["kind"], p["category"]))
                    parse_cache[name] = p
                conn.execute(
                    "INSERT INTO receipt_items"
                    "(receipt_id,name,base,category,kind,size,volume_ml,milk,mods,iced,qty,price)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        rid,
                        name,
                        p["base"],
                        p["category"],
                        p["kind"],
                        p["size"],
                        p["volume_ml"],
                        p["milk"],
                        p["mods"],
                        p["iced"],
                        qty,
                        price,
                    ),
                )

        # ---- что осталось в витрине, то и списали; владелец корректирует чутьё ----
        for name, left in stock.items():
            if left > 0:
                conn.execute(
                    "INSERT INTO waste(date,name,qty,unit,amount,kind,src) "
                    "VALUES(?,?,?,'шт',?,'food','demo')",
                    (day.isoformat(), name, left, left * food_price[name]),
                )
            if left <= 0:
                target[name] *= CASE_UP_ON_SELLOUT
            elif left > placed[name] * LEFTOVER_TOLERANCE:
                target[name] *= CASE_DOWN_ON_LEFTOVER

        # ---- вылитое молоко: бариста отмечает не каждый день ----
        if rnd.random() < 0.45:
            conn.execute(
                "INSERT INTO waste(date,name,qty,unit,amount,kind,src) "
                "VALUES(?,'Молоко обычное',?,'л',?,'milk','demo')",
                (day.isoformat(), round(rnd.uniform(0.4, 2.2), 1), 0),
            )

    # цены закупки витрины: в демо владелец их «уже ввёл», иначе маржа не считается
    for name, price, _w in FOOD_MENU:
        conn.execute(
            "UPDATE ingredients SET pack_price=?, price_src='owner' WHERE name=?",
            (round(price * 0.42), name),
        )
    for name, price, _w in GOODS_MENU:
        conn.execute(
            "UPDATE ingredients SET pack_price=?, price_src='owner' WHERE name=?",
            (round(price * 0.55), name),
        )
    # деньги на вылитом молоке считаем по себестоимости, когда цены уже проставлены
    _reprice_milk_waste(conn)
    db.kv_set(conn, "demo_data", "1")  # отметка: в базе демо-данные, не реальные
    conn.commit()
    total = conn.execute("SELECT COUNT(*) c FROM receipts").fetchone()["c"]
    conn.close()
    return total


def _reprice_milk_waste(conn):
    row = conn.execute(
        "SELECT pack_price, pack_qty FROM ingredients WHERE name='Молоко обычное'"
    ).fetchone()
    if not row or not row["pack_qty"]:
        return
    per_litre = (row["pack_price"] or 0) * 1000 / row["pack_qty"]
    conn.execute("UPDATE waste SET amount = qty * ? WHERE kind='milk' AND src='demo'", (per_litre,))


if __name__ == "__main__":
    print(f"Сгенерировано чеков: {seed()}")
