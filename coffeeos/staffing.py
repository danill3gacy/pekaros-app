"""Смена: утренний пик, пропускная способность и когда нужен второй бариста.

Витрина кончается — и это видно по чекам. А латте кончиться не может: кофейня
упирается не в запас, а в РУКИ. Утром между 8:00 и 9:30 гость
уходит не потому, что латте нет, а потому, что перед ним семь человек и он
опаздывает на работу.

Измерить ушедших по чекам невозможно: их там нет по определению. Поэтому
продукт не выдумывает «упущенную выручку на очереди», а делает то, что можно
сделать честно:

  • считает, сколько чашек кофейня физически выдаёт в час, когда старается —
    это её собственный рекорд, а не отраслевой норматив;
  • показывает часы, в которых она регулярно работает у этого потолка;
  • оценку прироста от второго бариста даёт ЯВНО как сценарий, с указанием
    допущения, а не как факт из кассы.

Разница между «мы теряем 40 000 ₽ на очереди» и «в эти два часа вы на пределе;
если поднять пропускную способность на 20%, это даст около 40 000 ₽ в месяц —
при условии, что спрос в очереди действительно есть» — это разница между
продуктом и обещанием.
"""

from collections import defaultdict
from datetime import timedelta

from .analytics import WEEKDAY_SHORT, avg_check_window, day_str, hour_breakdown, last_day_with_data

# Какую долю от собственного рекорда считаем работой «на пределе».
SATURATION = 0.80
# Во сколько раз час должен превышать МЕДИАННЫЙ час, чтобы считаться пиком.
# Без этого условия ровный день выглядит как сплошной пик: если поток одинаков
# все 12 часов, «рекорд» равен обычному часу, и загрузка каждого часа выходит
# 100%. Упор в потолок бывает только там, где есть настоящий наплыв.
PEAK_OVER_MEDIAN = 1.4
# Насколько вырастет пропускная способность со вторым бариста. Это допущение,
# и оно всегда печатается рядом с цифрой.
SECOND_BARISTA_UPLIFT = 0.20


def hourly_capacity(conn, days=56, upto=None):
    """Пропускная способность: сколько чашек в час кофейня выдаёт, когда старается.

    Берём не максимум за всю историю (один аномальный час не показатель), а
    высокий перцентиль по всем отработанным часам — уровень, который кофейня
    повторяет регулярно.
    """
    upto = upto or last_day_with_data(conn)
    start = upto - timedelta(days=days - 1)
    rows = conn.execute(
        """SELECT substr(r.ts,1,10) d, CAST(substr(r.ts,12,2) AS INT) h,
                  SUM(CASE WHEN i.kind='drink' AND i.qty>0 THEN i.qty ELSE 0 END) cups,
                  COUNT(DISTINCT r.id) checks
           FROM receipts r JOIN receipt_items i ON i.receipt_id=r.id
           WHERE substr(r.ts,1,10) BETWEEN ? AND ? GROUP BY d, h""",
        (day_str(start), day_str(upto)),
    ).fetchall()
    cups = sorted((r["cups"] or 0) for r in rows if (r["cups"] or 0) > 0)
    checks = sorted((r["checks"] or 0) for r in rows if (r["checks"] or 0) > 0)
    if len(cups) < 20:
        return {"known": False, "note": "мало отработанных часов, чтобы судить о пределе"}
    return {
        "known": True,
        "cups_per_hour": _pct(cups, 0.95),
        "checks_per_hour": _pct(checks, 0.95),
        "observed_hours": len(cups),
    }


def _pct(sorted_vals, p):
    if not sorted_vals:
        return 0
    i = min(len(sorted_vals) - 1, max(0, int(round(p * (len(sorted_vals) - 1)))))
    return sorted_vals[i]


