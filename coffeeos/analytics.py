"""Аналитика по чекам кофейни.

Часть считалок проверена на реальных выгрузках ещё в пекарной версии продукта:
выручка, чеки, часы, дни недели, качество данных. Часть специфична для кофейни
и в пекарне смысла не имела:

  • **attach-rate** — доля кофейных чеков, в которых есть ещё и еда. Это
    главный рычаг среднего чека в кофейне: напиток покупают и так, а еда
    добавляется только если её предложили. Разница между лучшим часом и
    средним по дню — это и есть размер упущенного.
  • **разрез по размеру, молоку, «горячий/холодный», «с собой / в зале»** —
    без него нельзя ни посчитать расход, ни увидеть, где утекает маржа.
  • **корзина** — что реально берут вместе с латте, а не что кажется владельцу.

Всё, чего в данных нет, помечается как «не считается на этих данных», а не
заполняется нулями.
"""

from collections import defaultdict
from datetime import date, timedelta

from . import config

ITEM_KEY = "COALESCE(i.base, i.name)"  # позиция меню: разобранное имя, иначе как в чеке


def day_str(d):
    return d.isoformat()


def fmt(n):
    return f"{int(round(n)):,}".replace(",", " ")


# ---------- продажи ----------
def sales_summary(conn, day):
    ds = day_str(day)
    rows = conn.execute(
        """SELECT r.id rid, r.payment, SUM(i.qty*i.price) total
           FROM receipts r JOIN receipt_items i ON i.receipt_id=r.id
           WHERE substr(r.ts,1,10)=? GROUP BY r.id""",
        (ds,),
    ).fetchall()
    revenue = sum(x["total"] for x in rows)
    checks = len(rows)
    cash = sum(x["total"] for x in rows if x["payment"] == "cash")
    card = revenue - cash
    avg = revenue / checks if checks else 0
    items = (
        conn.execute(
            "SELECT SUM(i.qty) q FROM receipts r JOIN receipt_items i ON i.receipt_id=r.id "
            "WHERE substr(r.ts,1,10)=?",
            (ds,),
        ).fetchone()["q"]
        or 0
    )
    cups = (
        conn.execute(
            "SELECT SUM(i.qty) q FROM receipts r JOIN receipt_items i ON i.receipt_id=r.id "
            "WHERE substr(r.ts,1,10)=? AND i.kind='drink' AND i.qty>0",
            (ds,),
        ).fetchone()["q"]
        or 0
    )
    return {
        "date": ds,
        "revenue": revenue,
        "checks": checks,
        "avg": avg,
        "cash": cash,
        "card": card,
        "items": items,
        "cups": cups,
    }


def top_positions(conn, day, n=6):
    ds = day_str(day)
    rows = conn.execute(
        f"""SELECT {ITEM_KEY} name, SUM(i.qty) q, SUM(i.qty*i.price) rev
            FROM receipts r JOIN receipt_items i ON i.receipt_id=r.id
            WHERE substr(r.ts,1,10)=? AND i.kind<>'service'
            GROUP BY name ORDER BY rev DESC LIMIT ?""",
        (ds, n),
    ).fetchall()
    return [{"name": x["name"], "qty": x["q"], "rev": x["rev"]} for x in rows]


def revenue_by_category(conn, day):
    ds = day_str(day)
    rows = conn.execute(
        """SELECT COALESCE(i.category,'Прочее') cat, SUM(i.qty*i.price) rev
           FROM receipts r JOIN receipt_items i ON i.receipt_id=r.id
           WHERE substr(r.ts,1,10)=? GROUP BY cat ORDER BY rev DESC""",
        (ds,),
    ).fetchall()
    return _with_shares(rows)


def _with_shares(rows):
    """Доли категорий в выручке.

    База — только положительная выручка: в день с крупным возвратом итог по
    категории уходил в минус, и доля получалась отрицательной («Кофе 120%»).
    """
    total = sum(max(0.0, x["rev"] or 0) for x in rows) or 1
    return [
        {"cat": x["cat"], "rev": x["rev"], "share": max(0.0, x["rev"] or 0) / total} for x in rows
    ]


