"""Оркестратор — понимает запрос, зовёт нужный расчёт, формулирует ответ.

Маршрутизация по ключевым словам работает всегда и без интернета. Если
настроена языковая модель — свободные вопросы уходят к ней вместе со срезом
данных кофейни (опционально; система полностью работает и без неё).
"""

import re
from datetime import timedelta
from typing import Any

from . import analytics, catalog, config, costing, demand, llm, menu, staffing, supply
from .analytics import fmt, last_day_with_data
from .menu import CATEGORY_EMOJI

# Порядок важен: словарь просматривается сверху вниз, выигрывает первое
# совпадение. Узкие темы должны стоять раньше широкой «выручки», иначе она
# перехватывает их вопросы.
INTENTS = {
    "case": r"витрин|заказ выпеч|сколько заказ|что заказать на завтра|"
    r"сколько ставить|заказ на завтра|десерт",
    "margin": r"маржа|маржинальн|себестоим|фудкост|foodcost|наценк|"
    r"сколько зараба(?:тыва|ты)|прибыльн|невыгодн|разбор меню",
    "supply": r"закуп|заявк|поставщик|заказать|зерно|молок[оа]|сироп|стакан|"
    r"расходник|на сколько хват|остат",
    # «недопродали» проверяется РАНЬШЕ attach: слово содержит «допрода», и
    # вопрос об упущенной выручке уезжал в раздел про еду к кофе.
    "lost": r"недопрод|упущ|кончал|кончил|закончил|не хват|распрода|не досчит",
    "attach": r"attach|к кофе|вместе с|допрода|средний чек подня|апселл|"
    r"еда к кофе|корзин",
    "cash": r"\bкасс|сверк|эквайр|безнал|наличн",
    "shift": r"смен|график|персонал|бариста|второй человек|очеред|загрузк|пик",
    "waste": r"списан|списал|списат|спиши|непродан|выброс|вылил|остал",
    "drinks": r"напитк|что пьют|размер|молоко альтернатив|растительн|айс|холодн",
    "audit": r"аудит|как дела|итог|сводк|экспресс",
    "revenue": r"выручк|оборот|категор|сколько заработ|продаж",
}


def route(text):
    low = text.lower()
    for intent, pat in INTENTS.items():
        if re.search(pat, low):
            return intent
    return None


# ---------- разметка ----------
# Названия позиций приходят из выгрузки кассы и могут содержать символы
# разметки Telegram: «Кофе 3_в_1», «Латте *новинка*». При нечётном их числе
# Telegram отклоняет сообщение, при чётном — молча съедает символы и включает
# курсив. Поэтому любое название подставляем через _p().
_MD_SPECIAL = ("_", "*", "`", "[")


def _p(name):
    """Название, безопасное для Markdown-разметки Telegram."""
    s = str(name if name is not None else "")
    s = s.replace("\\", "\\\\")  # сам слэш — первым, иначе он съест экранирование
    for ch in _MD_SPECIAL:
        s = s.replace(ch, "\\" + ch)
    return s


def plain(text):
    """Тот же ответ без разметки — для веба и логов."""
    out, i = [], 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text) and text[i + 1] in _MD_SPECIAL + ("\\",):
            out.append(text[i + 1])
            i += 2
            continue
        if ch in ("*", "`", "_"):
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _pct(x, digits=0):
    """Доля в процентах. None — это «не посчитано», а не ноль.

    Раньше None доходил сюда и ронял ответ: у позиции с нулевой ценой (акция,
    служебная строка кассы) фудкост не определён, и раздел «Молоко» падал
    целиком вместо того, чтобы показать прочерк в одной ячейке.
    """
    if x is None:
        return "—"
    return f"{x * 100:.{digits}f}%"


NO_DATA = (
    "🔴 *Нет данных о продажах.*\n\n"
    "Сводку собирать не из чего. Обычно это значит, что перестала "
    "приходить выгрузка чеков или база пустая.\n"
    "Проверьте: `python -m coffeeos status`, затем загрузите выгрузку: "
    "`python -m coffeeos import файл.csv`"
)


def _quality_note(conn):
    q = analytics.data_quality(conn)
    if q["ok"]:
        return None
    return "⚠️ " + " ".join(q["warnings"])


# ---------- ответы ----------
def answer_case(conn):
    """Что и сколько ставить в витрину завтра."""
    if not analytics.has_sales(conn):
        return NO_DATA
    p = demand.case_order(conn)
    if not p["items"]:
        return (
            "🥐 *Заказ витрины*\n\nПока нечего заказывать: в каталоге нет позиций "
            "витрины с достаточной историей продаж.\n"
            "Проверьте каталог — напишите «каталог»."
        )
    lines = [f"🥐 *Витрина на {p['target']}*", ""]
    for it in p["items"][:12]:
        arrow = "🔺" if it["tag"] == "up" else ("🔻" if it["tag"] == "down" else "•")
        lines.append(
            f"{arrow} {_p(it['name'])}: *{it['recommended']}* шт  "
            f"(продаётся ~{it['sold_avg']}, {it['note']})"
        )
    lines.append("")
    lines.append(
        "_Заказ намеренно выше среднего спроса: пустая витрина стоит дороже "
        "остатка — гость берёт только кофе, а завтракать идёт в другое место._"
    )
    lines.append("Нажмите кнопку ниже, чтобы отправить заказ смене.")
    return "\n".join(lines)


def answer_margin(conn):
    """Где деньги: маржа, foodcost и разбор меню."""
    if not analytics.has_sales(conn):
        return NO_DATA
    t = costing.totals(conn)
    lines = ["💰 *Маржа и себестоимость*", ""]
    if not t["revenue_costed"]:
        return (
            "💰 *Маржа и себестоимость*\n\nПока не посчитать: не заданы закупочные "
            "цены. Напишите «цена зерно 1800» (за килограмм), «цена молоко обычное 95» "
            "(за литр) — и я посчитаю маржу каждой позиции.\n"
            "Что именно не хватает — покажет «цены»."
        )
    lines.append(f"Выручка заведения *{fmt(t['revenue'])} ₽*")
    lines.append(
        f"Из неё посчитано {_pct(t['coverage'])}: сырьё {fmt(t['cost'])} ₽ · "
        f"маржа *{fmt(t['margin'])} ₽*"
    )
    lines.append(f"Фудкост *{_pct(t['foodcost'], 1)}* (ориентир {_pct(config.TARGET_FOODCOST)})")
    if t["coverage"] < 0.97:
        lines.append(
            "_Остальное — товары на полке, добавки отдельной строкой и "
            "позиции без закупочной цены. По ним маржа не считается._"
        )
    if t["estimated"]:
        lines.append("_Часть цен типовые, а не ваши. Уточните: «цены»._")

    bad = costing.high_foodcost(conn, limit=4)
    if bad:
        lines += ["", "*Сырьё съедает больше ориентира:*"]
        for i in bad:
            lines.append(
                f"• {_p(i['name'])} — фудкост {_pct(i['foodcost'])}, "
                f"маржа {fmt(i['margin'])} ₽ с порции"
            )

    m = costing.menu_matrix(conn)
    if m["items"]:
        groups = {}
        for i in m["items"]:
            groups.setdefault(i["group"], []).append(i)
        titles = {
            costing.STAR: "⭐ Держат заведение",
            costing.WORKHORSE: "🐴 Берут охотно, зарабатываете мало",
            costing.PUZZLE: "❓ Зарабатывают, но их не замечают",
            costing.BALLAST: "🪨 Кандидаты на вывод из меню",
        }
        lines.append("")
        lines.append("*Разбор меню:*")
        for g in (costing.STAR, costing.WORKHORSE, costing.PUZZLE, costing.BALLAST):
            items = groups.get(g)
            if items:
                lines.append(f"{titles[g]}: " + ", ".join(_p(i["name"]) for i in items[:4]))
    return "\n".join(lines)


