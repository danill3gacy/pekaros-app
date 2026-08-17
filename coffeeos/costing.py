"""Себестоимость порции, маржа и разбор меню по деньгам.

Кассе видна выручка. Владельцу нужна маржа — и именно в кофейне между ними
пропасть, о которой он обычно не знает:

  • Латте на овсяном молоке стоит ему почти вдвое дороже обычного, а наценка
    за альтернативу поставлена один раз и с тех пор не пересматривалась.
  • Раф выглядит дорогой позицией, но сливки и сироп съедают маржу так, что
    он приносит меньше американо.
  • Позиция может быть хитом продаж и при этом работать в минус.

Всё это считается точно, потому что рецептура напитка стабильна.

Главный принцип модуля — не соврать. Если цена ингредиента не подтверждена
владельцем, себестоимость помечается как оценочная. Если цена неизвестна
вовсе (закупочная цена витрины), себестоимость не показывается совсем:
`None`, а не ноль. Ноль здесь означал бы «маржа 100%», и владелец принял бы
это за правду.
"""
from collections import defaultdict

from . import config, menu, reference

PRICE_UNKNOWN = "unknown"
PRICE_DEFAULT = "default"
PRICE_OWNER = "owner"


# ---------- ингредиенты ----------
def ingredients(conn):
    """Справочник ингредиентов: {имя: строка}."""
    return {r["name"]: dict(r) for r in conn.execute("SELECT * FROM ingredients")}


def unit_price(row):
    """Цена одной единицы (грамма, миллилитра, штуки)."""
    pack = row.get("pack_qty") or 0
    return (row.get("pack_price") or 0) / pack if pack else 0.0


def set_price(conn, name, pack_price, pack_qty=None):
    """Владелец назвал свою цену — она главнее типовой и больше не помечается
    как оценочная."""
    row = conn.execute("SELECT * FROM ingredients WHERE name=?", (name,)).fetchone()
    if row is None:
        return None
    qty = pack_qty if pack_qty else row["pack_qty"]
    conn.execute("UPDATE ingredients SET pack_price=?, pack_qty=?, price_src=? WHERE name=?",
                 (float(pack_price), float(qty), PRICE_OWNER, name))
    conn.commit()
    return {"name": name, "pack_price": float(pack_price), "pack_qty": float(qty)}


def ensure_purchased_item(conn, name, kind, category=None):
    """Витрина и упакованный товар закупаются готовыми, значит это ингредиент
    с ценой закупки и рецептом «одна штука = одна штука».

    Цена ставится НЕИЗВЕСТНОЙ, а не нулевой. Пока владелец её не назвал,
    продукт честно пишет «маржа не посчитана» вместо «маржа 100%».
    """
    conn.execute(
        "INSERT OR IGNORE INTO ingredients"
        "(name,unit,pack_qty,pack_price,pack_name,category,shelf_days,lead_days,min_packs,price_src)"
        " VALUES(?,'pcs',1,0,'шт',?,?,?,0,?)",
        (name, category or ("case" if kind == menu.KIND_FOOD else "goods"),
         1 if kind == menu.KIND_FOOD else None,
         1 if kind == menu.KIND_FOOD else 5, PRICE_UNKNOWN))
    conn.execute(
        "INSERT OR IGNORE INTO recipes(item,size,ingredient,qty,src) VALUES(?,'*',?,1,'auto')",
        (name, name))


# ---------- рецепт порции ----------
def recipe_rows(conn):
    """Вся рецептура одним запросом: {(позиция, размер): {ингредиент: кол-во}}."""
    out = defaultdict(dict)
    for r in conn.execute("SELECT item,size,ingredient,qty FROM recipes"):
        out[(r["item"], r["size"])][r["ingredient"]] = r["qty"]
    return dict(out)