def hourly(conn, day):
    ds = day_str(day)
    rows = conn.execute(
        """SELECT CAST(substr(r.ts,12,2) AS INT) h, SUM(i.qty*i.price) rev,
                  COUNT(DISTINCT r.id) checks
           FROM receipts r JOIN receipt_items i ON i.receipt_id=r.id
           WHERE substr(r.ts,1,10)=? GROUP BY h ORDER BY h""",
        (ds,),
    ).fetchall()
    return [{"hour": x["h"], "rev": x["rev"], "checks": x["checks"]} for x in rows]


def week_trend(conn, end_day):
    out = []
    for i in range(6, -1, -1):
        d = end_day - timedelta(days=i)
        s = sales_summary(conn, d)
        out.append({"date": day_str(d), "dow": d.weekday(), "revenue": s["revenue"]})
    return out


def last_day_with_data(conn):
    """Последний день с продажами. Будущие даты игнорируем: один чек со сбитыми
    часами кассы (напр. 2027 год) иначе обнулил бы все сводки."""
    horizon = (config.today() + timedelta(days=1)).isoformat()
    row = conn.execute(
        "SELECT MAX(substr(ts,1,10)) d FROM receipts WHERE substr(ts,1,10) <= ?", (horizon,)
    ).fetchone()
    if row and row["d"]:
        y, m, dd = map(int, row["d"].split("-"))
        return date(y, m, dd)
    row = conn.execute("SELECT MAX(substr(ts,1,10)) d FROM receipts").fetchone()
    if row and row["d"]:  # данные есть, но все «из будущего»
        y, m, dd = map(int, row["d"].split("-"))
        return date(y, m, dd)
    return config.today() - timedelta(days=1)


def has_sales(conn):
    return conn.execute("SELECT COUNT(*) c FROM receipts").fetchone()["c"] > 0


# ---------- что на этих данных посчитать НЕЛЬЗЯ ----------
def data_quality(conn):
    """Что система МОЖЕТ и чего НЕ МОЖЕТ посчитать на этих данных.

    Выгрузки бывают бедные. По «продажам за день» без времени и номера чека
    нельзя восстановить ни утренний пик, ни attach-rate, ни распроданность
    витрины — и молчать об этом нельзя: владелец видел «пик продаж 0:00» и
    считал это правдой.
    """
    row = conn.execute(
        "SELECT COUNT(*) n, "
        "SUM(CASE WHEN substr(ts,12) <> '00:00:00' THEN 1 ELSE 0 END) timed "
        "FROM receipts"
    ).fetchone()
    total = row["n"] or 0
    timed = row["timed"] or 0
    warnings = []
    if not total:
        return {
            "ok": True,
            "warnings": [],
            "has_time": True,
            "has_receipts": True,
            "has_sizes": True,
            "has_milk": True,
            "has_barista": False,
            "has_guests": False,
            "has_channel": False,
            "receipts": 0,
        }
    has_time = timed > total * 0.05
    has_receipts = has_time or _looks_like_receipts(conn)
    if not has_time:
        warnings.append(
            "В выгрузке нет времени продажи — только дата. Поэтому не считаются "
            "утренний пик, загрузка бариста, attach-rate по часам и распроданность "
            "витрины. Выгрузите чеки с колонкой времени — эти разделы заработают."
        )
    if not has_receipts:
        warnings.append(
            "В выгрузке нет номера чека — восстановить состав чеков невозможно. "
            "Средний чек и attach-rate «кофе + еда» показывать нельзя, они будут неверны."
        )

    drinks = conn.execute(
        "SELECT COUNT(*) n, SUM(CASE WHEN size IS NOT NULL THEN 1 ELSE 0 END) sized, "
        "SUM(CASE WHEN milk IS NOT NULL THEN 1 ELSE 0 END) milked "
        "FROM receipt_items WHERE kind='drink'"
    ).fetchone()
    n_drinks = drinks["n"] or 0
    has_sizes = bool(n_drinks) and (drinks["sized"] or 0) > n_drinks * 0.3
    has_milk = bool(n_drinks) and (drinks["milked"] or 0) > n_drinks * 0.3
    if n_drinks and not has_sizes:
        warnings.append(
            "В названиях напитков нет размера. Себестоимость и расход зерна "
            "считаются по среднему размеру — цифры ориентировочные."
        )
    extra = conn.execute(
        "SELECT SUM(CASE WHEN barista IS NOT NULL AND barista<>'' THEN 1 ELSE 0 END) b, "
        "SUM(CASE WHEN guest IS NOT NULL AND guest<>'' THEN 1 ELSE 0 END) g, "
        "SUM(CASE WHEN channel IS NOT NULL AND channel<>'' THEN 1 ELSE 0 END) c "
        "FROM receipts"
    ).fetchone()
    return {
        "ok": not warnings,
        "warnings": warnings,
        "has_time": has_time,
        "has_receipts": has_receipts,
        "has_sizes": has_sizes,
        "has_milk": has_milk,
        "has_barista": (extra["b"] or 0) > total * 0.5,
        "has_guests": (extra["g"] or 0) > total * 0.1,
        "has_channel": (extra["c"] or 0) > total * 0.5,
        "receipts": total,
    }