def answer_milk(conn):
    """Отдельный разбор по молоку — там чаще всего и течёт маржа."""
    rows = costing.milk_economics(conn)
    if not rows:
        return (
            "🥛 В данных нет разбивки по молоку: касса не передаёт модификатор. "
            "Тогда себестоимость считается по обычному молоку, и маржа на "
            "растительном не видна."
        )
    lines = ["🥛 *Молоко: сколько зарабатываете*", ""]
    for r in rows:
        lines.append(
            f"• {r['milk']} — {_pct(r['share'])} чашек · цена {fmt(r['price'])} ₽ · "
            f"сырьё {r['cost']:.0f} ₽ · маржа *{r['margin']:.0f} ₽* "
            f"(фудкост {_pct(r['foodcost'])})"
        )
    reg = next((r for r in rows if r["milk"] == menu.MILK_REGULAR), None)
    alts = [r for r in rows if r["milk"] != menu.MILK_REGULAR]
    if reg and alts:
        worst = min(alts, key=lambda x: x["margin"])
        diff = worst["margin"] - reg["margin"]
        lines.append("")
        if diff < 0:
            lines.append(
                f"↳ На {worst['milk']} молоке вы зарабатываете на "
                f"*{fmt(-diff)} ₽ меньше* с чашки, чем на обычном. "
                f"Наценку стоит пересмотреть."
            )
        else:
            lines.append(
                f"↳ Наценка за альтернативное молоко покрывает разницу: "
                f"на {worst['milk']} маржа даже выше на {fmt(diff)} ₽."
            )
    return "\n".join(lines)


def answer_supply(conn):
    """Что заказать поставщику и когда кончится."""
    if not analytics.has_sales(conn):
        return NO_DATA
    rep = supply.reorder(conn)
    if not rep["items"]:
        return "📦 Расход сырья пока не посчитать: нет рецептов или продаж напитков."
    lines = [f"📦 *Заявка на {rep['horizon_days']} дней*", ""]
    urgent = [i for i in rep["items"] if i["urgency"] in ("critical", "soon")]
    if urgent:
        lines.append("*Горит:*")
        for i in urgent[:5]:
            lines.append(
                f"🔴 {_p(i['name'])} — хватит на {i['days_left']} дн., "
                f"а поставка идёт {i['lead_days']:.0f} дн. Взять "
                f"{i['packs']} {i['pack_name']}"
            )
        lines.append("")
    lines.append("*Заказ:*")
    short_cycle = []
    for i in rep["items"][:16]:
        if i["packs"] <= 0:
            continue
        left = f" · осталось {i['left']} {i['unit']}" if i["left"] is not None else ""
        lines.append(
            f"• {_p(i['name'])} — *{i['packs']}* {i['pack_name']} "
            f"(уходит {i['per_day']} {i['unit']}/день{left})"
        )
        if i["review_days"] < rep["horizon_days"]:
            short_cycle.append(i)
    if short_cycle:
        lines.append("")
        lines.append("⏱ *Заказывать чаще, чем раз в неделю:*")
        for i in short_cycle:
            lines.append(
                f"• {_p(i['name'])} — каждые {i['review_days']:.0f} дн. "
                f"(дольше не хранится; взято ровно на этот срок)"
            )
    if rep["total_price"]:
        lines.append("")
        lines.append(f"Ориентировочно *{fmt(rep['total_price'])} ₽*.")
    if rep["note"]:
        lines.append("")
        lines.append("_" + rep["note"] + "_")
    return "\n".join(lines)


def answer_attach(conn):
    """Еда к кофе — главный рычаг среднего чека."""
    if not analytics.has_sales(conn):
        return NO_DATA
    a = analytics.attach_rate(conn)
    if a.get("rate") is None:
        return "🥐 " + a["note"]
    lines = ["🥐 *Еда к кофе (attach-rate)*", ""]
    lines.append(
        f"В *{_pct(a['rate'], 1)}* кофейных чеков есть еда "
        f"({a['with_food']} из {a['drink_checks']})."
    )
    s = a.get("morning_vs_day")
    if s:
        lines.append(f"Утром до 11:00 — {_pct(s['morning'], 1)}, после — {_pct(s['day'], 1)}.")
        if s["gap"] > 0.05:
            lines.append(
                "↳ После полудня витрина почти не работает: чаще всего это "
                "значит, что к обеду в ней уже пусто или нечего предложить."
            )
    if a.get("scenario"):
        sc = a["scenario"]
        hrs = ", ".join(f"{h}:00" for h in sc["hours"][:6])
        lines += [
            "",
            f"Если подтянуть отстающие часы ({hrs}) до вашей же медианы "
            f"{_pct(sc['target_rate'], 1)} — это примерно "
            f"+{sc['extra_items_per_day']} позиции в день, "
            f"около *{fmt(sc['money_per_month'])} ₽ в месяц*.",
            "_" + sc["assumption"] + "_",
        ]
    b = analytics.basket(conn)
    if b:
        lines += ["", "*Что реально берут вместе:*"]
        for x in b[:4]:
            lines.append(
                f"• {_p(x['drink'])} + {_p(x['food'])} — в {x['checks']} чеках "
                f"(в {x['lift']}× чаще обычного)"
            )
    return "\n".join(lines)


def answer_drinks(conn):
    """Что пьют: напитки, размеры, молоко, холодное/горячее."""
    if not analytics.has_sales(conn):
        return NO_DATA
    mix = analytics.drink_mix(conn, n=6)
    lines = ["☕ *Что у вас пьют*", ""]
    for d in mix:
        lines.append(f"• {_p(d['name'])} — {_pct(d['share'])} чашек ({d['qty']} шт)")
    sizes = analytics.size_mix(conn)
    if sizes:
        lines += ["", "Размеры: " + ", ".join(f"{s['key']} {_pct(s['share'])}" for s in sizes)]
    milks = analytics.milk_mix(conn)
    if milks:
        alt = sum(m["share"] for m in milks if m["key"] != menu.MILK_REGULAR)
        lines.append(f"Молоко: растительное и безлактозное — {_pct(alt)} чашек")
    ic = analytics.iced_share(conn)
    if ic["total"]:
        lines.append(f"Холодные напитки — {_pct(ic['share'])} за период")
        if len(ic["by_month"]) >= 2:
            first, last = ic["by_month"][0], ic["by_month"][-1]
            lines.append(
                f"  ({first['month']}: {_pct(first['share'])} → "
                f"{last['month']}: {_pct(last['share'])})"
            )
    return "\n".join(lines)


def answer_shift(conn):
    """Когда на смене нужен второй человек."""
    if not analytics.has_sales(conn):
        return NO_DATA
    f = demand.forecast_checks(conn)
    sp = staffing.shift_plan(conn)
    lines = [f"👥 *Смена на {f['target']}*", "", f"Ожидается *{f['low']}–{f['high']} чеков*."]
    if sp.get("peaks"):
        lines.append(
            "🕗 Пик: "
            + " и ".join(f"{p['hour']:02d}:00" for p in sp["peaks"])
            + f" · тише всего в {sp['quietest']['hour']:02d}:00"
        )
    cap = sp.get("capacity") or {}
    if cap.get("known"):
        lines.append(
            f"Ваш потолок — около *{cap['cups_per_hour']:.0f} чашек в час* "
            f"(это ваш собственный повторяемый максимум)."
        )
    if sp["windows"]:
        txt = " и ".join(f"{a:02d}:00–{b:02d}:00" for a, b in sp["windows"])
        lines.append(f"👤 На пределе: {txt} — {_pct(sp['tight_share'])} всех чеков дня.")
    if sp.get("scenario"):
        sc = sp["scenario"]
        lines += [
            "",
            f"В эти часы вы упираетесь в потолок в {_pct(sc['clipped_share'])} "
            f"дней. Второй бариста дал бы примерно "
            f"+{sc['extra_checks_per_day']} чеков в день, около "
            f"*{fmt(sc['money_per_month'])} ₽ в месяц*.",
            "_" + sc["assumption"] + "_",
        ]
    elif sp.get("note"):
        lines += ["", "_" + sp["note"] + "_"]
    return "\n".join(lines)


