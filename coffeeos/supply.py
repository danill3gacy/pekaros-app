"""Расход сырья и заказ поставщику.

Это второй большой блок, которого в пекарной версии не было и быть не могло.

В пекарне расход муки из чеков не восстановить: между мукой и багетом стоит
тесто, расстойка и рука пекаря. В кофейне между зерном и латте не стоит
ничего — рецепт постоянен. Значит, по проданным чашкам расход зерна, молока,
сиропов и стаканов считается ТОЧНО, без единой инвентаризации.

Отсюда главное: заявка поставщику перестаёт быть списком «на глаз». Система
знает, что за прошлую неделю ушло 12,4 кг зерна и 96 литров молока, знает
профиль дней недели и знает, что поставка идёт два дня. Значит, она может
сказать не «закажите зерно», а «закажите 14 кг зерна сегодня, иначе в четверг
останетесь без него».

Инвентаризация не обязательна. Без неё система считает расход и заказывает под
него; с ней — дополнительно знает, сколько осталось прямо сейчас, и говорит,
на сколько дней хватит. Требовать ежедневного пересчёта нельзя: продукт,
который держится на ручном вводе, умирает вместе с бариста, которого научили.
"""

import math
import re
from datetime import timedelta

from . import config, costing, menu
from .analytics import day_str, last_day_with_data, operating_days, weekday_profile

# Витрина заказывается отдельно и ежедневно — под завтрашний спрос
# (demand.case_order). Сюда она не попадает: у неё другой цикл и другая логика.
CASE_CATEGORY = "case"


def consumption(conn, days=28, upto=None):
    """Сколько сырья ушло за период — по чекам и рецептуре.

    Возвращает {ингредиент: {used, per_day, unit, ...}}. В расход включаются и
    списания сырья: вылитое молоко покупать всё равно приходится.
    """
    upto = upto or last_day_with_data(conn)
    start = upto - timedelta(days=days - 1)
    recipes = costing.recipe_rows(conn)
    ing_map = costing.ingredients(conn)
    nd = operating_days(conn, days, upto)

    rows = conn.execute(
        """SELECT i.base, i.size, i.milk, i.mods, i.iced, r.channel, SUM(i.qty) q
           FROM receipts r JOIN receipt_items i ON i.receipt_id=r.id
           WHERE substr(r.ts,1,10) BETWEEN ? AND ? AND i.qty > 0
                 AND i.base IS NOT NULL AND i.kind IN ('drink','food','goods','addon')
           GROUP BY i.base, i.size, i.milk, i.mods, i.iced, r.channel""",
        (day_str(start), day_str(upto)),
    ).fetchall()

    used = {}
    no_recipe = set()
    for r in rows:
        parts = costing.portion_ingredients(
            recipes, r["base"], r["size"], r["milk"], r["mods"], r["iced"], r["channel"]
        )
        if not parts:
            no_recipe.add(r["base"])
            continue
        for ing, qty in parts.items():
            used[ing] = used.get(ing, 0.0) + qty * (r["q"] or 0)

    # списанное сырьё тоже израсходовано
    for w in conn.execute(
        "SELECT name, unit, SUM(qty) q FROM waste WHERE date BETWEEN ? AND ? "
        "AND kind IN ('milk','ingredient') GROUP BY name, unit",
        (day_str(start), day_str(upto)),
    ):
        row = ing_map.get(w["name"])
        if not row:
            continue
        mult = 1000.0 if row["unit"] in ("ml", "g") else 1.0
        used[w["name"]] = used.get(w["name"], 0.0) + (w["q"] or 0) * mult

    out = {}
    for name, qty in used.items():
        row = ing_map.get(name)
        if row is None:
            continue
        out[name] = {
            "name": name,
            "unit": row["unit"],
            "used": round(qty, 2),
            "per_day": round(qty / nd, 3),
            "category": row["category"],
            "pack_qty": row["pack_qty"],
            "pack_name": row["pack_name"],
            "pack_price": row["pack_price"],
            "price_src": row["price_src"],
            "shelf_days": row["shelf_days"],
            "lead_days": row["lead_days"],
            "min_packs": row["min_packs"],
        }
    return {
        "days": nd,
        "from": day_str(start),
        "to": day_str(upto),
        "items": out,
        "no_recipe": sorted(no_recipe),
    }