def _looks_like_receipts(conn):
    """Похоже ли, что чеки настоящие, а не «одна строка выгрузки = один чек»."""
    row = conn.execute(
        "SELECT COUNT(*) n FROM (SELECT receipt_id FROM receipt_items "
        "GROUP BY receipt_id HAVING COUNT(*) > 1 LIMIT 1)"
    ).fetchone()
    return bool(row["n"])


def future_dated_count(conn):
    """Сколько чеков с датой из будущего — признак сбитых часов кассы."""
    horizon = (config.today() + timedelta(days=1)).isoformat()
    return conn.execute(
        "SELECT COUNT(*) c FROM receipts WHERE substr(ts,1,10) > ?", (horizon,)
    ).fetchone()["c"]


# ---------- разрезы кофейни ----------
def drink_mix(conn, days=56, upto=None, n=12):
    """Что и сколько пьют: напитки по чашкам и выручке."""
    upto = upto or last_day_with_data(conn)
    start = upto - timedelta(days=days - 1)
    rows = conn.execute(
        """SELECT i.base name, SUM(i.qty) q, SUM(i.qty*i.price) rev
           FROM receipts r JOIN receipt_items i ON i.receipt_id=r.id
           WHERE substr(r.ts,1,10) BETWEEN ? AND ? AND i.kind='drink' AND i.qty>0
           GROUP BY i.base ORDER BY q DESC LIMIT ?""",
        (day_str(start), day_str(upto), n),
    ).fetchall()
    total = (
        conn.execute(
            """SELECT SUM(i.qty) q FROM receipts r JOIN receipt_items i ON i.receipt_id=r.id
           WHERE substr(r.ts,1,10) BETWEEN ? AND ? AND i.kind='drink' AND i.qty>0""",
            (day_str(start), day_str(upto)),
        ).fetchone()["q"]
        or 1
    )
    return [
        {
            "name": r["name"],
            "qty": round(r["q"]),
            "rev": round(r["rev"]),
            "share": round((r["q"] or 0) / total, 3),
        }
        for r in rows
    ]


def _mix_by(conn, column, days, upto, where="i.kind='drink' AND i.qty>0"):
    upto = upto or last_day_with_data(conn)
    start = upto - timedelta(days=days - 1)
    rows = conn.execute(
        f"""SELECT {column} k, SUM(i.qty) q, SUM(i.qty*i.price) rev
            FROM receipts r JOIN receipt_items i ON i.receipt_id=r.id
            WHERE substr(r.ts,1,10) BETWEEN ? AND ? AND {where} AND {column} IS NOT NULL
            GROUP BY k ORDER BY q DESC""",
        (day_str(start), day_str(upto)),
    ).fetchall()
    total = sum(r["q"] or 0 for r in rows) or 1
    return [
        {
            "key": r["k"],
            "qty": round(r["q"]),
            "rev": round(r["rev"]),
            "share": round((r["q"] or 0) / total, 3),
        }
        for r in rows
    ]