def answer_lost(conn):
    """Сколько недополучено из-за пустой витрины."""
    if not analytics.has_sales(conn):
        return NO_DATA
    q = analytics.data_quality(conn)
    if not q["has_time"]:
        return (
            "💸 *Упущенная выручка*\n\n"
            + (_quality_note(conn) or "")
            + "\n\nПоказать нечего честно: без времени продаж распроданность не видна."
        )
    last = last_day_with_data(conn)
    day = demand.sellouts_for_day(conn, last)
    rep = demand.lost_sales_report(conn)
    lines = ["💸 *Упущенная выручка витрины*", ""]
    if day["items"]:
        lines.append(f"*Вчера ({last.isoformat()}) кончилось:*")
        for i in day["items"][:6]:
            h = f"после {i['hour']:02d}:00" if i["hour"] is not None else ""
            lines.append(
                f"• {_p(i['name'])} {h} — не продали ~{i['lost_units']} шт "
                f"на {fmt(i['lost_money'])} ₽"
            )
        lines.append(f"Итого за вчера: *{fmt(day['total'])} ₽*")
        lines.append("")
    if not rep["items"]:
        lines.append("За период витрина до закрытия не кончалась — весь спрос закрываете.")
        return "\n".join(lines)
    lines.append(
        f"*В среднем: {fmt(rep['total_per_day'])} ₽ в день "
        f"≈ {fmt(rep['total_per_month'])} ₽ в месяц.*"
    )
    lines.append("")
    for i in rep["items"][:6]:
        lines.append(
            f"• {_p(i['name'])} — кончался {i['sellout_days']} дн., "
            f"~{fmt(i['lost_money_per_month'])} ₽/мес"
        )
    lines.append("")
    lines.append(
        "Этих денег нет в кассе: гость не нашёл завтрак, взял только кофе — "
        "и назавтра пошёл завтракать туда, где он есть."
    )
    return "\n".join(lines)


def answer_waste(conn):
    last = last_day_with_data(conn)
    w = catalog.waste_report(conn, last)
    mw = catalog.milk_waste(conn)
    lines = [f"🗑 *Списания за {last.isoformat()}*", ""]
    if w["case"]:
        lines.append(
            f"*Витрина* — {fmt(w['case_total'])} ₽ по ценникам ({w['case_pct']:.1f}% выручки):"
        )
        for i in w["case"][:8]:
            lines.append(f"• {_p(i['name'])} — {i['qty']:.0f} шт · {fmt(i['amount'])} ₽")
    if w["raw"]:
        lines.append("")
        lines.append(f"*Сырьё* — {fmt(w['raw_total'])} ₽ по себестоимости:")
        for i in w["raw"][:8]:
            lines.append(f"• {_p(i['name'])} — {i['qty']:g} {i['unit']} · {fmt(i['amount'])} ₽")
    if not w["items"]:
        lines.append("Списаний нет.")
        lines.append("")
        lines.append(
            "Это не всегда хорошая новость: если витрина кончается до закрытия, "
            "вы теряете больше, чем на списаниях. Спросите «сколько недопродали»."
        )
    if mw.get("tracked"):
        lines += [
            "",
            f"🥛 Вылитого молока в среднем {mw['litres_per_day']} л в день — "
            f"около *{fmt(mw['money_per_month'])} ₽ в месяц*.",
        ]
    else:
        lines += ["", "_" + mw["note"] + "_"]
    lines += [
        "",
        "Отметить: «списание круассаны 4» или «вылил молоко 1,5». "
        "Не обязательно — заказ витрины считается и без этого.",
    ]
    return "\n".join(lines)


def answer_audit(conn):
    if not analytics.has_sales(conn):
        return NO_DATA
    last = last_day_with_data(conn)
    s = analytics.sales_summary(conn, last)
    w = catalog.waste_report(conn, last)
    lost = demand.sellouts_for_day(conn, last)
    t = costing.totals(conn)
    lines = [f"🔍 *Экспресс-аудит за {last.isoformat()}*", ""]
    lines.append(
        f"💵 Выручка *{fmt(s['revenue'])} ₽* · {s['checks']} чеков · "
        f"чек {fmt(s['avg'])} ₽ · {s['cups']:.0f} чашек"
    )
    if t["revenue_costed"]:
        lines.append(
            f"💰 Фудкост за период *{_pct(t['foodcost'], 1)}* · маржа {fmt(t['margin'])} ₽"
        )
    lines.append("")
    lines.append("*Где ваши деньги:*")
    if lost["items"]:
        top = lost["items"][0]
        lines.append(
            f"💸 Недопродали на *{fmt(lost['total'])} ₽* — кончилось "
            + ", ".join(_p(i["name"]) for i in lost["items"][:3])
        )
        if top["hour"] is not None:
            lines.append(f"   ({_p(top['name'])} закончился после {top['hour']:02d}:00)")
    else:
        lines.append("💸 Витрина вчера не кончалась — весь спрос закрыли.")
    if w["total"]:
        lines.append(
            f"🗑 Списано на *{fmt(w['total'])} ₽* "
            f"(витрина {fmt(w['case_total'])} ₽, сырьё {fmt(w['raw_total'])} ₽)"
        )
    else:
        lines.append("🗑 Списаний нет.")
    a = analytics.attach_rate(conn)
    if a.get("rate") is not None:
        lines.append(f"🥐 Еда есть в {_pct(a['rate'], 1)} кофейных чеков")
    sp = staffing.shift_plan(conn)
    if sp.get("scenario"):
        lines.append(
            f"👥 В {', '.join(f'{h}:00' for h in sp['scenario']['confirmed_hours'])} "
            f"вы упираетесь в потолок скорости"
        )
    return "\n".join(lines)


def answer_revenue(conn):
    last = last_day_with_data(conn)
    cats = analytics.revenue_by_category(conn, last)
    lines = [f"📈 *Выручка по категориям за {last.isoformat()}*", ""]
    for c in cats:
        em = CATEGORY_EMOJI.get(c["cat"], "•")
        lines.append(f"{em} {c['cat']} — *{c['share'] * 100:.0f}%* · {fmt(c['rev'])} ₽")
    return "\n".join(lines)


def answer_cash(conn):
    last = last_day_with_data(conn)
    s = analytics.sales_summary(conn, last)
    return (
        f"🧾 *Сверка кассы за {last.isoformat()}*\n\n"
        f"💳 Безнал: {fmt(s['card'])} ₽\n"
        f"💵 Наличные: {fmt(s['cash'])} ₽\n"
        f"*Итого: {fmt(s['revenue'])} ₽* по {s['checks']} чекам."
    )


HANDLERS = {
    "case": answer_case,
    "margin": answer_margin,
    "supply": answer_supply,
    "attach": answer_attach,
    "drinks": answer_drinks,
    "shift": answer_shift,
    "lost": answer_lost,
    "waste": answer_waste,
    "audit": answer_audit,
    "revenue": answer_revenue,
    "cash": answer_cash,
}

# точные подписи кнопок и коротких команд -> мгновенный расчёт (без ИИ)
BUTTONS = {
    "🥐 витрина на завтра": "case",
    "витрина": "case",
    "витрина на завтра": "case",
    "💰 маржа и меню": "margin",
    "маржа": "margin",
    "маржа и меню": "margin",
    "📦 заявка поставщику": "supply",
    "заявка поставщику": "supply",
    "закупки": "supply",
    "🥐 еда к кофе": "attach",
    "еда к кофе": "attach",
    "attach": "attach",
    "☕ что пьют": "drinks",
    "что пьют": "drinks",
    "💸 упущенная выручка": "lost",
    "упущенная выручка": "lost",
    "сколько недопродали": "lost",
    "недопродажи": "lost",
    "👥 смена": "shift",
    "смена": "shift",
    "смена завтра": "shift",
    "🗑 списания": "waste",
    "списания": "waste",
    "📊 экспресс-аудит": "audit",
    "экспресс-аудит": "audit",
    "аудит": "audit",
    "📈 выручка": "revenue",
    "выручка": "revenue",
    "🧾 сверка кассы": "cash",
    "сверка кассы": "cash",
    "🥛 молоко": "milk",
    "молоко": "milk",
}
HANDLERS["milk"] = answer_milk


