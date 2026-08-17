"""Проверка утверждений КофейняОС на независимом потоке гостей.

Зачем это в поставке: любое утверждение «наша система экономит вам деньги»
должно быть проверяемым. Здесь оно проверяется — и владелец, и скептик могут
запустить одну команду и увидеть цифры, в том числе неудобные.

    python tools/simulate.py                 # 90 дней, четыре стратегии витрины
    python tools/simulate.py --days 180 --seeds 5
    python tools/simulate.py --supply        # ещё и проверка заявки поставщику

Проверяются ровно два утверждения продукта:

**1. Заказ витрины выгоднее «чутья» и выгоднее таблички со средним.**
Генерируется поток гостей (истинный спрос). Каждая стратегия получает РОВНО ТОТ
ЖЕ поток, но сама решает, сколько ставить в витрину. Видит она только то, что
реально продала — как в жизни: распроданный спрос не виден никому.

**2. Заявка поставщику не даёт встать без зерна и молока.**
Расход считается по проданным чашкам, заявка формируется раз в неделю, поставка
идёт со своим сроком. Считается, сколько дней хотя бы один ингредиент был в нуле.

Модель спроса здесь СВОЯ, а не импортированная из coffeeos/seed.py. Пока
симулятор брал профиль часов и дней недели у генератора демо-данных, он
проверял алгоритм на том же мире, из которого этот алгоритм вырос, и
опровергнуть себя не мог в принципе. Ниже — независимые профили: обеденный пик
вместо утреннего, другой недельный ритм, сезонный дрейф и редкие всплески.
"""
import argparse
import math
import os
import random
import sqlite3
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from coffeeos import catalog, costing, db, demand, economics, supply  # noqa: E402
from coffeeos.analytics import day_str, last_day_with_data  # noqa: E402

# --- независимая модель спроса: кофейня в деловом квартале с обеденным пиком ---
HOUR_WEIGHTS = {7: 0.25, 8: 0.55, 9: 0.60, 10: 0.62, 11: 0.70, 12: 0.95, 13: 1.00,
                14: 0.72, 15: 0.58, 16: 0.66, 17: 0.90, 18: 0.85, 19: 0.60, 20: 0.30}
DOW_FACTOR = [0.95, 0.90, 0.98, 1.10, 1.35, 1.28, 0.88]   # Пн..Вс, пик в пятницу
SEASON_DRIFT = 0.0035      # медленный рост спроса за период (≈ +0.35% в день)
SPIKE_CHANCE = 0.04        # доля дней с всплеском (ярмарка, праздник)
SPIKE_FACTOR = 1.8

# Поведение владельца «на чутьё» — тоже своё, чтобы точка отсчёта не совпадала
# с той, что зашита в генераторе демо-данных.
CASE_UP_ON_SELLOUT = 1.04
CASE_DOWN_ON_LEFTOVER = 0.93
LEFTOVER_TOLERANCE = 0.05

WARMUP = 21

# (имя, цена, средний дневной спрос)
CASE_MENU = [
    ("Круассан классический", 180, 26),
    ("Круассан миндальный",   240, 11),
    ("Синнабон",              260, 13),
    ("Чизкейк Нью-Йорк",      320,  9),
    ("Сырники",               340, 10),
    ("Сэндвич с курицей",     360,  9),
    ("Печенье овсяное",        90, 12),
    ("Брауни",                230,  7),
]
DRINK_MENU = [
    ("Латте 350", 260, 100), ("Капучино 250", 200, 80), ("Американо 350", 170, 50),
    ("Флэт уайт 350", 280, 25), ("Раф 350", 320, 20), ("Чай 350", 170, 18),
]
PRICE = {n: p for n, p, _ in CASE_MENU}
AVG = {n: a for n, _p, a in CASE_MENU}