def size_mix(conn, days=56, upto=None):
    return _mix_by(conn, "i.size", days, upto)


def milk_mix(conn, days=56, upto=None):
    return _mix_by(conn, "i.milk", days, upto)


def channel_mix(conn, days=56, upto=None):
    """С собой или в зале. Влияет и на расход стаканов, и на скорость смены."""
    return _mix_by(conn, "r.channel", days, upto, where="i.qty>0")


def iced_share(conn, days=56, upto=None):
    """Доля холодных напитков и её ход по месяцам.

    Кофейня, которая не готовит лёд и сиропы к маю, теряет весь летний прирост.
    Прогноза погоды у системы нет и не будет — но собственная сезонность видна
    по её же истории.
    """
    upto = upto or last_day_with_data(conn)
    start = upto - timedelta(days=days - 1)
    rows = conn.execute(
        """SELECT substr(r.ts,1,7) ym, SUM(CASE WHEN i.iced=1 THEN i.qty ELSE 0 END) iced,
                  SUM(i.qty) total
           FROM receipts r JOIN receipt_items i ON i.receipt_id=r.id
           WHERE substr(r.ts,1,10) BETWEEN ? AND ? AND i.kind='drink' AND i.qty>0
           GROUP BY ym ORDER BY ym""",
        (day_str(start), day_str(upto)),
    ).fetchall()
    by_month = [
        {
            "month": r["ym"],
            "share": round((r["iced"] or 0) / (r["total"] or 1), 3),
            "iced": round(r["iced"] or 0),
            "total": round(r["total"] or 0),
        }
        for r in rows
    ]
    iced = sum(r["iced"] or 0 for r in rows)
    total = sum(r["total"] or 0 for r in rows) or 1
    return {
        "share": round(iced / total, 3),
        "iced": round(iced),
        "total": round(total),
        "by_month": by_month,
    }