def human_qty(qty, unit):
    """Перевести хранимые единицы в те, которыми говорят люди: мл -> л, г -> кг."""
    if unit == "ml":
        return round(qty / 1000, 2), "л"
    if unit == "g":
        return round(qty / 1000, 2), "кг"
    return round(qty), "шт"


# ---------- остатки ----------
# Больше этого одна точка под стойкой не держит. Без проверки опечатка в
# инвентаризации («остаток зерно 99999999») давала «хватит на 71 миллион дней»,
# и заявка переставала заказывать вообще что-либо.
MAX_STOCK = 10000


def record_stock(conn, ingredient, qty):
    """Владелец пересчитал остаток. Количество — в человеческих единицах (л, кг, шт).

    Возвращает {"ambiguous": [...]}, если название подходит нескольким
    позициям: угадывать за владельца нельзя, остаток не той позиции сделает
    заявку неверной сразу по двум строкам.
    """
    from .catalog import match_ingredient

    m = match_ingredient(conn, ingredient)
    if not m["name"]:
        return {"ambiguous": m["options"]} if len(m["options"]) > 1 else None
    try:
        qty = float(qty)
    except (TypeError, ValueError):
        return None
    if not (0 <= qty <= MAX_STOCK):
        return {"error": "implausible", "max": MAX_STOCK}
    name = m["name"]
    row = conn.execute("SELECT unit FROM ingredients WHERE name=?", (name,)).fetchone()
    mult = 1000.0 if row["unit"] in ("ml", "g") else 1.0
    conn.execute(
        "INSERT INTO stock_counts(ts,ingredient,qty) VALUES(?,?,?)",
        (config.now().isoformat(timespec="seconds"), name, qty * mult),
    )
    conn.commit()
    unit = {"ml": "л", "g": "кг"}.get(row["unit"], "шт")
    return {"name": name, "qty": qty, "unit": unit}


def _last_counts(conn):
    return {
        r["ingredient"]: (r["ts"], r["qty"])
        for r in conn.execute(
            "SELECT ingredient, ts, qty FROM stock_counts s WHERE ts = "
            "(SELECT MAX(ts) FROM stock_counts x WHERE x.ingredient = s.ingredient)"
        )
    }


def _used_since(conn, ts):
    """Сколько сырья израсходовано с момента инвентаризации (по чекам после неё)."""
    recipes = costing.recipe_rows(conn)
    rows = conn.execute(
        """SELECT i.base, i.size, i.milk, i.mods, i.iced, r.channel, SUM(i.qty) q
           FROM receipts r JOIN receipt_items i ON i.receipt_id=r.id
           WHERE r.ts > ? AND i.qty > 0 AND i.base IS NOT NULL
           GROUP BY i.base, i.size, i.milk, i.mods, i.iced, r.channel""",
        (ts,),
    ).fetchall()
    used = {}
    for r in rows:
        parts = costing.portion_ingredients(
            recipes, r["base"], r["size"], r["milk"], r["mods"], r["iced"], r["channel"]
        )
        for ing, qty in parts.items():
            used[ing] = used.get(ing, 0.0) + qty * (r["q"] or 0)
    return used


def stock_state(conn):
    """Остаток на сейчас: последняя инвентаризация минус расход по чекам после неё."""
    counts = _last_counts(conn)
    if not counts:
        return {}
    out = {}
    for name, (ts, qty) in counts.items():
        spent = _used_since(conn, ts).get(name, 0.0)
        out[name] = {
            "counted_at": ts,
            "counted": qty,
            "spent_since": round(spent, 2),
            "left": round(max(0.0, qty - spent), 2),
        }
    return out