# ---------- ввод от персонала ----------
_WASTE_RE = re.compile(
    r"^\s*(?:списани[еяй]|списал[аи]?|выброс(?:ил[аи]?)?|вылил[аи]?)\s+"
    r"([^\d?]+?)\s+(\d+(?:[.,]\d+)?)\s*"
    r"(?:шт\.?|штук[иа]?|л\.?|литр[аов]*|кг|килограмм[аов]*)?\s*$"
)
_STOCK_RE = re.compile(
    r"^\s*(?:остаток|остатки|инвентаризац[ияи]+)\s+([^\d?]+?)\s+"
    r"(\d+(?:[.,]\d+)?)\s*[а-яё.]*\s*$"
)
# Минус в цене ловим намеренно, чтобы ответить «цена не может быть
# отрицательной», а не молча уронить фразу в раздел заявки — владелец был
# уверен, что цену задал.
_PRICE_RE = re.compile(r"^\s*цена\s+([^\d?]+?)\s+(-?\d+(?:[.,]\d+)?)\s*(?:₽|руб\.?|р\.?)?\s*$")
_DONE_RE = re.compile(r"^\s*(?:сделал[аи]?|готово|выполнил[аи]?)\s+(.{3,})$")


def _num(s):
    return float(str(s).replace(",", "."))


def answer(conn, text, allow_write=True):
    """allow_write=False — только чтение (для веб-эндпоинта: GET не должен писать)."""
    low = text.lower().strip()

    if low in ("/help", "help", "помощь", "что ты умеешь", "команды"):
        return help_text()
    if conn.execute("SELECT COUNT(*) c FROM receipts").fetchone()["c"] == 0:
        return "Пока нет данных о продажах. Загрузите выгрузку чеков — и я всё посчитаю."

    written = _handle_writes(conn, low, allow_write)
    if written is not None:
        return written

    if low in ("каталог", "меню", "позиции"):
        return _catalog_text(conn)
    if low in ("цены", "себестоимость сырья", "цены сырья"):
        return _prices_text(conn)
    if low in ("обслуживание", "регламент", "кофемашина"):
        return _maintenance_text(conn)

    if low in BUTTONS:
        return HANDLERS[BUTTONS[low]](conn)

    loc = local_analytics(conn, text)
    if loc:
        return loc

    intent = route(text)
    if intent and _looks_like_command(low):
        return HANDLERS[intent](conn)

    try:
        smart = smart_answer(conn, text)
    except llm.LLMUnavailable as e:
        # ИИ настроен, но не ответил. Показываем понятную причину, а если по
        # вопросу есть точный офлайн-ответ — добавляем и его.
        if intent:
            return HANDLERS[intent](conn) + "\n\n" + e.user_message
        return e.user_message
    if smart:
        return smart
    if intent:
        return HANDLERS[intent](conn)
    return (
        "Могу ответить по вашей кофейне: продажи и средний чек, маржа и фудкост, "
        "витрина на завтра, упущенная выручка, заявка поставщику, еда к кофе, "
        "загрузка смены, что пьют, списания, касса.\n\n"
        "Например: «какая маржа у рафа?», «на сколько хватит зерна?», "
        "«во сколько пик?», «сколько недопродали вчера?».\n\n"
        "Чтобы я отвечал и на любые свободные вопросы — включите бесплатный ИИ "
        "Ollama (см. README)."
    )


def _handle_writes(conn, low, allow_write):
    """Ввод, который меняет данные: списание, остаток, цена, каталог, регламент."""
    m = _WASTE_RE.match(low)
    if m:
        if not allow_write:
            return "Записать списание можно только в Telegram-боте."
        res = catalog.add_waste(conn, m.group(1).strip(), _num(m.group(2)))
        if res and res.get("ambiguous"):
            return _ask_which(
                m.group(1).strip(), res["ambiguous"], f"списание <нужное> {m.group(2)}"
            )
        if res is None:
            return _waste_input_help(conn, m.group(1).strip(), m.group(2))
        qty = f"{res['qty']:g}"
        note = ""
        if res.get("estimated"):
            note = "\n_Сумма по типовой цене — уточните свою: «цена молоко обычное 95»._"
        return (
            f"✅ Записал: {_p(res['name'])} — {qty} {res['unit']} ({fmt(res['amount'])} ₽).{note}"
        )
    # Похоже на списание, но формат не распознан. Молчать нельзя: иначе в ответ
    # приходил отчёт за вчера, и бариста был уверен, что списание записано.
    if (
        re.match(r"^\s*(?:списани[ея]|вылил)\s+\S", low)
        and re.search(r"\d", low)
        and not re.search(r"\bза\b|\bпо\b|покажи|какие|сколько|почему|отч[её]т", low)
    ):
        return (
            "Не понял формат. Напишите: «списание круассаны 4» или "
            "«вылил молоко 1,5» — сначала что, потом сколько."
        )

    m = _STOCK_RE.match(low)
    if m:
        if not allow_write:
            return "Записать остаток можно только в Telegram-боте."
        what = m.group(1).strip()
        res = supply.record_stock(conn, what, _num(m.group(2)))
        if res and res.get("ambiguous"):
            return _ask_which(what, res["ambiguous"], f"остаток <нужное> {m.group(2)}")
        if res and res.get("error") == "implausible":
            return (
                f"Столько под стойкой не помещается. Проверьте число: "
                f"остаток должен быть от 0 до {res['max']}."
            )
        if res is None:
            return f"Не нашёл «{what}» среди сырья. Список — напишите «цены»."
        rep = supply.reorder(conn)
        row = next((i for i in rep["items"] if i["name"] == res["name"]), None)
        tail = ""
        if row and row["days_left"] is not None:
            tail = (
                f"\nХватит примерно на *{row['days_left']} дн.* "
                f"(уходит {row['per_day']} {row['unit']}/день)."
            )
        return f"✅ Остаток {_p(res['name'])}: {res['qty']:g} {res['unit']}.{tail}"

    m = _PRICE_RE.match(low)
    if m:
        if not allow_write:
            return "Менять цены можно только в Telegram-боте."
        what, price = m.group(1).strip(), _num(m.group(2))
        if price <= 0:
            return (
                "Цена должна быть больше нуля: «цена зерно 1800» — "
                "это цена за упаковку, как она указана в разделе «цены»."
            )
        mi = catalog.match_ingredient(conn, what)
        if not mi["name"] and len(mi["options"]) > 1:
            return _ask_which(what, mi["options"], f"цена <нужное> {price:g}")
        if mi["name"] is None:
            return f"Не нашёл «{what}» среди сырья. Список — «цены»."
        name = mi["name"]
        row = conn.execute("SELECT pack_name FROM ingredients WHERE name=?", (name,)).fetchone()
        costing.set_price(conn, name, price)
        return f"✅ {_p(name)} — {price:g} ₽ за {row['pack_name']}. Маржа пересчитана."

    c = re.match(r"^\s*(не\s+витрин\w*|витрин\w*)\s+(.+?)\s*$", low)
    if c and not c.group(2).startswith("на завтра"):
        if not allow_write:
            return "Менять каталог можно только в Telegram-боте."
        want = not c.group(1).startswith("не")
        res = catalog.set_stocked(conn, c.group(2), want)
        if res and res.get("ambiguous"):
            return _ask_which(
                c.group(2),
                res["ambiguous"],
                ("витрина <нужное>" if want else "не витрина <нужное>"),
            )
        if res is None:
            return f"Не нашёл «{c.group(2)}» в каталоге. Посмотрите список: «каталог»."
        if res.get("error") == "not_food":
            return (
                f"{_p(res['name'])} — это не витрина, а «{res['kind']}». "
                f"Напиток кончиться не может, его в заказ витрины ставить нельзя."
            )
        return (
            f"✅ {_p(res['name'])} — {'в витрине' if want else 'НЕ в витрине'}.\n"
            f"{'Позиция появится в заказе на завтра.' if want else 'Убрана из заказа.'}"
        )

    m = _DONE_RE.match(low)
    if m:
        if not allow_write:
            return "Отметить обслуживание можно только в Telegram-боте."
        task = supply.mark_done(conn, m.group(1).strip())
        if task:
            return f"✅ Отметил: {_p(task)}."
    return None