# ---------- attach-rate: главный рычаг среднего чека ----------
def attach_rate(conn, days=56, upto=None):
    """Доля кофейных чеков, в которых есть ещё и еда.

    Почему это главное. Напиток покупают и без продавца — за ним пришли. Еда
    добавляется в чек, только если её предложили или она попалась на глаза.
    Поэтому attach-rate — это почти чистая мера работы витрины и бариста, и
    единственный рычаг среднего чека, который не требует поднимать цены.

    Оценка потенциала намеренно скромная. Соблазн — взять лучший час кофейни и
    распространить его на весь день: цифра выходит крупная и красивая. Но она
    неверна: в 8:20 гость опаздывает на работу, в 15:00 он сидит с ноутбуком, и
    это разные люди с разным поведением. Такую цифру нельзя опровергнуть, и
    поэтому она ничего не стоит.

    Поэтому потенциал считается как «подтянуть отстающие часы до собственной
    МЕДИАНЫ»: половина часов этой кофейни уже работает на этом уровне, значит,
    он достижим без фантазий. И даже это помечено как оценка.
    """
    upto = upto or last_day_with_data(conn)
    start = upto - timedelta(days=days - 1)
    rows = conn.execute(
        """SELECT r.id, CAST(substr(r.ts,12,2) AS INT) h, substr(r.ts,1,10) d,
                  MAX(CASE WHEN i.kind='drink' AND i.qty>0 THEN 1 ELSE 0 END) has_drink,
                  MAX(CASE WHEN i.kind='food'  AND i.qty>0 THEN 1 ELSE 0 END) has_food
           FROM receipts r JOIN receipt_items i ON i.receipt_id=r.id
           WHERE substr(r.ts,1,10) BETWEEN ? AND ? GROUP BY r.id""",
        (day_str(start), day_str(upto)),
    ).fetchall()
    drink_checks = [r for r in rows if r["has_drink"]]
    if not drink_checks:
        return {"rate": None, "note": "в данных нет напитков — attach-rate не считается"}
    with_food = sum(1 for r in drink_checks if r["has_food"])
    rate = with_food / len(drink_checks)

    by_hour = defaultdict(lambda: [0, 0])
    for r in drink_checks:
        by_hour[r["h"]][0] += 1
        by_hour[r["h"]][1] += r["has_food"]
    hours = [
        {"hour": h, "checks": n, "rate": round(f / n, 3)}
        for h, (n, f) in sorted(by_hour.items())
        if n >= 20
    ]
    best = max(hours, key=lambda x: x["rate"], default=None)
    worst = min(hours, key=lambda x: x["rate"], default=None)

    food_price = conn.execute(
        """SELECT SUM(i.qty*i.price) rev, SUM(i.qty) q
           FROM receipts r JOIN receipt_items i ON i.receipt_id=r.id
           WHERE substr(r.ts,1,10) BETWEEN ? AND ? AND i.kind='food' AND i.qty>0""",
        (day_str(start), day_str(upto)),
    ).fetchone()
    avg_food = (food_price["rev"] / food_price["q"]) if (food_price["q"] or 0) else 0

    ndays = (
        conn.execute(
            "SELECT COUNT(DISTINCT substr(ts,1,10)) d FROM receipts "
            "WHERE substr(ts,1,10) BETWEEN ? AND ?",
            (day_str(start), day_str(upto)),
        ).fetchone()["d"]
        or 1
    )

    scenario = None
    if hours and avg_food > 0:
        med = _median_of([h["rate"] for h in hours])
        laggards = [h for h in hours if h["rate"] < med]
        extra = sum((med - h["rate"]) * h["checks"] for h in laggards) / ndays
        if extra > 0:
            scenario = {
                "target_rate": round(med, 3),
                "hours": [h["hour"] for h in laggards],
                "extra_items_per_day": round(extra, 1),
                "money_per_month": round(extra * avg_food * 30),
                "assumption": (
                    "допущение: отстающие часы можно подтянуть до медианы "
                    "собственных часов кофейни. Это оценка, а не факт из кассы."
                ),
            }

    # Утро против остального дня — классический разрыв кофейни: витрина
    # выставлена к 8:00, к полудню опустела, и вечером предлагать нечего.
    morning = [h for h in hours if h["hour"] <= 11]
    day = [h for h in hours if h["hour"] > 11]
    split = None
    if morning and day:
        m_rate = sum(h["rate"] * h["checks"] for h in morning) / sum(h["checks"] for h in morning)
        d_rate = sum(h["rate"] * h["checks"] for h in day) / sum(h["checks"] for h in day)
        split = {
            "morning": round(m_rate, 3),
            "day": round(d_rate, 3),
            "gap": round(m_rate - d_rate, 3),
        }

    return {
        "rate": round(rate, 3),
        "drink_checks": len(drink_checks),
        "with_food": with_food,
        "by_hour": hours,
        "best_hour": best,
        "worst_hour": worst,
        "avg_food_price": round(avg_food),
        "morning_vs_day": split,
        "scenario": scenario,
        "note": None,
    }