# ---------- заказ ----------
def _horizon_factors(conn, upto, max_days=40):
    """Сколько «средних дней» приходится на ближайшие N дней, для каждого N.

    Пятничная заявка на выходные и вторничная — это разные объёмы. Профиль
    берётся из собственной истории кофейни; мало данных — коэффициент 1.

    Считается один раз на всю заявку: прежде профиль дней недели пересчитывался
    из базы на каждый ингредиент — два десятка одинаковых запросов подряд.
    """
    prof = weekday_profile(conn, 56, upto)
    avg = sum(prof) / 7 or 1.0
    out, total = [0.0], 0.0
    for i in range(1, max_days + 1):
        total += prof[(upto + timedelta(days=i)).weekday()] / avg
        out.append(total)
    return out


def reorder(conn, horizon_days=None, days=28, upto=None):
    """Что и сколько заказать, чтобы не встать.

    Считает по каждому ингредиенту: сколько уходит в день, на сколько хватит
    остатка (если его считали), и сколько упаковок взять.

    Срок поставки влияет на то, КОГДА заказать, а не на то, СКОЛЬКО. Это не
    придирка: если прибавлять срок поставки к объёму заявки каждый раз, запас
    растёт с каждым циклом, и через месяц под стойкой стоит месячный запас
    зерна на деньги, которые нужны в обороте.

      • остаток посчитан → классическая схема «дозаказ до уровня»: покрываем
        период до следующей заявки плюс срок поставки, минус то, что есть;
      • остаток не считали → берём ровно расход за период до следующей заявки,
        а срок поставки закрывает страховой запас.

    Скоропорт отдельно ограничен разумным запасом: заказать двухнедельный запас
    молока — это не запас, это списание.
    """
    upto = upto or last_day_with_data(conn)
    horizon = horizon_days or config.ORDER_HORIZON_DAYS
    cons = consumption(conn, days, upto)
    stock = stock_state(conn)
    factors = _horizon_factors(conn, upto)

    def cover(n):
        return factors[max(0, min(len(factors) - 1, int(round(n))))]

    out = []
    for name, c in cons["items"].items():
        if c["category"] == CASE_CATEGORY:
            continue  # витрина заказывается отдельно, каждый день
        per_day = c["per_day"]
        if per_day <= 0:
            continue
        lead = c["lead_days"] or 0
        safety = (c["min_packs"] or 0) * (c["pack_qty"] or 1)
        st = stock.get(name)
        left = st["left"] if st else None

        # Скоропорт нельзя заказывать на неделю вперёд — но нельзя и просто
        # урезать объём, оставив недельный цикл: тогда молока не хватит с
        # четверга. У такой позиции СВОЙ цикл заказа, и продукт обязан это
        # сказать вслух: «молоко — каждые 3 дня, а не раз в неделю».
        review = min(horizon, c["shelf_days"]) if c["shelf_days"] else horizon
        cover_days = review + lead if left is not None else review
        need_raw = per_day * cover(cover_days)
        need = need_raw + safety - (left or 0)
        packs = max(0, math.ceil(need / (c["pack_qty"] or 1) - 1e-9))
        days_left = round(left / per_day, 1) if (left is not None and per_day) else None

        if days_left is not None and days_left <= lead:
            urgency = "critical"  # уже не успеваем: закончится до поставки
        elif days_left is not None and days_left <= lead + 2:
            urgency = "soon"
        elif days_left is None and packs > 0:
            urgency = "plan"  # плановая заявка: остаток не считали
        else:
            urgency = "ok"

        qty_h, unit_h = human_qty(need_raw, c["unit"])
        left_h = human_qty(left, c["unit"])[0] if left is not None else None
        out.append(
            {
                "name": name,
                "category": c["category"],
                "per_day": human_qty(per_day, c["unit"])[0],
                "unit": unit_h,
                "need": qty_h,
                "packs": packs,
                "pack_name": c["pack_name"],
                "pack_qty": c["pack_qty"],
                "left": left_h,
                "days_left": days_left,
                "lead_days": lead,
                "review_days": review,
                "urgency": urgency,
                "price": round((c["pack_price"] or 0) * packs),
                "price_known": c["price_src"] != costing.PRICE_UNKNOWN,
                "shelf_days": c["shelf_days"],
            }
        )
    rank = {"critical": 0, "soon": 1, "plan": 2, "ok": 3}
    out.sort(key=lambda x: (rank[x["urgency"]], -(x["price"] or 0)))
    note = (
        None
        if stock
        else (
            "Остатки не пересчитывали, поэтому заявка построена по расходу за период, "
            "а не по тому, что стоит под стойкой. Пересчитайте пару позиций "
            "(«остаток зерно 4», «остаток молоко 12») — и система начнёт говорить, "
            "на сколько дней хватит и когда именно закончится."
        )
    )
    return {
        "horizon_days": horizon,
        "items": out,
        "counted": bool(stock),
        "based_on_days": cons["days"],
        "note": note,
        "total_price": sum(i["price"] for i in out if i["price_known"]),
        "no_recipe": cons["no_recipe"],
    }


