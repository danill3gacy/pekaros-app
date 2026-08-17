"""Витрина: спрос, распроданность, заказ на завтра и упущенная выручка.

Этот движок пришёл из пекарной версии и там был главным. В кофейне он
сохраняется целиком, но применяется ТОЛЬКО к витрине — еде и десертам с
конечным запасом. Напитки через него не проходят никогда: латте делается на
заказ и кончиться не может, а если пустить его сюда, система начнёт
«обнаруживать распроданность латте» каждый вечер после закрытия.

Три вещи, которых не делает «средняя за две недели» из таблички:

1. **Продажи — это не спрос.** Продажи урезаны наличием: если круассаны
   кончились в 11:20, продажи показывают ровно то, что успели поставить.
   Такие дни находятся по чекам, и спрос восстанавливается по условному
   матожиданию усечённого распределения.

2. **День недели учитывается усадкой**, а не жёстким правилом: одна суббота с
   ярмаркой не утраивает заказ, но и восемь суббот не игнорируются.

3. **Запас берётся из целевого уровня сервиса**, а не «на глаз».

Признак распроданности виден прямо в чеках, без ежевечернего ввода остатков.
Это принципиально: текучесть бариста — 4–6 месяцев, и продукт, который держится
на ручном вводе, перестаёт работать через месяц вместе с человеком, которого
научили.
"""
from datetime import date, timedelta
from collections import defaultdict
from statistics import NormalDist

from . import config, economics, menu
from .analytics import day_str, last_day_with_data, operating_days

_ND = NormalDist()

CASE_KINDS = (menu.KIND_FOOD,)      # витрина: только то, что имеет конечный запас


# ---------- распродано ли? (по чекам, без ручного ввода) ----------
SELLOUT_MIN_RECEIPTS = 3          # по одной-двум продажам судить нельзя
SELLOUT_EARLY_HOURS = 2           # насколько раньше СВОЕГО обычного часа кончилась позиция
WASTE_MIN_UNITS = 2               # сколько штук должно остаться, чтобы это был «не распродано»
WASTE_MIN_SHARE = 0.03            # …либо столько от дневных продаж позиции


def _daily_item_sales(conn, days, upto):
    """По каждой (позиция витрины, день): продано, в скольких чеках, час последней ПРОДАЖИ.

    Возвраты (qty < 0) не могут задавать «час последней продажи»: возврат в
    20:40 делал вид, что товар продавался до самого закрытия, распроданность
    переставала определяться, и упущенная выручка молча обнулялась.
    """
    start = upto - timedelta(days=days - 1)
    rows = conn.execute(
        """SELECT i.base name, substr(r.ts,1,10) d, SUM(i.qty) q,
                  COUNT(DISTINCT CASE WHEN i.qty > 0 THEN r.id END) n,
                  MAX(CASE WHEN i.qty > 0 THEN CAST(substr(r.ts,12,2) AS INT) END) last_h
           FROM receipts r JOIN receipt_items i ON i.receipt_id=r.id
           WHERE substr(r.ts,1,10) BETWEEN ? AND ? AND i.kind='food' AND i.base IS NOT NULL
           GROUP BY i.base, d""", (day_str(start), day_str(upto))).fetchall()
    return [dict(r) for r in rows]


def _tail_traffic_share(conn, days, upto):
    """Доля чеков дня, пришедшая ПОСЛЕ каждого часа: {день: {час: доля}}."""
    start = upto - timedelta(days=days - 1)
    rows = conn.execute(
        """SELECT substr(ts,1,10) d, CAST(substr(ts,12,2) AS INT) h, COUNT(*) c
           FROM receipts WHERE substr(ts,1,10) BETWEEN ? AND ? GROUP BY d, h""",
        (day_str(start), day_str(upto))).fetchall()
    by_day = defaultdict(dict)
    for r in rows:
        by_day[r["d"]][r["h"]] = r["c"]
    out = {}
    for d, hours in by_day.items():
        total = sum(hours.values()) or 1
        after = {}
        for h in range(0, 24):
            after[h] = sum(c for hh, c in hours.items() if hh > h) / total
        out[d] = after
    return out