# ---------- поток гостей (одинаковый для всех стратегий) ----------
def demand_stream(days, base_checks, seed_val):
    """Список дней; каждый день — упорядоченные покупки.

    Это ИСТИННЫЙ спрос: чего гость хотел, независимо от того, было ли оно.
    """
    rnd = random.Random(seed_val)
    hours = list(HOUR_WEIGHTS)
    hweights = list(HOUR_WEIGHTS.values())
    drink_w = [w for _n, _p, w in DRINK_MENU]
    case_w = [w for _n, _p, w in CASE_MENU]
    today = date.today()
    out = []
    for idx, d in enumerate(range(days, 0, -1)):
        day = today - timedelta(days=d)
        drift = 1.0 + SEASON_DRIFT * idx
        spike = SPIKE_FACTOR if rnd.random() < SPIKE_CHANCE else 1.0
        n_checks = int(base_checks * DOW_FACTOR[day.weekday()] * drift * spike
                       * rnd.uniform(0.85, 1.15))
        checks = []
        for _ in range(n_checks):
            hour = rnd.choices(hours, weights=hweights, k=1)[0]
            drink = rnd.choices(DRINK_MENU, weights=drink_w, k=1)[0]
            want_food = rnd.random() < (0.34 if hour <= 11 else 0.20)
            food = rnd.choices(CASE_MENU, weights=case_w, k=1)[0] if want_food else None
            checks.append((hour, rnd.randint(0, 59), rnd.random() < 0.20, drink, food))
        checks.sort(key=lambda x: (x[0], x[1]))
        out.append((day, checks))
    return out


# ---------- стратегии ----------
def policy_gut(conn, day, state):
    """Чутьё владельца: ставим по ощущению, режем, если много осталось."""
    avg_dow = sum(DOW_FACTOR) / 7
    target = state.setdefault("target", {n: float(a) for n, a in AVG.items()})
    rnd = state.setdefault("rnd", random.Random(1))
    return {n: max(1, int(round(target[n] * (DOW_FACTOR[day.weekday()] / avg_dow)
                                * rnd.uniform(0.92, 1.06)))) for n in target}


def gut_feedback(state, placed, left):
    """Владелец видит остатки и не видит ушедших — отсюда систематический недозаказ."""
    target = state.get("target")
    if not target:
        return
    for n, b in placed.items():
        if n not in target:
            continue
        if left.get(n, 0) <= 0:
            target[n] *= CASE_UP_ON_SELLOUT
        elif left[n] > b * LEFTOVER_TOLERANCE:
            target[n] *= CASE_DOWN_ON_LEFTOVER


def policy_average(conn, day, state):
    """Табличка в Excel: среднее за две недели. Самый честный конкурент.

    Именно так считает большинство: без поправки на распроданность и без
    запаса. Сравниваться надо с этим, а не с собственной прошлой версией,
    которой ни у кого нет.
    """
    last = last_day_with_data(conn)
    start = last - timedelta(days=13)
    nd = conn.execute(
        "SELECT COUNT(DISTINCT substr(ts,1,10)) d FROM receipts "
        "WHERE substr(ts,1,10) BETWEEN ? AND ?",
        (day_str(start), day_str(last))).fetchone()["d"] or 1
    rows = conn.execute(
        """SELECT i.base n, SUM(i.qty) q FROM receipts r JOIN receipt_items i
           ON i.receipt_id=r.id WHERE substr(r.ts,1,10) BETWEEN ? AND ?
              AND i.kind='food' GROUP BY i.base""",
        (day_str(start), day_str(last))).fetchall()
    return {r["n"]: max(0, int(round((r["q"] or 0) / nd))) for r in rows}


def policy_coffeeos(conn, day, state):
    return {i["name"]: i["recommended"] for i in demand.case_order(conn, day)["items"]}