def order_draft(conn, horizon_days=None):
    """Готовая заявка поставщику — текстом, который можно переслать как есть."""
    rep = reorder(conn, horizon_days)
    lines = [i for i in rep["items"] if i["packs"] > 0]
    if not lines:
        return {"text": "", "items": [], "report": rep}
    by_cat = {}
    for i in lines:
        by_cat.setdefault(i["category"], []).append(i)
    title = {
        "coffee": "Кофе",
        "dairy": "Молоко",
        "syrup": "Сиропы",
        "packaging": "Расходники",
        "goods": "Товары",
        "other": "Прочее",
    }
    body = []
    for cat in ("coffee", "dairy", "syrup", "packaging", "goods", "other"):
        group = by_cat.get(cat)
        if not group:
            continue
        body.append(f"{title.get(cat, cat)}:")
        for i in group:
            body.append(f"  • {i['name']} — {i['packs']} {i['pack_name']}")
    return {"text": "\n".join(body), "items": lines, "report": rep}


# ---------- регламент обслуживания ----------
def maintenance_due(conn, today=None):
    """Что по обслуживанию просрочено или пора делать.

    Не «ещё один чеклист»: сбитая помолка уводит дозу на 2–3 грамма с чашки —
    это десятая часть расхода зерна и заметное искажение вкуса, а забитый
    фильтр воды убивает бойлер целиком.
    """
    today = today or config.today()
    out = []
    for r in conn.execute("SELECT * FROM maintenance ORDER BY period_days"):
        last = r["last_done"]
        if last:
            try:
                y, m, d = map(int, last[:10].split("-"))
                from datetime import date as _date

                since = (today - _date(y, m, d)).days
            except ValueError:
                since = None
        else:
            since = None
        due = since is None or since >= r["period_days"]
        out.append(
            {
                "task": r["task"],
                "period_days": r["period_days"],
                "last_done": last,
                "days_since": since,
                "due": due,
                "overdue_days": (since - r["period_days"]) if (since is not None) else None,
                "note": r["note"],
            }
        )
    return [x for x in out if x["due"]]


def mark_done(conn, task_fragment, today=None):
    """Отметить выполненную работу по регламенту.

    Сопоставляем по ОСНОВАМ слов, а не по подстроке: человек пишет «сделал
    калибровку», а в регламенте «Калибровка помола». Проверка на вхождение
    подстроки такое не находила, и раздел выглядел неработающим.
    """
    today = today or config.today()
    words = [
        menu.stem(w)
        for w in re.findall(r"[а-яёa-z]+", (task_fragment or "").lower())
        if len(w) >= 3
    ]
    if not words:
        return None
    row = None
    for r in conn.execute("SELECT task FROM maintenance"):
        task_words = {menu.stem(w) for w in re.findall(r"[а-яёa-z]+", r["task"].lower())}
        if all(any(tw.startswith(w) or w.startswith(tw) for tw in task_words) for w in words):
            row = r
            break
    if row is None:
        return None
    conn.execute("UPDATE maintenance SET last_done=? WHERE task=?", (day_str(today), row["task"]))
    conn.commit()
    return row["task"]