def portion_ingredients(recipes, base, size=None, milk=None, mods="", iced=0, channel=None):
    """Что реально уходит на одну порцию с учётом молока, модификаторов и канала.

    Возвращает {ингредиент: количество}. Пустой словарь — рецепта нет.
    """
    size_key, _known = menu.size_or_default(size)
    parts = dict(recipes.get((base, size_key))
                 or recipes.get((base, "*"))
                 or recipes.get((base, "M"))
                 or {})
    if not parts:
        return {}

    # молоко: рецепт написан на обычном, чек говорит, какое налили на самом деле
    if milk and milk != menu.MILK_REGULAR and "Молоко обычное" in parts:
        qty = parts.pop("Молоко обычное")
        parts[f"Молоко {milk}"] = qty

    for tag in (mods or "").split(";"):
        for ing, qty in reference.MOD_INGREDIENTS.get(tag.strip(), {}).items():
            parts[ing] = parts.get(ing, 0) + qty

    if iced:
        parts["Трубочка"] = parts.get("Трубочка", 0) + 1

    # в зале подают в керамике — одноразовая посуда не расходуется
    if channel == "here":
        for ing in ("Стакан S", "Стакан M", "Стакан L", "Крышка", "Трубочка"):
            parts.pop(ing, None)
    return parts


# ---------- себестоимость ----------
def portion_cost(parts, ing_map):
    """Себестоимость порции по разложенному рецепту.

    Возвращает {cost, known, estimated, missing}:
      known=False     — часть цен неизвестна, цифру показывать нельзя;
      estimated=True  — всё известно, но какие-то цены типовые, не свои.
    """
    total = 0.0
    missing, estimated = [], False
    for name, qty in parts.items():
        row = ing_map.get(name)
        if row is None or row.get("price_src") == PRICE_UNKNOWN:
            missing.append(name)
            continue
        if row.get("price_src") != PRICE_OWNER:
            estimated = True
        total += unit_price(row) * qty
    if missing:
        return {"cost": None, "known": False, "estimated": estimated, "missing": missing}
    return {"cost": round(total, 2), "known": True, "estimated": estimated, "missing": []}


def cost_of(conn, base, size=None, milk=None, mods="", iced=0, channel=None,
            recipes=None, ing_map=None):
    """Себестоимость одной порции конкретной позиции — удобная обёртка."""
    recipes = recipes if recipes is not None else recipe_rows(conn)
    ing_map = ing_map if ing_map is not None else ingredients(conn)
    parts = portion_ingredients(recipes, base, size, milk, mods, iced, channel)
    if not parts:
        return {"cost": None, "known": False, "estimated": False, "missing": [], "no_recipe": True}
    res = portion_cost(parts, ing_map)
    res["no_recipe"] = False
    res["parts"] = parts
    return res


# ---------- экономика позиций меню ----------
def item_economics(conn, days=56, upto=None, kinds=(menu.KIND_DRINK, menu.KIND_FOOD)):
    """Сколько каждая позиция меню приносит и сколько стоит.

    Считается по фактическим строкам чеков: у одной и той же позиции в разные
    дни разный размер, молоко и модификаторы — и себестоимость у них разная.
    Усреднять «латте вообще» нельзя, иначе продажа на овсяном молоке спрячется
    в среднем и маржа окажется завышенной.
    """
    from .analytics import last_day_with_data, day_str
    from datetime import timedelta
    upto = upto or last_day_with_data(conn)
    start = upto - timedelta(days=days - 1)
    recipes = recipe_rows(conn)
    ing_map = ingredients(conn)

    rows = conn.execute(
        """SELECT i.base, i.kind, i.size, i.milk, i.mods, i.iced, r.channel,
                  SUM(i.qty) q, SUM(i.qty*i.price) rev
           FROM receipts r JOIN receipt_items i ON i.receipt_id=r.id
           WHERE substr(r.ts,1,10) BETWEEN ? AND ? AND i.kind IN (%s) AND i.qty > 0
           GROUP BY i.base, i.kind, i.size, i.milk, i.mods, i.iced, r.channel"""
        % ",".join("?" * len(kinds)),
        (day_str(start), day_str(upto), *kinds)).fetchall()

    agg = {}
    for r in rows:
        base = r["base"]
        if not base:
            continue
        cur = agg.setdefault(base, {
            "name": base, "kind": r["kind"], "qty": 0.0, "revenue": 0.0,
            "cost_total": 0.0, "qty_costed": 0.0, "estimated": False,
            "missing": set(), "variants": 0})
        cur["qty"] += r["q"] or 0
        cur["revenue"] += r["rev"] or 0
        cur["variants"] += 1
        c = cost_of(conn, base, r["size"], r["milk"], r["mods"], r["iced"], r["channel"],
                    recipes=recipes, ing_map=ing_map)
        if c["known"]:
            cur["cost_total"] += c["cost"] * (r["q"] or 0)
            cur["qty_costed"] += r["q"] or 0
            cur["estimated"] = cur["estimated"] or c["estimated"]
        else:
            cur["missing"].update(c["missing"] or ([base] if c.get("no_recipe") else []))

    out = []
    for base, a in agg.items():
        qty = a["qty"]
        if qty <= 0:
            continue
        price = a["revenue"] / qty
        # Себестоимость показываем, только если посчитана хотя бы для 80%
        # проданных порций. Иначе средняя маржа собрана из половины данных и
        # выглядит достовернее, чем есть.
        covered = a["qty_costed"] / qty if qty else 0
        if covered >= 0.8 and a["qty_costed"] > 0:
            cost = a["cost_total"] / a["qty_costed"]
            margin = price - cost
            item = {"cost": round(cost, 2), "margin": round(margin, 2),
                    "margin_total": round(margin * qty),
                    "foodcost": round(cost / price, 4) if price else None,
                    "cost_known": True, "estimated": a["estimated"],
                    "coverage": round(covered, 2)}
        else:
            item = {"cost": None, "margin": None, "margin_total": None,
                    "foodcost": None, "cost_known": False, "estimated": a["estimated"],
                    "coverage": round(covered, 2),
                    "missing": sorted(a["missing"])[:4]}
        out.append({"name": base, "kind": a["kind"], "qty": round(qty, 1),
                    "revenue": round(a["revenue"]), "price": round(price, 1), **item})
    out.sort(key=lambda x: x["revenue"], reverse=True)
    return out