# ---------- прогон одной стратегии ----------
def run_policy(name, plan_fn, stream, cost_share, record_waste=True):
    # cost_share — ВНЕШНЕЕ допущение только для этого бенчмарка: оно нужно,
    # чтобы перевести штуки в рубли и честно сравнить стратегии.
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)

    state, gut_state = {}, {}
    revenue = wasted_units = lost_units = lost_money = waste_money = 0.0
    covered = graded = 0
    rid = 0
    for idx, (day, checks) in enumerate(stream):
        if idx < WARMUP or plan_fn is policy_gut:
            placed = policy_gut(conn, day, gut_state)
        else:
            try:
                placed = plan_fn(conn, day, state) or {}
            except Exception as e:                  # стратегия не должна ронять прогон
                print(f"  [{name}] {day}: {e!r}", file=sys.stderr)
                placed = policy_gut(conn, day, gut_state)
            for n in AVG:                           # страховка на новые позиции
                placed.setdefault(n, max(1, int(AVG[n] * 0.5)))
        stock = dict(placed)

        for hour, minute, is_cash, drink, food in checks:
            bought = [(drink[0], 1, drink[1])]
            revenue += drink[1]
            if food is not None:
                n, price, _w = food
                if stock.get(n, 0) > 0:
                    stock[n] -= 1
                    bought.append((n, 1, price))
                    revenue += price
                else:
                    # гость хотел завтрак и не получил его: это и есть та потеря,
                    # которой нет ни в кассе, ни в ОФД
                    lost_units += 1
                    lost_money += price * (1 - cost_share)
            rid += 1
            ts = datetime(day.year, day.month, day.day, hour, minute).isoformat()
            cur = conn.execute("INSERT INTO receipts(ts,payment,ext_id) VALUES(?,?,?)",
                               (ts, "cash" if is_cash else "card", f"s{rid}"))
            for n, qty, price in bought:
                p = catalog.resolve(conn, n)
                catalog.register(conn, p)
                conn.execute(
                    "INSERT INTO receipt_items"
                    "(receipt_id,name,base,category,kind,size,volume_ml,milk,mods,iced,qty,price)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (cur.lastrowid, n, p["base"], p["category"], p["kind"], p["size"],
                     p["volume_ml"], p["milk"], p["mods"], p["iced"], qty, price))

        # фактический уровень сервиса: доля (позиция, день), где хватило на всех
        if idx >= WARMUP:
            for n in placed:
                graded += 1
                covered += 1 if stock.get(n, 0) > 0 else 0

        left = {n: stock.get(n, 0) for n in placed}
        for n, q in left.items():
            if q > 0:
                wasted_units += q
                waste_money += q * PRICE.get(n, 0) * cost_share
                if record_waste:
                    conn.execute(
                        "INSERT INTO waste(date,name,qty,unit,amount,kind,src) "
                        "VALUES(?,?,?,'шт',?,'food','demo')",
                        (day.isoformat(), n, q, q * PRICE.get(n, 0)))
        gut_feedback(gut_state, placed, left)
        conn.commit()
        demand._DEMAND_CACHE.clear()

    gross = revenue * (1 - cost_share) - waste_money
    conn.close()
    return {"name": name, "revenue": revenue, "gross": gross, "waste_money": waste_money,
            "waste_units": wasted_units, "lost_units": lost_units, "lost_money": lost_money,
            "service": (covered / graded) if graded else 0.0}