def _median(vals):
    s = sorted(vals)
    if not s:
        return None
    return s[len(s) // 2] if len(s) % 2 else (s[len(s) // 2 - 1] + s[len(s) // 2]) / 2


def sellout_days(conn, days=56, upto=None, rows=None):
    """Дни, когда позиция витрины, судя по чекам, кончилась раньше закрытия.

    Возвращает {позиция: {дата: доля дневного трафика, оставшаяся после распродажи}}.

    Два независимых условия, оба обязательны:

    1. Статистическое. Если после последней продажи позиции прошла доля f
       дневного трафика, а позиция была в n чеках, случайно так совпасть можно
       с вероятностью (1−f)^n. Меньше порога — подозрительно.

    2. Сравнение позиции С САМОЙ СОБОЙ. Первого условия мало: оно исходит из
       того, что позиция одинаково вероятна в любом чеке дня, а для кофейни это
       особенно неверно — завтраки и сырники «обрываются» к полудню каждый
       день, ничуть не кончаясь. Поэтому день засчитывается, только если
       позиция кончилась минимум на два часа раньше СВОЕГО обычного часа.

    Списания, если их ведут, имеют приоритет: было непроданное — не распродажа.

    Чего метод принципиально не видит: позицию, которая кончается рано КАЖДЫЙ
    день. Тогда её «обычный час» и есть ранний, и сравнивать не с чем. Такое
    ловится только вводом списаний либо увеличением заказа вручную.
    """
    upto = upto or last_day_with_data(conn)
    start = upto - timedelta(days=days - 1)
    rows = rows if rows is not None else _daily_item_sales(conn, days, upto)
    tail = _tail_traffic_share(conn, days, upto)
    # Списание отменяет признак распродажи, но не любое: один списанный
    # круассан (брак, витринный образец) — не повод считать, что еды хватило
    # всем. Иначе одна строка от бариста гасила сигнал за весь день.
    sold_that_day = {(r["name"], r["d"]): (r["q"] or 0) for r in rows}
    had_waste = set()
    for r in conn.execute("SELECT date, name, SUM(qty) q FROM waste "
                          "WHERE date BETWEEN ? AND ? AND qty > 0 AND kind='food' "
                          "GROUP BY name, date",
                          (day_str(start), day_str(upto))):
        left = r["q"] or 0
        sold = sold_that_day.get((r["name"], r["date"]), 0)
        if left >= max(WASTE_MIN_UNITS, sold * WASTE_MIN_SHARE):
            had_waste.add((r["name"], r["date"]))

    typical_last = defaultdict(list)
    for row in rows:
        if (row["n"] or 0) >= SELLOUT_MIN_RECEIPTS and row["last_h"] is not None:
            typical_last[row["name"]].append(row["last_h"])
    typical_last = {k: _median(v) for k, v in typical_last.items()}

    out = defaultdict(dict)
    for row in rows:
        n = row["n"] or 0
        if n < SELLOUT_MIN_RECEIPTS or row["last_h"] is None:
            continue
        if (row["name"], row["d"]) in had_waste:
            continue                              # осталось непроданное — не распродажа
        usual = typical_last.get(row["name"])
        if usual is None or row["last_h"] > usual - SELLOUT_EARLY_HOURS:
            continue                              # для этой позиции такой час — норма
        f = tail.get(row["d"], {}).get(row["last_h"], 0.0)
        if f <= 0:
            continue
        if (1.0 - f) ** n < config.SELLOUT_P:
            out[row["name"]][row["d"]] = f
    return dict(out)


# ---------- спрос (а не продажи) ----------
WEEKDAY_SHRINK = 3.0      # «виртуальных» наблюдений общего среднего на каждый день недели
CENSOR_CAP = 1.35         # спрос не может быть оценён выше продаж более чем в 1.35 раза
SIGMA_CAP = 0.60          # сигма не выше 60% среднего: дальше это уже не оценка, а фантазия
CONFIDENCE_DAYS = 21      # с меньшей историей не гонимся за хвостом распределения
RECENCY_HALF_LIFE = 21.0  # вес наблюдения падает вдвое каждые три недели


def _mills(z):
    """Отношение Миллса: на сколько сигм в среднем спрос превышает уровень z."""
    tail = 1.0 - _ND.cdf(z)
    if tail < 1e-9:
        return max(0.0, -z) + 5.0
    return _ND.pdf(z) / tail


_DEMAND_CACHE = {}     # (состояние базы, день недели, окно, upto) -> готовая оценка


def demand_stats(conn, target_weekday, days=56, upto=None):
    """Оценка спроса с кэшем: одна сводка считала её несколько раз подряд."""
    from .analytics import db_state
    upto = upto or last_day_with_data(conn)
    key = (target_weekday, days, day_str(upto)) + db_state(conn)
    cached = _DEMAND_CACHE.get(key)
    if cached is not None:
        return cached
    _DEMAND_CACHE.clear()                 # держим только актуальную оценку
    res = _demand_stats_uncached(conn, target_weekday, days, upto)
    _DEMAND_CACHE[key] = res
    return res


def _stdev(vals, mean, df_used=1):
    """Выборочное отклонение. df_used — сколько средних уже оценено по этим данным."""
    n = len(vals)
    if n < 2:
        return 0.0
    m = mean if mean is not None else sum(vals) / n
    df = max(1, n - max(1, df_used))
    return (sum((v - m) ** 2 for v in vals) / df) ** 0.5


def _demand_stats_uncached(conn, target_weekday, days=56, upto=None):
    """Оценка СПРОСА (не продаж) по каждой позиции витрины на заданный день недели.

    Возвращает {позиция: {mu, sd, sold_avg, days, sellouts, lost_units}}.
    """
    upto = upto or last_day_with_data(conn)
    rows = _daily_item_sales(conn, days, upto)
    sellouts = sellout_days(conn, days, upto, rows=rows)
    # Рабочие дни берём из ЧЕКОВ кофейни, а не из дней продаж позиции: иначе у
    # кофейни с одной-двумя позициями витрины «рабочими» окажутся только те
    # дни, когда эти позиции продавались, и любой день станет похож на нужный.
    _start = upto - timedelta(days=days - 1)
    all_days = [r["d"] for r in conn.execute(
        "SELECT DISTINCT substr(ts,1,10) d FROM receipts WHERE substr(ts,1,10) BETWEEN ? AND ? "
        "ORDER BY d", (day_str(_start), day_str(upto)))]
    if not all_days:
        return {}
    day_pos = {d: i for i, d in enumerate(all_days)}
    dow_of = {}
    for d in all_days:
        y, m, dd = map(int, d.split("-"))
        dow_of[d] = date(y, m, dd).weekday()

    by_item = defaultdict(dict)                   # позиция -> {дата: продано}
    for r in rows:
        by_item[r["name"]][r["d"]] = r["q"] or 0

    out = {}
    for name, sold_by_day in by_item.items():
        # Дни считаем С МОМЕНТА ПОЯВЛЕНИЯ позиции, а не по всей выгрузке.
        # Иначе новинка, появившаяся вчера, делится на 56 дней и получает заказ
        # «1 шт» при реальном спросе 40 — из которого ей уже не выбраться.
        first = min(day_pos[d] for d in sold_by_day)
        life = all_days[first:]
        n_days = len(life)
        sold_total = sum(sold_by_day.values())
        sold_avg = sold_total / n_days            # дни без продаж внутри жизни — настоящие нули
        if sold_avg <= 0:
            continue
        sold_vals = [sold_by_day.get(d, 0) for d in life]
        mu0 = max(0.0, sold_avg)
        sd0 = max(_stdev(sold_vals, mu0), mu0 ** 0.5)

        # --- поправка на распроданность: в такие дни спрос был выше продаж ---
        so = sellouts.get(name, {})
        corrected, lost_units = {}, 0.0
        for d in life:
            q = sold_by_day.get(d, 0)
            if d in so and sd0 > 0:
                z = (q - mu0) / sd0
                est = min(mu0 + sd0 * _mills(z), q * CENSOR_CAP)   # потолок на поправку
                lost_units += max(0.0, est - q)
                corrected[d] = est
            else:
                corrected[d] = q

        # --- средний спрос по дню недели с усадкой к общему среднему ---
        # Свежие дни весят больше старых: спрос не стоит на месте (сезон, район,
        # соседи, цены). Простое среднее по 56 дням отстаёт от тренда.
        w_of = {d: 0.5 ** ((n_days - 1 - i) / RECENCY_HALF_LIFE) for i, d in enumerate(life)}
        wd_sum, wd_cnt, raw_sum = defaultdict(float), defaultdict(float), defaultdict(float)
        for d in life:
            w = w_of[d]
            wd_sum[dow_of[d]] += corrected[d] * w
            raw_sum[dow_of[d]] += sold_by_day.get(d, 0) * w
            wd_cnt[dow_of[d]] += w
        w_total = sum(w_of.values()) or 1.0
        mu_all = sum(corrected[d] * w_of[d] for d in life) / w_total
        raw_all = sum(sold_by_day.get(d, 0) * w_of[d] for d in life) / w_total
        n_w = wd_cnt.get(target_weekday, 0)
        mu_w = (wd_sum[target_weekday] / n_w) if n_w else mu_all
        raw_w = (raw_sum[target_weekday] / n_w) if n_w else raw_all
        mu = (n_w * mu_w + WEEKDAY_SHRINK * mu_all) / (n_w + WEEKDAY_SHRINK)
        # Предохранитель на поправку сравнивается с ТЕМ ЖЕ срезом сырых продаж
        # (тот же день недели, та же усадка), иначе он режет сезонность.
        mu_raw = (n_w * raw_w + WEEKDAY_SHRINK * raw_all) / (n_w + WEEKDAY_SHRINK)
        mu = max(0.0, min(mu, mu_raw * CENSOR_CAP))

        # --- разброс: остатки вокруг средних по дням недели ---
        resid = [corrected[d] - wd_sum[dow_of[d]] / wd_cnt[dow_of[d]] for d in life]
        # степеней свободы меньше на число оценённых средних по дням недели —
        # иначе на короткой истории сигма занижается вдвое
        sd = max(_stdev(resid, 0.0, df_used=len(wd_cnt)), mu ** 0.5)
        sd = min(sd, mu * SIGMA_CAP)

        out[name] = {"mu": mu, "sd": sd, "mu0": mu0, "sd0": sd0,
                     "sold_avg": sold_avg, "days": n_days, "first_day": life[0],
                     "sellouts": len(so), "lost_units": lost_units / n_days,
                     "confidence": min(1.0, n_days / CONFIDENCE_DAYS)}
    return out


def _waste_by_day_map(conn, days, upto):
    """Списания витрины по каждой (позиция, день) — чтобы усреднять их по тем же
    дням, что и продажи. Общий знаменатель важен: у новинки, живущей три дня,
    средние списания по всем 56 дням окна выглядят как «2 шт», хотя выбрасывают 25."""
    start = upto - timedelta(days=days - 1)
    rows = conn.execute(
        "SELECT name, date, SUM(qty) q FROM waste WHERE date BETWEEN ? AND ? AND kind='food' "
        "GROUP BY name, date", (day_str(start), day_str(upto))).fetchall()
    out = defaultdict(dict)
    for r in rows:
        out[r["name"]][r["date"]] = r["q"] or 0
    return dict(out)


# ---------- заказ витрины на завтра ----------
def case_order(conn, target_day=None, days=56):
    """Сколько чего ставить в витрину: с запасом под верхний перцентиль спроса.

    Заказ = средний спрос + z·сигма, где z задаётся целевым уровнем сервиса.
    Недостающая единица стоит дороже непроданной: первая — это гость, который
    взял только кофе (а завтра пошёл завтракать в другое место), вторая — лишь
    остаток. Поэтому сознательно берём с запасом.
    """
    last = last_day_with_data(conn)
    target_day = target_day or (last + timedelta(days=1))
    stats = demand_stats(conn, target_day.weekday(), days, last)
    waste_days = _waste_by_day_map(conn, days, last)   # то же окно, что и у спроса
    stocked = conn.execute(
        "SELECT name, category FROM menu_items WHERE stocked=1 AND kind='food'").fetchall()

    econ = economics.plan_z()
    order = []
    for p in stocked:
        name = p["name"]
        # Служебные позиции кассы в заказ витрины не идут никогда. Полагаться
        # только на stocked=1 нельзя: флаг мог прийти из старой базы или
        # сторонней правки.
        if menu.is_service_item(name):
            continue
        st = stats.get(name)
        if not st or st["mu"] < 1:
            continue
        # мало истории — не гонимся за хвостом: сигма ещё не оценена
        z = econ["z"] * st["confidence"]
        recommended = max(0, int(round(st["mu"] + z * st["sd"])))
        sold = st["sold_avg"]
        diff = recommended - int(round(sold))
        # списания усредняем по дням ЖИЗНИ позиции — тому же знаменателю, что и
        # продажи, иначе сравнение «много ли остаётся» теряет смысл на новинках
        wd = waste_days.get(name, {})
        waste = sum(q for d, q in wd.items() if d >= st["first_day"]) / max(1, st["days"])

        if st["sellouts"] >= max(2, st["days"] * 0.15):
            note = f"кончается раньше закрытия ({st['sellouts']} дн.) — берём с запасом"
            tag = "up"
        elif waste > max(3, sold * 0.15):
            note = "стабильно много остаётся — заказ по спросу"
            tag = "down"
        elif diff >= 2:
            note = f"+{diff} · запас под пик спроса"
            tag = "up"
        elif diff <= -2:
            note = f"{diff} · спрос ниже обычного"
            tag = "down"
        else:
            note = "по спросу"
            tag = "flat"

        order.append({"name": name, "sold_avg": round(sold), "waste_avg": round(waste),
                      "recommended": recommended, "note": note, "tag": tag,
                      "demand_avg": round(st["mu"], 1), "sd": round(st["sd"], 1),
                      "sellout_days": st["sellouts"],
                      "service_level": round(econ["service_level"], 2)})
    # решение владельца важнее расчёта
    overrides = order_overrides(conn, target_day)
    for it in order:
        if it["name"] in overrides:
            it["recommended"] = int(round(overrides[it["name"]]))
            it["adjusted"] = True
            it["note"] = "вы поправили вручную"
            it["tag"] = "flat"
    order.sort(key=lambda x: x["recommended"], reverse=True)
    return {"target": day_str(target_day), "items": order}


def set_order_override(conn, day, name, qty):
    """Владелец поправил заказ вручную — сохранить решение."""
    from .catalog import find_item
    nm = find_item(conn, name) or name
    qty = max(0, int(round(float(qty))))
    conn.execute("INSERT INTO case_order_override(date,name,qty) VALUES(?,?,?) "
                 "ON CONFLICT(date,name) DO UPDATE SET qty=excluded.qty",
                 (day_str(day), nm, qty))
    conn.commit()
    return {"name": nm, "qty": qty}


def order_overrides(conn, day):
    return {r["name"]: r["qty"] for r in conn.execute(
        "SELECT name, qty FROM case_order_override WHERE date=?", (day_str(day),))}


# ---------- деньги: упущенная выручка ----------
def _price_map(conn, days, upto):
    """Средняя цена продажи по каждой позиции витрины за период."""
    start = upto - timedelta(days=days - 1)
    rows = conn.execute(
        """SELECT i.base name, SUM(i.qty*i.price) rev, SUM(i.qty) q
           FROM receipts r JOIN receipt_items i ON i.receipt_id=r.id
           WHERE substr(r.ts,1,10) BETWEEN ? AND ? AND i.kind='food'
           GROUP BY i.base""", (day_str(start), day_str(upto))).fetchall()
    return {r["name"]: ((r["rev"] / r["q"]) if r["q"] else 0, r["rev"], r["q"]) for r in rows}


def sellouts_for_day(conn, day, days=56):
    """Что кончилось в конкретный день, во сколько и во сколько это обошлось."""
    ds = day_str(day)
    so = sellout_days(conn, days, day)
    stats = demand_stats(conn, day.weekday(), days, day)
    prices = _price_map(conn, days, day)
    today_rows = _daily_item_sales(conn, 1, day)
    last_hour = {r["name"]: r["last_h"] for r in today_rows}
    sold = {r["name"]: r["q"] for r in today_rows}

    out = []
    for name, dmap in so.items():
        if ds not in dmap or menu.is_service_item(name):
            continue
        st = stats.get(name)
        if not st or st["sd0"] <= 0:
            continue
        # те же mu0/sd0, что и в demand_stats: две разные оценки одной величины
        # в одном сообщении бота — это вопрос «а какой цифре верить»
        v = sold.get(name, 0)
        z = (v - st["mu0"]) / st["sd0"]
        lost = max(0.0, min(st["mu0"] + st["sd0"] * _mills(z), v * CENSOR_CAP) - v)
        price = prices.get(name, (0, 0, 0))[0]
        out.append({"name": name, "hour": last_hour.get(name),
                    "lost_units": round(lost, 1),
                    "lost_money": round(lost * price)})
    out.sort(key=lambda x: x["lost_money"], reverse=True)
    return {"date": ds, "items": out, "total": sum(i["lost_money"] for i in out)}


def lost_sales_report(conn, days=56, upto=None):
    """Упущенная выручка витрины: сколько недопродано из-за того, что еда кончалась.

    Этих денег не видно в кассе: она показывает проданное, а не гостя, который
    хотел завтрак, не нашёл его и взял только кофе.
    """
    upto = upto or last_day_with_data(conn)
    stats = demand_stats(conn, upto.weekday(), days, upto)
    prices = _price_map(conn, days, upto)
    start = upto - timedelta(days=days - 1)
    row = conn.execute(
        "SELECT COUNT(DISTINCT substr(ts,1,10)) d, MIN(substr(ts,1,10)) first "
        "FROM receipts WHERE substr(ts,1,10) BETWEEN ? AND ?",
        (day_str(start), day_str(upto))).fetchone()
    operating = row["d"] or 1
    # Долю рабочих дней считаем от реально покрытого интервала истории, а не от
    # длины окна: при 45 днях данных в окне 56 недостающие дни выглядели как
    # выходные, и «в месяц» занижалось на 20%.
    span = days
    if row["first"]:
        y, m, dd = map(int, row["first"].split("-"))
        span = min(days, (upto - date(y, m, dd)).days + 1)
    days_per_month = max(1.0, min(31.0, operating / max(1, span) * 30.0))
    ndays = 0
    items = []
    for name, st in stats.items():
        if st["sellouts"] == 0 or menu.is_service_item(name):
            continue
        ndays = max(ndays, st["days"])
        price = prices.get(name, (0, 0, 0))[0]
        units_day = st["lost_units"]
        money_day = units_day * price
        per_month = money_day * days_per_month
        items.append({"name": name, "sellout_days": st["sellouts"],
                      "lost_units_per_day": round(units_day, 1),
                      "lost_money_per_day": round(money_day),
                      "lost_money_per_month": round(per_month)})
    items.sort(key=lambda x: x["lost_money_per_month"], reverse=True)
    return {"items": items, "days": ndays or 1,
            "total_per_day": round(sum(i["lost_money_per_day"] for i in items)),
            "total_per_month": round(sum(i["lost_money_per_month"] for i in items))}


def forecast_checks(conn, target_day=None):
    """Прогноз потока чеков на день — по этому же дню недели за 4 недели."""
    last = last_day_with_data(conn)
    target_day = target_day or (last + timedelta(days=1))
    from .analytics import sales_summary
    vals = []
    for w in range(1, 5):
        d = target_day - timedelta(days=7 * w)
        s = sales_summary(conn, d)
        if s["checks"]:
            vals.append(s["checks"])
    base = sum(vals) / len(vals) if vals else sales_summary(conn, last)["checks"]
    return {"target": day_str(target_day), "low": int(base * 0.94), "high": int(base * 1.08)}