def totals(conn, days=56, upto=None):
    """Итог по деньгам: выручка заведения, себестоимость проданного, маржа, фудкост.

    Здесь ТРИ разные выручки, и путать их нельзя — владелец сверяет цифру с
    кассой, и расхождение он воспримет как ошибку системы:

      • `revenue` — выручка заведения целиком, ровно как в кассе: вместе с
        товарами на полке, добавками отдельной строкой и за вычетом возвратов;
      • `revenue_menu` — только позиции меню (напитки и витрина), по которым
        вообще имеет смысл считать рецептурную себестоимость;
      • `revenue_costed` — та их часть, где закупочные цены известны.

    Раньше `revenue` считалась по третьему смыслу, а подписывалась первым: в
    разделе «Маржа» стояла сумма на несколько процентов меньше кассовой.
    """
    from datetime import timedelta
    from .analytics import day_str, last_day_with_data
    upto = upto or last_day_with_data(conn)
    start = upto - timedelta(days=days - 1)
    row = conn.execute(
        """SELECT COALESCE(SUM(i.qty*i.price), 0) r
           FROM receipts r0 JOIN receipt_items i ON i.receipt_id=r0.id
           WHERE substr(r0.ts,1,10) BETWEEN ? AND ?""",
        (day_str(start), day_str(upto))).fetchone()
    revenue_total = row["r"] or 0

    items = item_economics(conn, days, upto)
    known = [i for i in items if i["cost_known"]]
    rev_menu = sum(i["revenue"] for i in items)
    rev_known = sum(i["revenue"] for i in known)
    margin = sum(i["margin_total"] for i in known)
    cost = rev_known - margin
    return {
        "revenue": round(revenue_total),
        "revenue_menu": round(rev_menu),
        "revenue_costed": round(rev_known),
        # доля ВСЕЙ выручки заведения, по которой маржа действительно посчитана
        "coverage": round(rev_known / revenue_total, 3) if revenue_total else 0,
        "cost": round(cost), "margin": round(margin),
        "foodcost": round(cost / rev_known, 4) if rev_known else None,
        "estimated": any(i["estimated"] for i in known),
        "unpriced": [i["name"] for i in items if not i["cost_known"]][:12],
    }


# ---------- разбор меню по деньгам ----------
STAR = "звезда"          # много продаётся и хорошо зарабатывает
WORKHORSE = "лошадка"    # много продаётся, зарабатывает мало
PUZZLE = "загадка"       # зарабатывает хорошо, продаётся мало
BALLAST = "балласт"      # и не продаётся, и не зарабатывает