# ---------- проверка заявки поставщику ----------
def run_supply_check(stream, horizon=7):
    """Проверить, что заявка не даёт кофейне встать без зерна и молока.

    Проверяется ровно то, что делает владелец: каждый день смотрит заявку и
    заказывает то, что она пометила как «горит» или «скоро». Поставка приезжает
    через lead_days. Каждый день расход списывается со склада.

    Первые дни склад наполнен: кофейня не открывается с нулевым остатком, и
    считать стокауты разогрева было бы подтасовкой в обратную сторону.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)

    ing = costing.ingredients(conn)
    recipes = costing.recipe_rows(conn)
    watched = [n for n, r in ing.items()
               if r["category"] in ("coffee", "dairy", "syrup", "packaging")]
    stock = dict.fromkeys(watched, 0.0)
    incoming = {}          # день прибытия -> {ингредиент: количество}
    stockouts = dict.fromkeys(watched, 0)
    ordered_packs = dict.fromkeys(watched, 0)
    rid = 0

    # стартовый запас: 10 дней ожидаемого расхода (кофейня открылась не пустой)
    warm_days = max(1, min(WARMUP, len(stream)))
    for _day, checks in stream[:warm_days]:
        for _h, _m, _c, drink, food in checks:
            p = menu_parse(drink[0])
            for ingredient, amount in costing.portion_ingredients(
                    recipes, p["base"], p["size"], p["milk"], p["mods"], p["iced"]).items():
                if ingredient in stock:
                    stock[ingredient] += amount * 10.0 / warm_days

    for idx, (day, checks) in enumerate(stream):
        for name, qty in incoming.pop(day, {}).items():
            stock[name] = stock.get(name, 0) + qty

        for hour, minute, is_cash, drink, food in checks:
            rid += 1
            ts = datetime(day.year, day.month, day.day, hour, minute).isoformat()
            cur = conn.execute("INSERT INTO receipts(ts,payment,ext_id) VALUES(?,?,?)",
                               (ts, "cash" if is_cash else "card", f"p{rid}"))
            rows = [(drink[0], drink[1])] + ([(food[0], food[1])] if food else [])
            for n, price in rows:
                p = catalog.resolve(conn, n)
                catalog.register(conn, p)
                conn.execute(
                    "INSERT INTO receipt_items"
                    "(receipt_id,name,base,category,kind,size,volume_ml,milk,mods,iced,qty,price)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (cur.lastrowid, n, p["base"], p["category"], p["kind"], p["size"],
                     p["volume_ml"], p["milk"], p["mods"], p["iced"], 1, price))
                for ingredient, amount in costing.portion_ingredients(
                        recipes, p["base"], p["size"], p["milk"], p["mods"], p["iced"]).items():
                    if ingredient in stock:
                        stock[ingredient] -= amount
        conn.commit()

        for n in watched:
            if stock[n] < 0:
                if idx >= WARMUP:
                    stockouts[n] += 1
                stock[n] = 0.0              # встали: дальше продаём «в долг»

        # владелец каждый день смотрит заявку и берёт то, что она пометила срочным
        if idx >= 13:
            conn.execute("DELETE FROM stock_counts")
            for n, q in stock.items():
                conn.execute("INSERT INTO stock_counts(ts,ingredient,qty) VALUES(?,?,?)",
                             (datetime(day.year, day.month, day.day, 23, 0).isoformat(), n, q))
            conn.commit()
            rep = supply.reorder(conn, horizon_days=horizon, upto=day)
            for item in rep["items"]:
                if item["name"] not in stock or item["packs"] <= 0:
                    continue
                if item["urgency"] not in ("critical", "soon"):
                    continue
                row = ing[item["name"]]
                arrive = day + timedelta(days=int(math.ceil(row["lead_days"] or 0)))
                got = item["packs"] * row["pack_qty"]
                incoming.setdefault(arrive, {})[item["name"]] = \
                    incoming.setdefault(arrive, {}).get(item["name"], 0) + got
                if idx >= WARMUP:
                    ordered_packs[item["name"]] += item["packs"]

    conn.close()
    days_after_warmup = max(1, len(stream) - WARMUP)
    bad = {n: d for n, d in stockouts.items() if d}
    return {"days": days_after_warmup, "stockouts": bad, "watched": len(watched),
            "ordered": {n: p for n, p in ordered_packs.items() if p}}


def menu_parse(name):
    from coffeeos import menu as _menu
    return _menu.parse(name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--checks", type=int, default=180)
    ap.add_argument("--cost", type=float, default=0.42,
                    help="доля закупки в цене витрины — ВНЕШНЕЕ допущение только "
                         "для оценки прибыли в этом бенчмарке")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--seeds", type=int, default=1,
                    help="сколько разных потоков гостей усреднить "
                         "(на одном сиде разница может оказаться шумом)")
    ap.add_argument("--supply", action="store_true",
                    help="ещё и проверить, что заявка не даёт встать без сырья")
    a = ap.parse_args()

    months = (a.days - WARMUP) / 30.0
    names = ["чутьё владельца (как сейчас)",
             "среднее за 2 недели (табличка)",
             "КофейняОС (спрос + z·сигма)",
             "КофейняОС без ввода списаний"]
    fns = [(policy_gut, True), (policy_average, True),
           (policy_coffeeos, True), (policy_coffeeos, False)]
    acc = {n: {"gross": [], "waste": [], "lost": [], "service": []} for n in names}

    for k in range(a.seeds):
        stream = demand_stream(a.days, a.checks, a.seed + k)
        for nm, (fn, rec) in zip(names, fns):
            r = run_policy(nm, fn, stream, a.cost, record_waste=rec)
            acc[nm]["gross"].append(r["gross"] / months)
            acc[nm]["waste"].append(r["waste_units"] / months)
            acc[nm]["lost"].append(r["lost_units"] / months)
            acc[nm]["service"].append(r["service"])

    def avg(v):
        return sum(v) / len(v)

    print(f"\nПоток гостей один и тот же для всех стратегий. {a.days} дней "
          f"(первые {WARMUP} — разогрев); прибыль оценена при допущении «закупка "
          f"витрины {a.cost:.0%} цены» (только для сравнения)"
          + (f", усреднение по {a.seeds} потокам." if a.seeds > 1 else "."))
    print(f"Целевой уровень сервиса витрины: закрыть {economics.service_level():.0%} спроса.\n")
    base = avg(acc[names[0]]["gross"])
    print(f"{'стратегия':34s} {'валовая приб.':>15} {'к «чутью»':>12} "
          f"{'списано':>9} {'недопрод.':>10} {'сервис':>8}")
    for nm in names:
        g, d = avg(acc[nm]["gross"]), avg(acc[nm]["gross"]) - base
        spread = ""
        if a.seeds > 1 and nm != names[0]:
            deltas = [x - y for x, y in zip(acc[nm]["gross"], acc[names[0]]["gross"])]
            spread = f"  (от {min(deltas):+,.0f} до {max(deltas):+,.0f})".replace(",", " ")
        print(f"{nm:34s} {g:14,.0f} ₽ {d:+11,.0f} ₽ {avg(acc[nm]['waste']):8,.0f} "
              f"{avg(acc[nm]['lost']):9,.0f} {avg(acc[nm]['service'])*100:7.0f}%{spread}"
              .replace(",", " "))
    print("\nШтуки — в месяц. Списано = поставили и не продали. "
          "Недопродано = гость хотел, а не было.")
    print("Сервис = в какой доле «позиция×день» хватило на всех. Это фактический "
          "результат, а не цель: оценки спроса строятся по урезанным продажам, "
          "поэтому фактический сервис ниже целевого.")

    if a.supply:
        stream = demand_stream(a.days, a.checks, a.seed)
        res = run_supply_check(stream)
        print(f"\nЗаявка поставщику: заказ раз в 7 дней, {res['days']} дней работы, "
              f"{res['watched']} позиций сырья под наблюдением.")
        print("  Владелец каждый день смотрит заявку и берёт то, что помечено "
              "«горит» или «скоро».")
        if res["stockouts"]:
            print("  🔴 Встали без сырья:")
            for n, d in sorted(res["stockouts"].items(), key=lambda x: -x[1]):
                print(f"     {n}: {d} дн. из {res['days']}")
        else:
            print("  🟢 Ни одного дня без зерна, молока и стаканов.")


if __name__ == "__main__":
    main()