def approve_case_order(conn):
    """Утверждённый заказ витрины + отметка об утверждении."""
    p = demand.case_order(conn)
    lines = [f"🥐 *Витрина на {p['target']}* — утверждено", ""]
    for it in p["items"]:
        mark = " (поправлено вручную)" if it.get("adjusted") else ""
        lines.append(f"• {_p(it['name'])}: *{it['recommended']}* шт{mark}")
    if not p["items"]:
        lines.append("_Пока нечего заказывать: мало данных о продажах._")
    from . import db as _db

    _db.kv_set(conn, "case_approved", f"{p['target']}|{config.now().strftime('%H:%M')}")
    return {"target": p["target"], "items": p["items"], "text": "\n".join(lines)}


def _catalog_text(conn):
    rows = catalog.catalog(conn)
    if not rows:
        return "Каталог пуст — загрузите выгрузку чеков, он соберётся сам."
    titles = {
        menu.KIND_DRINK: "☕ Напитки",
        menu.KIND_FOOD: "🥐 Витрина",
        menu.KIND_GOODS: "🛍 Товары",
        menu.KIND_ADDON: "➕ Добавки",
        menu.KIND_SERVICE: "⚙️ Служебные",
    }
    lines = ["📋 *Каталог*"]
    for kind in (
        menu.KIND_DRINK,
        menu.KIND_FOOD,
        menu.KIND_GOODS,
        menu.KIND_ADDON,
        menu.KIND_SERVICE,
    ):
        group = [r for r in rows if r["kind"] == kind]
        if not group:
            continue
        lines += ["", f"*{titles[kind]} ({len(group)}):*"]
        lines += [f"• {_p(r['name'])}" for r in group[:20]]
        if len(group) > 20:
            lines.append(f"…и ещё {len(group) - 20}")
    lines += ["", "Поправить: «витрина сырники» или «не витрина вода»."]
    return "\n".join(lines)


def _prices_text(conn):
    rows = list(
        conn.execute(
            "SELECT name, unit, pack_qty, pack_price, pack_name, price_src, category "
            "FROM ingredients ORDER BY price_src, category, name"
        )
    )
    if not rows:
        return "Справочник сырья пуст."
    unknown = [r for r in rows if r["price_src"] == costing.PRICE_UNKNOWN]
    default = [r for r in rows if r["price_src"] == costing.PRICE_DEFAULT]
    owner = [r for r in rows if r["price_src"] == costing.PRICE_OWNER]
    lines = ["🏷 *Цены сырья*", ""]
    if unknown:
        lines.append("*Цена неизвестна — маржа по этим позициям не считается:*")
        lines += [f"• {_p(r['name'])}" for r in unknown[:15]]
        lines.append("")
    if default:
        lines.append("*Типовые (не ваши) — стоит уточнить:*")
        lines += [
            f"• {_p(r['name'])} — {r['pack_price']:g} ₽ за {r['pack_name']}" for r in default[:15]
        ]
        lines.append("")
    if owner:
        lines.append("*Ваши:*")
        lines += [
            f"• {_p(r['name'])} — {r['pack_price']:g} ₽ за {r['pack_name']}" for r in owner[:15]
        ]
    lines += ["", "Поправить: «цена зерно 1800» (за упаковку, как указано выше)."]
    return "\n".join(lines)


def _maintenance_text(conn):
    due = supply.maintenance_due(conn)
    if not due:
        return "🔧 По регламенту всё выполнено."
    lines = ["🔧 *Обслуживание: пора сделать*", ""]
    for d in due[:10]:
        when = (
            "ни разу не отмечали"
            if d["days_since"] is None
            else f"прошло {d['days_since']} дн. при норме {d['period_days']:.0f}"
        )
        lines.append(f"• {_p(d['task'])} — {when}")
        if d["note"]:
            lines.append(f"  _{d['note']}_")
    lines += ["", "Отметить: «сделал калибровку»."]
    return "\n".join(lines)


def _ask_which(what, options, example):
    """Переспросить, а не выбрать за владельца.

    Молча выбранная позиция — это неверная запись в базе, которую никто уже не
    заметит: списание гасит признак распроданности, и упущенная выручка по
    настоящей позиции перестаёт считаться.
    """
    lines = [f"«{what}» — их несколько, уточните какая:", ""]
    lines += [f"• {_p(o)}" for o in options[:8]]
    lines += ["", f"Напишите полностью: «{example}»."]
    return "\n".join(lines)


def _waste_input_help(conn, what, qty_text):
    try:
        qty = _num(qty_text)
    except ValueError:
        qty = None
    if qty is not None and qty <= 0:
        return "Количество должно быть больше нуля: «списание круассаны 4»."
    if qty is not None and qty > catalog.MAX_WASTE_QTY:
        return (
            f"Многовато для одного дня — {qty:g}. Если это не опечатка, "
            f"внесите несколькими сообщениями."
        )
    if len(what) < catalog.MIN_STEM:
        return (
            "Слишком короткое название — по одной-двум буквам не опознать. "
            "Напишите как в чеке: «списание круассан 4»."
        )
    return (
        f"Не нашёл «{what}» ни в витрине, ни в сырье. "
        f"Список позиций — «каталог», список сырья — «цены»."
    )


def help_text():
    return (
        "Я — КофейняОС. Вот что я умею:\n\n"
        "💰 *Маржа и меню* — сколько зарабатывает каждая позиция и где течёт фудкост\n"
        "🥐 *Витрина на завтра* — сколько чего ставить, чтобы не остаться пустыми\n"
        "📦 *Заявка поставщику* — расход зерна и молока по чекам, готовый заказ\n"
        "🥐 *Еда к кофе* — attach-rate и где вы теряете средний чек\n"
        "💸 *Упущенная выручка* — сколько недополучаете на пустой витрине\n"
        "👥 *Смена* — где упираетесь в потолок и нужен второй бариста\n"
        "☕ *Что пьют* — напитки, размеры, молоко, айс\n"
        "🗑 *Списания* · 📈 *Выручка* · 🧾 *Сверка кассы*\n\n"
        "Ввод одной строкой:\n"
        "«списание круассаны 4» · «вылил молоко 1,5»\n"
        "«остаток зерно 3» · «цена зерно 1800» · «сделал калибровку»\n\n"
        "Команды: /svodka /vitrina /zakupki /marzha /upusheno /nedelya /status"
    )


# ---------- сводки ----------
def morning_brief(conn):
    if not analytics.has_sales(conn):
        return NO_DATA
    last = last_day_with_data(conn)
    s = analytics.sales_summary(conn, last)
    prev = analytics.sales_summary(conn, last - timedelta(days=7))
    delta = ((s["revenue"] - prev["revenue"]) / prev["revenue"] * 100) if prev["revenue"] else 0
    lost = demand.sellouts_for_day(conn, last)
    w = catalog.waste_report(conn, last)
    lines = [f"☀️ *Доброе утро, {config.OWNER_NAME}!*", f"Сводка за {last.isoformat()}:", ""]
    lines.append(
        f"💵 Выручка *{fmt(s['revenue'])} ₽* "
        f"({'+' if delta >= 0 else ''}{delta:.0f}% к прошлой неделе)"
    )
    lines.append(f"🧾 {s['checks']} чеков · чек {fmt(s['avg'])} ₽ · {s['cups']:.0f} чашек")
    if lost["items"]:
        top = lost["items"][0]
        when = f" (после {top['hour']:02d}:00)" if top["hour"] is not None else ""
        lines.append(f"💸 Недопродали ~*{fmt(lost['total'])} ₽*: кончился {_p(top['name'])}{when}")
    if w["total"]:
        lines.append(f"🗑 Списано на {fmt(w['total'])} ₽")

    order = demand.case_order(conn)
    ups = [i for i in order["items"] if i["tag"] == "up"][:3]
    if ups:
        lines.append(
            "🥐 Сегодня в витрину больше: "
            + ", ".join(f"{_p(i['name'])} — {i['recommended']} шт" for i in ups)
        )
    urgent = [i for i in supply.reorder(conn)["items"] if i["urgency"] in ("critical", "soon")][:3]
    if urgent:
        lines.append(
            "📦 Заканчивается: "
            + ", ".join(f"{_p(i['name'])} ({i['days_left']} дн.)" for i in urgent)
        )
    due = supply.maintenance_due(conn)
    weekly = [d for d in due if d["period_days"] >= 7][:2]
    if weekly:
        lines.append("🔧 По регламенту: " + ", ".join(_p(d["task"]) for d in weekly))
    lines.append("")
    lines.append("«🥐 Витрина на завтра» · «💰 Маржа и меню» · «📦 Заявка поставщику» 👇")
    return "\n".join(lines)