def menu_matrix(conn, days=56, upto=None):
    """Разложить меню на четыре группы: что беречь, что чинить, что продвигать,
    что убрать.

    Сравнение идёт с МЕДИАНОЙ по своему же меню, а не с отраслевым нормативом:
    у кофейни у дороги и у кофейни в спальном районе разные и цены, и объёмы,
    и «правильная» маржа. Сравнивать позицию имеет смысл только с соседями по
    её собственной витрине.
    """
    items = [i for i in item_economics(conn, days, upto) if i["cost_known"] and i["qty"] > 0]
    if len(items) < 4:
        return {"items": [], "note": "мало позиций с известной себестоимостью", "median": None}
    med_margin = _median([i["margin"] for i in items])
    med_qty = _median([i["qty"] for i in items])
    for i in items:
        hi_m = i["margin"] >= med_margin
        hi_q = i["qty"] >= med_qty
        i["group"] = (STAR if (hi_m and hi_q) else
                      WORKHORSE if (not hi_m and hi_q) else
                      PUZZLE if hi_m else BALLAST)
        i["advice"] = _ADVICE[i["group"]]
    items.sort(key=lambda x: (x["margin_total"] or 0), reverse=True)
    return {"items": items, "median": {"margin": round(med_margin, 1), "qty": round(med_qty, 1)},
            "note": None}


_ADVICE = {
    STAR: "держать в наличии всегда — на этом стоит заведение",
    WORKHORSE: "берут охотно, а зарабатываете мало: поднять цену или пересчитать рецепт",
    PUZZLE: "зарабатывает хорошо, но её не замечают — вынести в меню и предлагать",
    BALLAST: "не продаётся и не зарабатывает — кандидат на вывод из меню",
}


def high_foodcost(conn, days=56, upto=None, limit=6):
    """Позиции, где сырьё съедает больше ориентира: где искать деньги первым делом."""
    items = [i for i in item_economics(conn, days, upto)
             if i["cost_known"] and i["foodcost"] is not None]
    bad = [i for i in items if i["foodcost"] > config.TARGET_FOODCOST]
    bad.sort(key=lambda x: (x["foodcost"] * x["revenue"]), reverse=True)
    return bad[:limit]


def milk_economics(conn, days=56, upto=None):
    """Сколько зарабатывает кофейня на альтернативном молоке.

    Обычная история: наценку за растительное молоко поставили один раз, а
    закупочная цена с тех пор выросла вдвое. Проверяется это ровно так:
    сравнить маржу одного и того же напитка на разном молоке.
    """
    from .analytics import last_day_with_data, day_str
    from datetime import timedelta
    upto = upto or last_day_with_data(conn)
    start = upto - timedelta(days=days - 1)
    recipes, ing_map = recipe_rows(conn), ingredients(conn)
    rows = conn.execute(
        """SELECT i.base, i.size, i.milk, i.mods, i.iced, r.channel,
                  SUM(i.qty) q, SUM(i.qty*i.price) rev
           FROM receipts r JOIN receipt_items i ON i.receipt_id=r.id
           WHERE substr(r.ts,1,10) BETWEEN ? AND ? AND i.kind='drink'
                 AND i.milk IS NOT NULL AND i.qty > 0
           GROUP BY i.base, i.size, i.milk, i.mods, i.iced, r.channel""",
        (day_str(start), day_str(upto))).fetchall()
    agg = defaultdict(lambda: {"qty": 0.0, "rev": 0.0, "cost": 0.0, "costed": 0.0})
    for r in rows:
        c = cost_of(conn, r["base"], r["size"], r["milk"], r["mods"], r["iced"], r["channel"],
                    recipes=recipes, ing_map=ing_map)
        a = agg[r["milk"]]
        a["qty"] += r["q"] or 0
        a["rev"] += r["rev"] or 0
        if c["known"]:
            a["cost"] += c["cost"] * (r["q"] or 0)
            a["costed"] += r["q"] or 0
    out = []
    for milk, a in agg.items():
        if a["qty"] <= 0 or a["costed"] <= 0:
            continue
        price = a["rev"] / a["qty"]
        cost = a["cost"] / a["costed"]
        out.append({"milk": milk, "qty": round(a["qty"]), "price": round(price),
                    "cost": round(cost, 1), "margin": round(price - cost, 1),
                    "foodcost": round(cost / price, 3) if price else None,
                    "share": 0.0})
    total = sum(x["qty"] for x in out) or 1
    for x in out:
        x["share"] = round(x["qty"] / total, 3)
    out.sort(key=lambda x: x["qty"], reverse=True)
    return out


def _median(vals):
    s = sorted(vals)
    if not s:
        return 0.0
    return s[len(s) // 2] if len(s) % 2 else (s[len(s) // 2 - 1] + s[len(s) // 2]) / 2