def _median_of(vals):
    s = sorted(vals)
    if not s:
        return 0.0
    return s[len(s) // 2] if len(s) % 2 else (s[len(s) // 2 - 1] + s[len(s) // 2]) / 2


def basket(conn, days=56, upto=None, top=8):
    """Что берут вместе с напитками — по фактам, а не по ощущениям.

    Считается подъём (lift): во сколько раз чаще еда встречается в чеке с этим
    напитком, чем вообще. Подъём, а не голая частота: круассан лидирует в паре
    с любым напитком просто потому, что круассанов много.
    """
    upto = upto or last_day_with_data(conn)
    start = upto - timedelta(days=days - 1)
    rows = conn.execute(
        """SELECT r.id rid, i.kind, i.base b
           FROM receipts r JOIN receipt_items i ON i.receipt_id=r.id
           WHERE substr(r.ts,1,10) BETWEEN ? AND ? AND i.qty>0
                 AND i.kind IN ('drink','food') AND i.base IS NOT NULL""",
        (day_str(start), day_str(upto)),
    ).fetchall()
    per_check = defaultdict(lambda: (set(), set()))
    for r in rows:
        d, f = per_check[r["rid"]]
        (d if r["kind"] == "drink" else f).add(r["b"])
    total = len(per_check) or 1
    food_freq, drink_freq, pair = defaultdict(int), defaultdict(int), defaultdict(int)
    for drinks, foods in per_check.values():
        for x in foods:
            food_freq[x] += 1
        for x in drinks:
            drink_freq[x] += 1
        for dk in drinks:
            for fd in foods:
                pair[(dk, fd)] += 1
    out = []
    for (dk, fd), n in pair.items():
        if n < 10:
            continue
        expected = drink_freq[dk] * food_freq[fd] / total
        if expected <= 0:
            continue
        out.append(
            {
                "drink": dk,
                "food": fd,
                "checks": n,
                "lift": round(n / expected, 2),
                "rate": round(n / drink_freq[dk], 3),
            }
        )
    out.sort(key=lambda x: (x["lift"], x["checks"]), reverse=True)
    return out[:top]


def guests(conn, days=56, upto=None):
    """Постоянные гости — только если касса отдаёт карту лояльности.

    Кофейня живёт на возвращаемости: один гость, заходящий четырежды в неделю,
    стоит дороже четырёх случайных. Но без идентификатора гостя это НЕ
    вычисляется никаким способом, и притворяться нельзя.
    """
    upto = upto or last_day_with_data(conn)
    start = upto - timedelta(days=days - 1)
    rows = conn.execute(
        """SELECT t.guest guest, COUNT(*) visits, SUM(t.total) spent FROM (
              SELECT r.id, r.guest, SUM(i.qty*i.price) total
              FROM receipts r JOIN receipt_items i ON i.receipt_id=r.id
              WHERE substr(r.ts,1,10) BETWEEN ? AND ?
                    AND r.guest IS NOT NULL AND r.guest <> ''
              GROUP BY r.id) t
           GROUP BY t.guest""",
        (day_str(start), day_str(upto)),
    ).fetchall()
    if not rows:
        return {
            "available": False,
            "note": "касса не передаёт гостя (карту лояльности) — "
            "возвращаемость и частоту визитов посчитать нельзя",
        }
    total_checks = (
        conn.execute(
            "SELECT COUNT(*) c FROM receipts WHERE substr(ts,1,10) BETWEEN ? AND ?",
            (day_str(start), day_str(upto)),
        ).fetchone()["c"]
        or 1
    )
    ident = sum(r["visits"] for r in rows)
    repeat = [r for r in rows if r["visits"] >= 2]
    loyal = [r for r in rows if r["visits"] >= 8]
    spent_repeat = sum(r["spent"] for r in repeat)
    spent_all = sum(r["spent"] for r in rows) or 1
    return {
        "available": True,
        "guests": len(rows),
        "identified_share": round(ident / total_checks, 3),
        "repeat_share": round(len(repeat) / len(rows), 3),
        "loyal": len(loyal),
        "revenue_share_repeat": round(spent_repeat / spent_all, 3),
        "avg_visits": round(ident / len(rows), 1),
        "note": None,
    }


def barista_stats(conn, days=56, upto=None):
    """Работа бариста — только если касса отдаёт сотрудника."""
    upto = upto or last_day_with_data(conn)
    start = upto - timedelta(days=days - 1)
    rows = conn.execute(
        """SELECT t.barista, COUNT(*) checks, SUM(t.total) rev,
                  SUM(t.has_food) with_food, SUM(t.drinks) drinks
           FROM (SELECT r.id, r.barista,
                        SUM(i.qty*i.price) total,
                        MAX(CASE WHEN i.kind='food' AND i.qty>0 THEN 1 ELSE 0 END) has_food,
                        SUM(CASE WHEN i.kind='drink' AND i.qty>0 THEN i.qty ELSE 0 END) drinks
                 FROM receipts r JOIN receipt_items i ON i.receipt_id=r.id
                 WHERE substr(r.ts,1,10) BETWEEN ? AND ?
                       AND r.barista IS NOT NULL AND r.barista <> ''
                 GROUP BY r.id) t
           GROUP BY t.barista ORDER BY checks DESC""",
        (day_str(start), day_str(upto)),
    ).fetchall()
    if not rows:
        return {
            "available": False,
            "note": "касса не передаёт сотрудника — сравнить работу смен нельзя",
        }
    out = []
    for r in rows:
        checks = r["checks"] or 1
        out.append(
            {
                "barista": r["barista"],
                "checks": r["checks"],
                "revenue": round(r["rev"] or 0),
                "avg_check": round((r["rev"] or 0) / checks),
                "attach": round((r["with_food"] or 0) / checks, 3),
                "drinks": round(r["drinks"] or 0),
            }
        )
    return {"available": True, "items": out, "note": None}


# ---------- окна и разрезы ----------
WEEKDAY_RU = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
WEEKDAY_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def weekday_breakdown(conn, days=56, upto=None):
    """Средние чеки и выручка по каждому дню недели (+ число наблюдений)."""
    upto = upto or last_day_with_data(conn)
    start = upto - timedelta(days=days - 1)
    rows = conn.execute(
        """SELECT substr(r.ts,1,10) d, COUNT(DISTINCT r.id) checks, SUM(i.qty*i.price) rev
           FROM receipts r JOIN receipt_items i ON i.receipt_id=r.id
           WHERE substr(r.ts,1,10) BETWEEN ? AND ? GROUP BY d""",
        (day_str(start), day_str(upto)),
    ).fetchall()
    agg = defaultdict(lambda: [0, 0, 0.0])
    for row in rows:
        y, m, dd = map(int, row["d"].split("-"))
        wd = date(y, m, dd).weekday()
        agg[wd][0] += 1
        agg[wd][1] += row["checks"]
        agg[wd][2] += row["rev"]
    out = []
    for wd in range(7):
        n, ch, rev = agg[wd]
        out.append(
            {
                "weekday": WEEKDAY_RU[wd],
                "days": n,
                "avg_checks": round(ch / n) if n else 0,
                "avg_revenue": round(rev / n) if n else 0,
            }
        )
    return out


def weekday_profile(conn, days=56, upto=None):
    """Реальный профиль дней недели этой кофейни (а не усреднённый по рынку).
    Если данных мало — нейтральный профиль, чтобы не гадать."""
    upto = upto or last_day_with_data(conn)
    wd = weekday_breakdown(conn, days, upto)
    vals = [x["avg_revenue"] for x in wd]
    if sum(1 for x in wd if x["days"] >= 3 and x["avg_revenue"] > 0) < 5:
        return [1.0] * 7
    filled = [v for v in vals if v > 0]
    avg = sum(filled) / len(filled)
    prof = []
    for x, v in zip(wd, vals):
        if v <= 0 or x["days"] < 3:
            prof.append(1.0)
        else:
            prof.append(min(1.6, max(0.6, v / avg)))
    return prof


def revenue_by_category_window(conn, days=56, upto=None):
    upto = upto or last_day_with_data(conn)
    start = upto - timedelta(days=days - 1)
    rows = conn.execute(
        """SELECT COALESCE(i.category,'Прочее') cat, SUM(i.qty*i.price) rev
           FROM receipts r JOIN receipt_items i ON i.receipt_id=r.id
           WHERE substr(r.ts,1,10) BETWEEN ? AND ? GROUP BY cat ORDER BY rev DESC""",
        (day_str(start), day_str(upto)),
    ).fetchall()
    return _with_shares(rows)


def hour_breakdown(conn, days=56, upto=None):
    upto = upto or last_day_with_data(conn)
    start = upto - timedelta(days=days - 1)
    rows = conn.execute(
        """SELECT CAST(substr(r.ts,12,2) AS INT) h, COUNT(DISTINCT r.id) checks,
                  SUM(i.qty*i.price) rev,
                  SUM(CASE WHEN i.kind='drink' AND i.qty>0 THEN i.qty ELSE 0 END) cups,
                  COUNT(DISTINCT substr(r.ts,1,10)) days
           FROM receipts r JOIN receipt_items i ON i.receipt_id=r.id
           WHERE substr(r.ts,1,10) BETWEEN ? AND ? GROUP BY h ORDER BY h""",
        (day_str(start), day_str(upto)),
    ).fetchall()
    return [
        {
            "hour": r["h"],
            "checks": r["checks"],
            "rev": r["rev"],
            "cups": round(r["cups"] or 0),
            "days": r["days"],
        }
        for r in rows
    ]


def avg_check_window(conn, days, upto=None):
    upto = upto or last_day_with_data(conn)
    start = upto - timedelta(days=days - 1)
    row = conn.execute(
        """SELECT COUNT(DISTINCT r.id) checks, SUM(i.qty*i.price) rev
           FROM receipts r JOIN receipt_items i ON i.receipt_id=r.id
           WHERE substr(r.ts,1,10) BETWEEN ? AND ?""",
        (day_str(start), day_str(upto)),
    ).fetchone()
    checks = row["checks"] or 0
    rev = row["rev"] or 0
    return {
        "days": days,
        "checks": checks,
        "revenue": round(rev),
        "avg": round(rev / checks) if checks else 0,
    }


def top_products(conn, days=56, upto=None, n=8):
    upto = upto or last_day_with_data(conn)
    start = upto - timedelta(days=days - 1)
    rows = conn.execute(
        f"""SELECT {ITEM_KEY} name, SUM(i.qty) q, SUM(i.qty*i.price) rev
            FROM receipts r JOIN receipt_items i ON i.receipt_id=r.id
            WHERE substr(r.ts,1,10) BETWEEN ? AND ? AND i.kind<>'service'
            GROUP BY name ORDER BY q DESC LIMIT ?""",
        (day_str(start), day_str(upto), n),
    ).fetchall()
    return [{"name": r["name"], "qty": round(r["q"]), "rev": round(r["rev"])} for r in rows]


def operating_days(conn, days, upto):
    """Сколько дней в окне реально были продажи."""
    start = upto - timedelta(days=days - 1)
    row = conn.execute(
        "SELECT COUNT(DISTINCT substr(ts,1,10)) d FROM receipts WHERE substr(ts,1,10) BETWEEN ? AND ?",
        (day_str(start), day_str(upto)),
    ).fetchone()
    return max(1, row["d"] or 0)


def db_state(conn):
    """Дешёвая «отметка» состояния базы: если данные не менялись — можно взять кэш.

    В отметку входят и каталог, и справочники: маржа зависит от цен
    ингредиентов, а заказ витрины — от того, какие позиции в ней числятся.
    Дата тоже входит: «последний день с данными» зависит от сегодняшнего числа.
    """
    row = conn.execute(
        "SELECT (SELECT COUNT(*) FROM receipts), (SELECT MAX(ts) FROM receipts), "
        "(SELECT COUNT(*) FROM waste), (SELECT COUNT(*) FROM receipt_items), "
        "(SELECT COALESCE(SUM(qty*price),0) FROM receipt_items), "
        "(SELECT COUNT(*) FROM menu_items), "
        "(SELECT COALESCE(SUM(stocked),0) FROM menu_items), "
        "(SELECT COALESCE(MAX(name),'') FROM menu_items), "
        "(SELECT COALESCE(SUM(pack_price),0) FROM ingredients), "
        "(SELECT COUNT(*) FROM recipes)"
    ).fetchone()
    return tuple(row) + (config.today().isoformat(),)