def weekly_brief(conn):
    if not analytics.has_sales(conn):
        return NO_DATA
    last = last_day_with_data(conn)
    week = analytics.week_trend(conn, last)
    total = sum(d["revenue"] for d in week)
    prev_week = sum(
        analytics.sales_summary(conn, last - timedelta(days=7 + i))["revenue"] for i in range(7)
    )
    delta = ((total - prev_week) / prev_week * 100) if prev_week else 0
    lines = [f"📅 *Итоги недели* (по {last.isoformat()})", ""]
    lines.append(
        f"💵 Выручка за 7 дней: *{fmt(total)} ₽* "
        f"({'+' if delta >= 0 else ''}{delta:.0f}% к пред. неделе)"
    )
    best = max(week, key=lambda d: d["revenue"])
    lines.append(
        f"🔝 Лучший день: {analytics.WEEKDAY_SHORT[best['dow']]} — {fmt(best['revenue'])} ₽"
    )
    t = costing.totals(conn, days=7, upto=last)
    if t["revenue_costed"]:
        lines.append(f"💰 Фудкост недели {_pct(t['foodcost'], 1)} · маржа {fmt(t['margin'])} ₽")
    a = analytics.attach_rate(conn, days=7, upto=last)
    if a.get("rate") is not None:
        lines.append(f"🥐 Еда к кофе: {_pct(a['rate'], 1)}")
    mix = analytics.drink_mix(conn, days=7, upto=last, n=3)
    if mix:
        lines.append("☕ Пьют чаще всего: " + ", ".join(_p(m["name"]) for m in mix))
    return "\n".join(lines)


# ---------- быстрые ответы без ИИ ----------
_QUESTION_WORDS = (
    "почему",
    "зачем",
    "стоит ли",
    "посовет",
    "как лучше",
    "что делать",
    "можно ли",
    "а если",
    "сравни",
    "объясни",
    "насколько",
    "прогноз на",
    "что думаешь",
    "как думаешь",
    "лучше ли",
    "выгодн",
)


def _looks_like_command(low):
    """Короткая фраза-команда, а не рассуждение."""
    if any(w in low for w in _QUESTION_WORDS):
        return False
    return len(low.split()) <= 5


def local_analytics(conn, text):
    """Мгновенные точные ответы на самые частые вопросы — без ИИ.
    Возвращает строку либо None (тогда вопрос уйдёт в ИИ)."""
    low = text.lower()

    def has(*ws):
        return any(w in low for w in ws)

    # Быстрый отсев: если вопрос точно не про эти темы — не считаем срез данных.
    # Список обязан покрывать все ветки ниже, иначе часть из них недостижима.
    if not any(
        w in low
        for w in (
            "чек",
            "день",
            "дням",
            "недел",
            "час",
            "пик",
            "во сколько",
            "прода",
            "берут",
            "покупа",
            "популярн",
            "ходов",
            "топ",
            "хит",
            "бестселлер",
            "дела",
            "как бизнес",
            "как идут",
            "как обстоят",
            "картина",
            "чаще",
            "идёт",
            "идет",
            "хват",
            "зерн",
            "чашек",
            "чашк",
            "фудкост",
            "марж",
            "зарабат",
            "себестоим",
            "наценк",
            "выгодн",
            "стоит",
            "почём",
            "почем",
        )
    ):
        return None

    if has("средний чек", "средний чёк"):
        m = re.search(r"(\d+)\s*(недел|нед|дн|мес)", low)
        days = None
        if m:
            n = int(m.group(1))
            u = m.group(2)
            days = n * 7 if u.startswith("нед") else (n * 30 if u.startswith("мес") else n)
        elif has("две недел", "2 недел", "за неделю", "за недел"):
            days = 14 if has("две недел", "2 недел") else 7
        elif has("месяц"):
            days = 30
        a = analytics.avg_check_window(conn, days or 56)
        return f"Средний чек за последние {a['days']} дней — {a['avg']} ₽ (по {a['checks']} чекам)."

    # «какая маржа у рафа» — вопрос про КОНКРЕТНУЮ позицию.
    # Общий отчёт по меню здесь не годится: владелец спросил про одну чашку и
    # должен получить её цифры, а не искать строку глазами.
    if has(
        "марж",
        "фудкост",
        "себестоим",
        "наценк",
        "сколько зараба",
        "выгодн",
        "сколько стоит",
        "почём",
        "почем",
    ):
        item = _item_in_text(conn, low)
        if item:
            return _item_money_answer(conn, item)

    # «на сколько хватит зерна / молока» — прямой вопрос к заявке
    if has("хват") and has("зерн", "молок", "сироп", "стакан", "кофе"):
        rep = supply.reorder(conn)
        known = [i for i in rep["items"] if i["days_left"] is not None]
        if not known:
            return (
                "Остатки не пересчитывали, поэтому «на сколько хватит» я сказать не могу — "
                "могу только сколько уходит в день. Напишите, например, «остаток зерно 4»."
            )
        parts = [f"{i['name']} — на {i['days_left']} дн." for i in known[:5]]
        return "По последнему пересчёту: " + "; ".join(parts) + "."

    d = data_digest(conn)
    t = d["totals"]

    if has("день недели", "дни недели", "по дням недел", "дню недели", "какой день недел"):
        b, s = d["busiest_weekday"], d["slowest_weekday"]
        if not b:
            return None
        if has("меньше", "слаб", "хуже", "мало", "тих", "наимень"):
            return (
                f"Самый слабый день недели — {s['weekday']}: в среднем {s['avg_checks']} "
                f"чеков и {fmt(s['avg_revenue'])} ₽ в день."
            )
        return (
            f"Больше всего у вас {b['weekday']}: в среднем {b['avg_checks']} чеков и "
            f"{fmt(b['avg_revenue'])} ₽ в день. Самый слабый — {s['weekday']} "
            f"(~{s['avg_checks']} чеков)."
        )

    if (
        re.search(r"\bпик\b", low)
        or has("по часам", "в каком часу", "в котором часу", "час пик")
        or (
            "во сколько" in low
            and any(
                w in low
                for w in [
                    "прода",
                    "выручк",
                    "народ",
                    "поток",
                    "люд",
                    "покупа",
                    "больше всего",
                    "загруж",
                    "трафик",
                    "заказ",
                    "очеред",
                ]
            )
        )
    ):
        p, s = d["peak_hour"], d["slow_hour"]
        parts = []
        if p:
            parts.append(f"Пик — около {p['hour']}:00")
        if s:
            parts.append(f"самый тихий час — {s['hour']}:00")
        return (", ".join(parts) + ".") if parts else None

    if ("лучше всего" in low and has("прода", "берут", "идёт", "идет", "поку")) or has(
        "популярн",
        "ходов",
        "топ товар",
        "что берут",
        "хиты",
        "хит прода",
        "бестселлер",
        "больше всего прода",
        "что покупают",
        "что чаще всего",
    ):
        tp = d["top_products"][:3]
        return (
            "Лучше всего продаются: "
            + ", ".join(f"{_p(x['name'])} (~{x['qty']} шт за период)" for x in tp)
            + "."
        )

    if "недел" in low and (
        re.search(r"как\s+(?:\w+\s+){0,2}недел", low)
        or has(
            "итог",
            "обзор недел",
            "результат",
            "сводка за недел",
            "прошлую недел",
            "прошлой недел",
            "недельн",
        )
    ):
        return weekly_brief(conn)

    if has(
        "как дела",
        "как бизнес",
        "как идут",
        "как обстоят",
        "общая картина",
        "как у нас дела",
        "как продажи",
    ):
        base = (
            f"За {d['period']['days']} дней: выручка {fmt(t['revenue'])} ₽, "
            f"{t['checks']} чеков, средний чек {t['avg_check']} ₽."
        )
        if d["foodcost"] is not None:
            base += f" Фудкост {_pct(d['foodcost'], 1)}."
        if d["busiest_weekday"]:
            base += f" Лучший день — {d['busiest_weekday']['weekday']}."
        if d["peak_hour"]:
            base += f" Пик в {d['peak_hour']['hour']}:00."
        if d["top_products"]:
            base += f" Хит — {_p(d['top_products'][0]['name'])}."
        return base
    return None