def load_profile(conn, days=56, upto=None):
    """Средняя загрузка по часам и насколько она близка к собственному пределу."""
    upto = upto or last_day_with_data(conn)
    cap = hourly_capacity(conn, days, upto)
    hours = [h for h in hour_breakdown(conn, days, upto) if h["checks"] > 0]
    if not hours:
        return {"hours": [], "capacity": cap, "tight": []}
    for h in hours:
        nd = h["days"] or 1
        h["avg_checks"] = round(h["checks"] / nd, 1)
        h["avg_cups"] = round(h["cups"] / nd, 1)
        h["load"] = (
            round(h["avg_cups"] / cap["cups_per_hour"], 2) if cap.get("cups_per_hour") else None
        )
    median_cups = _median([h["avg_cups"] for h in hours]) or 0
    tight = [
        h
        for h in hours
        if h["load"] is not None
        and h["load"] >= SATURATION
        and h["avg_cups"] >= median_cups * PEAK_OVER_MEDIAN
    ]
    return {"hours": hours, "capacity": cap, "tight": tight, "median_cups": round(median_cups, 1)}


def _median(vals):
    s = sorted(vals)
    if not s:
        return 0.0
    return s[len(s) // 2] if len(s) % 2 else (s[len(s) // 2 - 1] + s[len(s) // 2]) / 2


def peak_hours(conn, days=56, upto=None):
    """Два разнесённых пика дня. Соседние часы — это один пик, а не два."""
    hours = [h for h in hour_breakdown(conn, days, upto) if h["checks"] > 0]
    if not hours:
        return []
    ranked = sorted(hours, key=lambda x: x["checks"], reverse=True)
    peaks = [ranked[0]]
    second = next((h for h in ranked[1:] if abs(h["hour"] - ranked[0]["hour"]) >= 3), None)
    if second:
        peaks.append(second)
    peaks.sort(key=lambda x: x["hour"])
    return peaks


def _windows(hours):
    """Склеить соседние часы в непрерывные окна."""
    out, run = [], []
    for h in sorted(hours):
        if run and h != run[-1] + 1:
            out.append((run[0], run[-1] + 1))
            run = []
        run.append(h)
    if run:
        out.append((run[0], run[-1] + 1))
    return out


def clipping_evidence(conn, hours, days=56, upto=None):
    """Проверить, действительно ли час упирается в потолок, а не просто загружен.

    Это и есть разница между измерением и догадкой. Если час просто популярный,
    число чашек в нём гуляет день ото дня. Если кофейня в этот час физически не
    успевает, распределение по дням «срезано сверху»: изо дня в день получается
    один и тот же максимум, потому что больше рук нет.

    Возвращает по каждому часу долю дней, в которые он упёрся в собственный
    потолок. Без этого числа рассуждать о втором бариста нельзя.
    """
    upto = upto or last_day_with_data(conn)
    start = upto - timedelta(days=days - 1)
    cap = hourly_capacity(conn, days, upto)
    if not cap.get("known") or not hours:
        return {}
    ceiling = cap["cups_per_hour"]
    rows = conn.execute(
        """SELECT CAST(substr(r.ts,12,2) AS INT) h, substr(r.ts,1,10) d,
                  SUM(CASE WHEN i.kind='drink' AND i.qty>0 THEN i.qty ELSE 0 END) cups
           FROM receipts r JOIN receipt_items i ON i.receipt_id=r.id
           WHERE substr(r.ts,1,10) BETWEEN ? AND ? GROUP BY h, d""",
        (day_str(start), day_str(upto)),
    ).fetchall()
    per_hour = defaultdict(list)
    for r in rows:
        per_hour[r["h"]].append(r["cups"] or 0)
    out = {}
    for h in hours:
        vals = per_hour.get(h, [])
        if len(vals) < 10:
            continue
        clipped = sum(1 for v in vals if v >= ceiling * 0.95)
        out[h] = {
            "days": len(vals),
            "clipped_days": clipped,
            "share": round(clipped / len(vals), 3),
            "ceiling": ceiling,
            "avg": round(sum(vals) / len(vals), 1),
        }
    return out


# Доля дней, с которой упор в потолок считается подтверждённым, а не случайным.
CLIP_CONFIRMED = 0.25


def shift_plan(conn, days=56, upto=None):
    """Где на смене реально нужен второй человек — и что это может дать.

    Прирост считается как СЦЕНАРИЙ и подписан как сценарий: сколько ушло из
    очереди, по чекам не видно и видно быть не может.
    """
    upto = upto or last_day_with_data(conn)
    prof = load_profile(conn, days, upto)
    hours = prof["hours"]
    if not hours:
        return {"windows": [], "note": "нет данных о часах продаж"}
    cap = prof["capacity"]
    tight_hours = [h["hour"] for h in prof["tight"]]
    windows = _windows(tight_hours)

    total_checks = sum(h["checks"] for h in hours) or 1
    share = sum(h["checks"] for h in hours if h["hour"] in tight_hours) / total_checks

    clip = clipping_evidence(conn, tight_hours, days, upto)
    confirmed = {h: c for h, c in clip.items() if c["share"] >= CLIP_CONFIRMED}

    scenario, note = None, None
    if not windows:
        note = "поток ровный в течение дня — разгонять смену смысла нет"
    elif not confirmed:
        note = (
            "Загруженные часы есть, но упор в потолок не подтверждается: "
            "число чашек в них гуляет день ото дня, а не упирается в одно и то же "
            "число. Значит, второй бариста ускорит выдачу, но новых чеков сам по "
            "себе не создаст."
        )
    else:
        avg_check = avg_check_window(conn, days, upto)["avg"]
        # Прирост считаем ТОЛЬКО за те дни, когда час действительно упёрся в
        # потолок. В остальные дни второй бариста лишних чеков не добавит —
        # спроса на них просто нет.
        extra_day = sum(
            c["ceiling"] * SECOND_BARISTA_UPLIFT * c["share"] for c in confirmed.values()
        )
        scenario = {
            "uplift": SECOND_BARISTA_UPLIFT,
            "confirmed_hours": sorted(confirmed),
            "clipped_share": round(sum(c["share"] for c in confirmed.values()) / len(confirmed), 2),
            "extra_checks_per_day": round(extra_day, 1),
            "money_per_month": round(extra_day * avg_check * 30),
            "assumption": (
                f"допущение: второй бариста поднимает пропускную способность на "
                f"{int(SECOND_BARISTA_UPLIFT * 100)}%, и очередь, которая упиралась в "
                f"потолок, действительно дожидается. Прирост посчитан только за дни, "
                f"когда упор в потолок подтверждён по чекам. Это оценка, а не факт: "
                f"ушедших из очереди в кассе нет и быть не может."
            ),
        }
    return {
        "windows": windows,
        "tight_share": round(share, 3),
        "capacity": cap,
        "peaks": peak_hours(conn, days, upto),
        "quietest": min(hours, key=lambda x: x["checks"]),
        "clipping": clip,
        "scenario": scenario,
        "note": note,
    }


def weekday_load(conn, days=56, upto=None):
    """Загрузка по дням недели в чашках — для расстановки смен на неделю."""
    upto = upto or last_day_with_data(conn)
    start = upto - timedelta(days=days - 1)
    rows = conn.execute(
        """SELECT substr(r.ts,1,10) d,
                  SUM(CASE WHEN i.kind='drink' AND i.qty>0 THEN i.qty ELSE 0 END) cups
           FROM receipts r JOIN receipt_items i ON i.receipt_id=r.id
           WHERE substr(r.ts,1,10) BETWEEN ? AND ? GROUP BY d""",
        (day_str(start), day_str(upto)),
    ).fetchall()
    agg = defaultdict(lambda: [0, 0.0])
    from datetime import date as _date

    for r in rows:
        y, m, dd = map(int, r["d"].split("-"))
        wd = _date(y, m, dd).weekday()
        agg[wd][0] += 1
        agg[wd][1] += r["cups"] or 0
    return [
        {
            "weekday": WEEKDAY_SHORT[wd],
            "days": agg[wd][0],
            "avg_cups": round(agg[wd][1] / agg[wd][0]) if agg[wd][0] else 0,
        }
        for wd in range(7)
    ]