def _item_in_text(conn, low):
    """Найти позицию меню, упомянутую в вопросе («у рафа» -> «Раф»).

    Ищем по основе первого слова названия и только с ГРАНИЦЫ слова: без этого
    «раф» находился внутри «график», и вопрос про смену отвечал экономикой рафа.
    Окончание при этом свободно — «рафа», «латте», «круассаны».
    """
    # Служебные слова из вопроса не должны считаться названиями. «Какая» и
    # «Какао» отличаются одной буквой, и без этого списка вопрос «какая маржа?»
    # отвечал экономикой какао.
    tokens = [w for w in re.findall(r"[а-яёa-z]+", low) if len(w) >= 3 and w not in _STOPWORDS]
    if not tokens:
        return None
    names = [
        r["name"]
        for r in conn.execute("SELECT name FROM menu_items WHERE kind IN ('drink','food')")
    ]

    def matches(word, token):
        """Слово названия и слово вопроса — одно и то же с точностью до окончания.

        Сравниваем ОСНОВЫ, а не ищем подстроку: «раф» не должен находиться
        внутри «график». Основа получается отсечением падежного окончания —
        иначе «чая» не опознаётся как «Чай», а «рафа» как «Раф».
        """
        a, b = menu.stem(word), menu.stem(token)
        if len(a) < 2 or len(b) < 2:
            return False
        return a == b and abs(len(word) - len(token)) <= 3

    def score(n):
        return sum(1 for w in n.lower().split() if any(matches(w, t) for t in tokens))

    hits = [(score(n), len(n), n) for n in names]
    best = max(hits, default=None)
    return best[2] if best and best[0] > 0 else None


_STOPWORDS = {
    "какая",
    "какой",
    "какое",
    "какие",
    "каков",
    "сколько",
    "почему",
    "зачем",
    "когда",
    "где",
    "что",
    "чем",
    "как",
    "это",
    "мне",
    "нам",
    "для",
    "при",
    "над",
    "под",
    "без",
    "про",
    "его",
    "её",
    "их",
    "мой",
    "наш",
    "меня",
    "маржа",
    "маржи",
    "маржу",
    "цена",
    "цены",
    "фудкост",
    "наценка",
    "наценку",
    "себестоимость",
    "выгодно",
    "зарабатываем",
    "зарабатываю",
    "зарабатывает",
    "продано",
    "выручка",
    "сейчас",
    "тогда",
    "лучше",
    "хуже",
}


def _item_money_answer(conn, name):
    """Цифры по одной позиции: цена, сырьё, маржа, фудкост."""
    row = next((i for i in costing.item_economics(conn) if i["name"] == name), None)
    if row is None:
        return f"{_p(name)} за период не продавался — считать не по чему."
    if not row["cost_known"]:
        miss = ", ".join(row.get("missing") or []) or "закупочная цена"
        return (
            f"{_p(name)}: продано {row['qty']:.0f}, выручка {fmt(row['revenue'])} ₽, "
            f"средняя цена {row['price']:.0f} ₽.\n"
            f"Маржу посчитать не могу — не задана {miss}. "
            f"Напишите, например: «цена {name.lower()} 76»."
        )
    est = " (по типовым ценам сырья)" if row["estimated"] else ""
    return (
        f"{_p(name)}: цена {row['price']:.0f} ₽, сырьё {row['cost']:.0f} ₽, "
        f"маржа *{row['margin']:.0f} ₽* с порции, фудкост {_pct(row['foodcost'])}{est}.\n"
        f"За период продано {row['qty']:.0f} — это {fmt(row['margin_total'])} ₽ маржи."
    )


# ---------- срез данных для ИИ ----------
_DIGEST_CACHE: dict[tuple, Any] = {}


def data_digest(conn, days=56):
    """Компактный срез всех данных кофейни — контекст для умного ассистента.
    Кэшируется, пока в базе не появились новые чеки/списания/цены."""
    key = (days,) + analytics.db_state(conn)
    cached = _DIGEST_CACHE.get(key)
    if cached is not None:
        return cached
    _DIGEST_CACHE.clear()
    res = _digest_uncached(conn, days)
    _DIGEST_CACHE[key] = res
    return res


def _digest_uncached(conn, days=56):
    last = last_day_with_data(conn)
    start = last - timedelta(days=days - 1)
    tot = conn.execute(
        """SELECT COUNT(DISTINCT r.id) checks, SUM(i.qty*i.price) rev,
                  COUNT(DISTINCT substr(r.ts,1,10)) days
           FROM receipts r JOIN receipt_items i ON i.receipt_id=r.id
           WHERE substr(r.ts,1,10) BETWEEN ? AND ?""",
        (analytics.day_str(start), analytics.day_str(last)),
    ).fetchone()
    ndays = tot["days"] or 1
    revenue = tot["rev"] or 0
    checks = tot["checks"] or 0
    wd = analytics.weekday_breakdown(conn, days, last)
    wd_by_checks = sorted(
        [x for x in wd if x["avg_checks"]], key=lambda x: x["avg_checks"], reverse=True
    )
    hours = analytics.hour_breakdown(conn, days, last)
    peak = max(hours, key=lambda x: x["rev"]) if hours else None
    slow = min([h for h in hours if h["checks"] > 0], key=lambda x: x["rev"], default=None)
    econ = costing.totals(conn, days, last)
    w = conn.execute(
        "SELECT SUM(amount) a, COUNT(DISTINCT date) d FROM waste WHERE date BETWEEN ? AND ?",
        (analytics.day_str(start), analytics.day_str(last)),
    ).fetchone()
    return {
        "period": {"from": analytics.day_str(start), "to": analytics.day_str(last), "days": ndays},
        "totals": {
            "revenue": round(revenue),
            "checks": checks,
            "avg_daily_revenue": round(revenue / ndays),
            "avg_check": round(revenue / checks) if checks else 0,
        },
        "by_weekday": wd,
        "busiest_weekday": wd_by_checks[0] if wd_by_checks else None,
        "slowest_weekday": wd_by_checks[-1] if wd_by_checks else None,
        "peak_hour": peak,
        "slow_hour": slow,
        "top_products": analytics.top_products(conn, days, last, 8),
        "drink_mix": analytics.drink_mix(conn, days, last, 8),
        "by_category": [
            {"cat": c["cat"], "share": round(c["share"] * 100), "rev": round(c["rev"])}
            for c in analytics.revenue_by_category_window(conn, days, last)
        ],
        "week_trend": analytics.week_trend(conn, last),
        "foodcost": econ["foodcost"],
        "margin": econ["margin"],
        "avg_daily_waste": round((w["a"] or 0) / (w["d"] or 1)),
        "case_order_tomorrow": [
            {"name": i["name"], "recommended": i["recommended"]}
            for i in demand.case_order(conn)["items"]
        ],
    }


def _digest_text(conn):
    """Человеко-читаемый срез — контекст для ИИ.

    Чем полнее и конкретнее срез, тем точнее отвечает небольшая модель: она не
    считает сама, а пересказывает готовые числа.
    """
    d = data_digest(conn)
    p, t = d["period"], d["totals"]
    L = [
        f"Заведение: кофейня «{config.VENUE_NAME}».",
        f"Период данных: {p['from']}..{p['to']} ({p['days']} дней).",
        f"Итого за период: выручка {t['revenue']} ₽, чеков {t['checks']}, "
        f"средняя выручка/день {t['avg_daily_revenue']} ₽, средний чек {t['avg_check']} ₽.",
        "Средние показатели по дням недели (чеков / выручка ₽):",
    ]
    for x in d["by_weekday"]:
        L.append(f"  {x['weekday']}: {x['avg_checks']} чеков / {x['avg_revenue']} ₽")
    if d["busiest_weekday"]:
        L.append(f"Самый загруженный день: {d['busiest_weekday']['weekday']}.")
    if d["slowest_weekday"]:
        L.append(f"Самый слабый день: {d['slowest_weekday']['weekday']}.")
    if d["peak_hour"]:
        L.append(f"Пиковый час по выручке: {d['peak_hour']['hour']}:00.")
    if d["slow_hour"]:
        L.append(f"Самый слабый час: {d['slow_hour']['hour']}:00.")
    L.append(
        "Напитки за период (чашек / доля): "
        + "; ".join(
            f"{x['name']} — {x['qty']} шт / {round(x['share'] * 100)}%" for x in d["drink_mix"]
        )
    )
    L.append(
        "Топ позиций (продано / выручка ₽ / цена за штуку ₽): "
        + "; ".join(
            f"{x['name']} — {x['qty']}/{x['rev']}/{round(x['rev'] / x['qty']) if x['qty'] else 0}"
            for x in d["top_products"]
        )
    )
    L.append(
        "Доли категорий в выручке: "
        + "; ".join(f"{c['cat']} {c['share']}%" for c in d["by_category"])
    )

    if d["foodcost"] is not None:
        L.append(
            f"Фудкост (доля сырья в выручке): {round(d['foodcost'] * 100, 1)}%, "
            f"маржа за период {d['margin']} ₽."
        )
        econ = costing.item_economics(conn)
        known = [i for i in econ if i["cost_known"]][:10]
        if known:
            L.append(
                "Маржа по позициям (цена ₽ / себестоимость ₽ / маржа ₽ / фудкост %): "
                + "; ".join(
                    f"{i['name']} — {i['price']:.0f}/{i['cost']:.0f}/"
                    f"{i['margin']:.0f}/{round(i['foodcost'] * 100)}"
                    for i in known
                )
            )
    else:
        L.append("Себестоимость не посчитана: не заданы закупочные цены сырья.")

    for m in costing.milk_economics(conn)[:5]:
        L.append(
            f"Молоко {m['milk']}: {round(m['share'] * 100)}% чашек, цена {m['price']} ₽, "
            f"себестоимость {m['cost']} ₽, маржа {m['margin']} ₽."
        )

    a = analytics.attach_rate(conn)
    if a.get("rate") is not None:
        L.append(f"Attach-rate (доля кофейных чеков с едой): {round(a['rate'] * 100, 1)}%.")
        if a.get("morning_vs_day"):
            s = a["morning_vs_day"]
            L.append(
                f"  утром до 11:00 — {round(s['morning'] * 100, 1)}%, "
                f"после — {round(s['day'] * 100, 1)}%."
            )

    sp = staffing.shift_plan(conn)
    if sp.get("capacity", {}).get("known"):
        L.append(
            f"Пропускная способность: около {sp['capacity']['cups_per_hour']:.0f} "
            f"чашек в час (собственный повторяемый максимум)."
        )
    if sp.get("windows"):
        L.append(
            "Часы работы на пределе: "
            + ", ".join(f"{a2:02d}:00-{b:02d}:00" for a2, b in sp["windows"])
            + "."
        )

    rep = supply.reorder(conn)
    if rep["items"]:
        L.append(
            "Расход сырья в день: "
            + "; ".join(f"{i['name']} {i['per_day']} {i['unit']}" for i in rep["items"][:8])
        )
        urgent = [i for i in rep["items"] if i["urgency"] in ("critical", "soon")]
        if urgent:
            L.append(
                "Заканчивается: "
                + "; ".join(f"{i['name']} — на {i['days_left']} дн." for i in urgent)
            )

    last = last_day_with_data(conn)
    ys = analytics.sales_summary(conn, last)
    ytop = analytics.top_positions(conn, last, 6)
    L.append(
        f"Вчера ({last.isoformat()}): выручка {round(ys['revenue'])} ₽, "
        f"{ys['checks']} чеков, средний чек {round(ys['avg'])} ₽, "
        f"{round(ys['cups'])} чашек, нал {round(ys['cash'])} ₽ / "
        f"безнал {round(ys['card'])} ₽."
        + (
            " Продано вчера: " + "; ".join(f"{x['name']} {round(x['qty'])}" for x in ytop)
            if ytop
            else ""
        )
    )
    L.append(f"Средние списания в день: {d['avg_daily_waste']} ₽.")
    lost = demand.lost_sales_report(conn)
    if lost["items"]:
        L.append(
            f"Упущенная выручка витрины (еда кончалась до закрытия): "
            f"{lost['total_per_day']} ₽/день, около {lost['total_per_month']} ₽/мес. "
            "По позициям: "
            + "; ".join(
                f"{i['name']} — кончался {i['sellout_days']} дн., {i['lost_money_per_month']} ₽/мес"
                for i in lost["items"][:6]
            )
        )
    else:
        L.append("Упущенной выручки нет: витрина до закрытия не кончалась.")
    L.append(
        "Заказ витрины на завтра (штук): "
        + "; ".join(f"{i['name']} {i['recommended']}" for i in d["case_order_tomorrow"])
    )
    L.append(
        "Важно при ответах: напитки делаются на заказ и кончиться не могут — "
        "распроданность и заказ на завтра касаются только витрины (еды). "
        "Заказ витрины намеренно выше среднего спроса: пустая витрина стоит "
        "дороже остатка."
    )
    return "\n".join(L)


def smart_answer(conn, text):
    """Умный ответ на свободный вопрос по срезу данных кофейни.

    Возвращает строку либо None — если ИИ вообще не настроен. Если ИИ настроен,
    но не ответил, поднимает llm.LLMUnavailable с понятной причиной: молчать
    про поломку нельзя, иначе бот выглядит так, будто «не умеет отвечать».
    """
    if not config.llm_enabled():
        return None
    ctx = _digest_text(conn)
    sysmsg = (
        f"Ты — ассистент-аналитик кофейни «{config.VENUE_NAME}». Отвечаешь владельцу "
        "по-русски: коротко, по делу, живым языком, без воды.\n"
        "ГЛАВНОЕ ПРАВИЛО: все числа бери ТОЛЬКО из блока ДАННЫЕ КОФЕЙНИ ниже. Никогда не "
        "выдумывай и не оценивай цифры «на глаз».\n"
        "1) Вопрос о показателях (продажи, выручка, чеки, средний чек, дни недели, часы, "
        "напитки, размеры, молоко, маржа, фудкост, расход сырья, витрина, упущенная "
        "выручка, касса) — отвечай точными числами из данных. Если нужен разрез, "
        "которого в данных нет, не считай в уме — честно скажи, что точной цифры нет, и "
        "подскажи кнопку («Маржа и меню», «Витрина на завтра», «Заявка поставщику», "
        "«Еда к кофе», «Смена», «Упущенная выручка») или команду.\n"
        "2) Вопрос-совет (стоит ли поднять цену, что продвигать, как ставить смену, "
        "почему проседает день, что делать с растительным молоком) — рассуждай как "
        "опытный управляющий кофейней, опираясь на числа из данных, но не приписывай "
        "кофейне цифр, которых в данных нет. Предложи один конкретный следующий шаг.\n"
        "3) Себестоимость здесь — только сырьё по рецептуре. Аренду, зарплаты и налоги "
        "система не знает: не выдавай маржу за чистую прибыль.\n"
        "Отвечай 1–4 предложениями. Не используй символы * и _ для оформления.\n\n"
        f"ДАННЫЕ КОФЕЙНИ:\n{ctx}"
    )
    return llm.complete(sysmsg, text) or None
