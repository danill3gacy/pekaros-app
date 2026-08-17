"""Тесты ядра КофейняОС. Запуск:  python -m pytest -q

Тесты делятся на три группы, и все три нужны:

1. **Считалки дают правильные числа** — на синтетических данных, где верный
   ответ известен заранее.
2. **Продукт не выдумывает** — если данных нет, показывается «не считается»,
   а не бодрый ноль; если цена неизвестна, маржа не «100%», а «неизвестна».
3. **Регрессии** — каждый пункт здесь когда-то был настоящим дефектом.
"""

import datetime as dt
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# отдельная тестовая БД; ИИ отключаем, чтобы тесты не ходили в сеть
os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "coffeeos_test.db")
os.environ["OPENAI_API_KEY"] = ""
os.environ["LLM_BASE_URL"] = ""
os.environ["LLM_API_KEY"] = ""

from coffeeos import (  # noqa: E402
    analytics,
    catalog,
    config,
    costing,
    db,
    demand,
    health,
    menu,
    orchestrator,
    reference,
    seed,
    staffing,
    supply,
)

SEED_DAYS = 56
SEED_CHECKS = 150


_BASELINE = {}


def setup_module(_=None):
    seed.seed(days=SEED_DAYS, base_checks=SEED_CHECKS, seed_val=7)
    conn = db.get_conn()
    _BASELINE["receipts"] = conn.execute("SELECT COUNT(*) c FROM receipts").fetchone()["c"]
    _BASELINE["ingredients"] = [dict(r) for r in conn.execute("SELECT * FROM ingredients")]
    conn.close()


def _reseed():
    """Вернуть демо-данные после тестов, которые чистят базу.

    Сам seed бережёт ручные списания персонала (это реальные данные), но между
    тестами их надо убирать: иначе списание из одного теста меняет выводы
    другого — по такому дню система считает, что витрину не распродали.
    """
    seed.seed(days=SEED_DAYS, base_checks=SEED_CHECKS, seed_val=7)
    _cleanup()


def _cleanup():
    """Снять побочные эффекты теста: ручные списания, остатки, правки цен.

    Без этого тесты становятся зависимыми от порядка запуска: изменённая цена
    зерна из одного теста ломает проверку маржи в другом, и разбираться в этом
    приходится дольше, чем в настоящем дефекте.
    """
    conn = db.get_conn()
    conn.execute("DELETE FROM waste WHERE src='user'")
    conn.execute("DELETE FROM stock_counts")
    conn.execute("DELETE FROM case_order_override")
    conn.execute("DELETE FROM kv WHERE key='alert_last'")
    for row in _BASELINE.get("ingredients", []):
        conn.execute(
            "UPDATE ingredients SET pack_price=?, pack_qty=?, price_src=? WHERE name=?",
            (row["pack_price"], row["pack_qty"], row["price_src"], row["name"]),
        )
    conn.execute("UPDATE maintenance SET last_done=NULL")
    conn.commit()
    conn.close()
    _drop_caches()


@pytest.fixture(autouse=True)
def _demo_stays_intact():
    """После каждого теста база возвращается в исходное состояние.

    Если тест уронил исключение посреди синтетического сценария, база остаётся
    вычищенной — и падают все следующие тесты, скрывая настоящую причину.
    """
    yield
    conn = db.get_conn()
    n = conn.execute("SELECT COUNT(*) c FROM receipts").fetchone()["c"]
    conn.close()
    base = _BASELINE.get("receipts", 0)
    if base and abs(n - base) > base * 0.02:
        _reseed()
    else:
        _cleanup()


def _drop_caches():
    orchestrator._DIGEST_CACHE.clear()
    demand._DEMAND_CACHE.clear()


def _wipe(conn):
    """Пустая база для синтетического сценария."""
    for t in (
        "receipt_items",
        "receipts",
        "menu_items",
        "waste",
        "case_order_override",
        "stock_counts",
    ):
        conn.execute(f"DELETE FROM {t}")
    conn.execute("DELETE FROM ingredients WHERE category IN ('case','goods')")
    conn.execute("DELETE FROM recipes WHERE src='auto'")
    conn.commit()
    _drop_caches()


_RID = [0]


def _receipt(conn, when, items, payment="card", channel=None, barista=None, guest=None):
    """Добавить чек с разобранными позициями. items: [(имя, кол-во, цена)]."""
    _RID[0] += 1
    cur = conn.execute(
        "INSERT INTO receipts(ts,payment,ext_id,channel,barista,guest) VALUES(?,?,?,?,?,?)",
        (when.isoformat(), payment, f"t{_RID[0]}", channel, barista, guest),
    )
    rid = cur.lastrowid
    for name, qty, price in items:
        p = catalog.resolve(conn, name)
        catalog.register(conn, p)
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
    return rid


# ======================================================================
# Разбор позиции чека
# ======================================================================
@pytest.mark.parametrize(
    "name,base,kind,size,milk,iced",
    [
        ("Латте 350 мл", "Латте", "drink", "M", "обычное", 0),
        ("Латте 450 на овсяном", "Латте", "drink", "L", "овсяное", 0),
        ("Латте 0,25", "Латте", "drink", "S", "обычное", 0),
        ("Айс американо 400", "Американо", "drink", "L", None, 1),
        ("Капучино M, безлактозное", "Капучино", "drink", "M", "безлактозное", 0),
        ("Флэт уайт", "Флэт уайт", "drink", None, "обычное", 0),
        ("Латте макиато 350", "Латте макиато", "drink", "M", "обычное", 0),
        ("Колд брю 300", "Колд брю", "drink", "M", None, 1),
        ("Чай облепиховый 400", "Чай", "drink", "L", None, 0),
        ("Круассан миндальный", "Круассан миндальный", "food", None, None, 0),
        ("Сырники со сметаной", "Сырники со сметаной", "food", None, None, 0),
    ],
)
def test_menu_parsing(name, base, kind, size, milk, iced):
    p = menu.parse(name)
    assert p["base"] == base
    assert p["kind"] == kind
    assert p["size"] == size
    assert p["milk"] == milk
    assert p["iced"] == iced


def test_matcha_latte_is_matcha_not_coffee():
    """«Матча латте» — это матча. Иначе система списала бы на неё 18 г зерна."""
    p = menu.parse("Матча латте 350")
    assert p["base"] == "Матча"
    assert menu.is_coffee("Матча") is False


def test_packaged_coffee_is_goods_not_a_drink():
    """«Кофе в зёрнах 250 г» — пачка с полки, а не выпитая чашка.

    Если счесть её напитком, система спишет зерно на зерно и добавит
    несуществующую чашку в расчёт загрузки бариста.
    """
    p = menu.parse("Кофе в зёрнах 250 г")
    assert p["kind"] == menu.KIND_GOODS


def test_modifier_line_is_not_food():
    """Отдельная строка «Овсяное молоко» — добавка, а не еда.

    Иначе attach-rate «кофе + еда» считает её едой и завышается вдвое.
    """
    assert menu.parse("Овсяное молоко")["kind"] == menu.KIND_ADDON
    assert menu.parse("Сироп карамель")["kind"] == menu.KIND_ADDON
    assert menu.parse("Доп. шот эспрессо")["kind"] == menu.KIND_ADDON


def test_service_items_are_not_products():
    for name in ("Пакет-майка", "Скидка 10%", "Возврат", "Депозит за кружку"):
        assert menu.parse(name)["kind"] == menu.KIND_SERVICE
    # исключения: содержат «опасную» подстроку, но это настоящий товар
    assert menu.parse("Сосиска в тесте")["kind"] == menu.KIND_FOOD
    assert menu.parse("Хлеб в упаковке")["kind"] == menu.KIND_FOOD


def test_unknown_size_stays_unknown():
    """Размер, которого нет в названии, не выдумывается."""
    p = menu.parse("Капучино")
    assert p["size"] is None
    # но для расчёта рецепта берётся средний — и об этом честно сообщается
    size, known = menu.size_or_default(None)
    assert size == "M" and known is False


# ======================================================================
# Себестоимость и маржа
# ======================================================================
def test_recipe_library_covers_all_drinks():
    """У каждого напитка из словаря есть рецепт всех трёх размеров."""
    recipes = {(i, s) for i, s, _ing, _q in reference.build_recipes()}
    for base, *_ in menu.DRINKS:
        for size in ("S", "M", "L"):
            assert (base, size) in recipes, f"нет рецепта {base} {size}"


def test_alternative_milk_costs_more():
    """Овсяное молоко дороже обычного — себестоимость обязана это видеть."""
    conn = db.get_conn()
    regular = costing.cost_of(conn, "Латте", "M", menu.MILK_REGULAR)
    oat = costing.cost_of(conn, "Латте", "M", "овсяное")
    conn.close()
    assert regular["known"] and oat["known"]
    assert oat["cost"] > regular["cost"], "подмена молока не попала в себестоимость"


def test_large_drink_costs_more_than_small():
    conn = db.get_conn()
    s = costing.cost_of(conn, "Латте", "S")
    large = costing.cost_of(conn, "Латте", "L")
    conn.close()
    assert large["cost"] > s["cost"]


def test_in_house_service_saves_disposables():
    """В зале подают в керамике — стакан и крышка не расходуются."""
    conn = db.get_conn()
    togo = costing.cost_of(conn, "Латте", "M", channel="takeaway")
    here = costing.cost_of(conn, "Латте", "M", channel="here")
    conn.close()
    assert here["cost"] < togo["cost"]
    assert not any(k.startswith("Стакан") for k in here["parts"])


def test_unknown_purchase_price_gives_no_margin_not_hundred_percent():
    """Цена закупки неизвестна — себестоимость None, а НЕ ноль.

    Ноль здесь означал бы маржу 100%, и владелец принял бы это за правду.
    """
    conn = db.get_conn()
    _wipe(conn)
    day = dt.datetime(2026, 7, 1, 9, 0)
    for i in range(30):
        _receipt(
            conn, day + dt.timedelta(days=i % 10, hours=i % 8), [("Круассан с лососем", 1, 300)]
        )
    conn.commit()
    # позиция заведена как закупаемая, но цену владелец не называл
    row = conn.execute(
        "SELECT price_src FROM ingredients WHERE name='Круассан с лососем'"
    ).fetchone()
    econ = costing.item_economics(conn, 30, dt.date(2026, 7, 10))
    item = next(i for i in econ if i["name"] == "Круассан с лососем")
    totals = costing.totals(conn, 30, dt.date(2026, 7, 10))
    conn.close()
    assert row["price_src"] == costing.PRICE_UNKNOWN
    assert item["cost"] is None and item["margin"] is None
    assert item["cost_known"] is False
    assert "Круассан с лососем" in totals["unpriced"]


def test_default_prices_are_marked_as_estimates():
    """Типовая цена — не своя. Продукт обязан об этом говорить."""
    conn = db.get_conn()
    res = costing.cost_of(conn, "Американо", "M")
    conn.close()
    assert res["known"] and res["estimated"], "типовые цены должны помечаться оценочными"


def test_owner_price_stops_being_an_estimate():
    conn = db.get_conn()
    costing.set_price(conn, "Зерно кофе", 2000)
    res = conn.execute(
        "SELECT pack_price, price_src FROM ingredients WHERE name='Зерно кофе'"
    ).fetchone()
    conn.close()
    assert res["pack_price"] == 2000 and res["price_src"] == costing.PRICE_OWNER


def test_menu_matrix_splits_into_four_groups():
    conn = db.get_conn()
    m = costing.menu_matrix(conn)
    conn.close()
    assert m["items"], "разбор меню не должен быть пустым на демо-данных"
    groups = {i["group"] for i in m["items"]}
    assert groups <= {costing.STAR, costing.WORKHORSE, costing.PUZZLE, costing.BALLAST}
    assert all(i["advice"] for i in m["items"])


def test_foodcost_is_between_zero_and_one():
    conn = db.get_conn()
    t = costing.totals(conn)
    conn.close()
    assert 0 < t["foodcost"] < 1
    assert t["margin"] > 0
    assert abs(t["cost"] + t["margin"] - t["revenue_costed"]) <= 2


# ======================================================================
# Витрина: спрос, распроданность, заказ
# ======================================================================
def test_case_order_is_nonnegative_and_nonempty():
    conn = db.get_conn()
    order = demand.case_order(conn)
    conn.close()
    assert order["items"], "заказ витрины не должен быть пустым на демо-данных"
    assert all(i["recommended"] >= 0 for i in order["items"])


def test_drinks_never_enter_the_case_order():
    """Латте кончиться не может. Если он попал в заказ витрины — это дефект."""
    conn = db.get_conn()
    order = demand.case_order(conn)
    kinds = {r["name"]: r["kind"] for r in conn.execute("SELECT name, kind FROM menu_items")}
    conn.close()
    for i in order["items"]:
        assert kinds.get(i["name"]) == menu.KIND_FOOD, f"{i['name']} — не витрина"


def test_drink_cannot_be_marked_as_case():
    conn = db.get_conn()
    res = catalog.set_stocked(conn, "Латте", True)
    conn.close()
    assert res and res.get("error") == "not_food"


def test_order_exceeds_mean_demand():
    """Заказ выше среднего спроса: берём с запасом под верхний перцентиль."""
    conn = db.get_conn()
    target = analytics.last_day_with_data(conn) + dt.timedelta(days=1)
    order = demand.case_order(conn, target)
    conn.close()
    above = [i for i in order["items"] if i["recommended"] >= i["demand_avg"]]
    assert len(above) >= len(order["items"]) * 0.8


def test_service_level_requires_a_buffer():
    from coffeeos import economics as ec

    sl = ec.service_level()
    assert ec.MIN_SERVICE <= sl <= ec.MAX_SERVICE
    assert sl > 0.5, "берём с запасом — выше медианы спроса"
    assert ec.z_for(sl) > 0


def test_service_level_is_configurable():
    """Уровень сервиса читается из настройки, а не застывает при импорте модуля."""
    from coffeeos import economics as ec

    old = config.CASE_SERVICE_LEVEL
    try:
        config.CASE_SERVICE_LEVEL = 0.85
        assert ec.plan_z()["z"] > ec.z_for(old)
    finally:
        config.CASE_SERVICE_LEVEL = old


def test_sellout_detected_from_receipts_only():
    """Распроданность видна по чекам: продажи оборвались раньше обычного."""
    conn = db.get_conn()
    _wipe(conn)
    for d in range(28):
        day = dt.date(2026, 7, 1) + dt.timedelta(days=d)
        short = d % 4 == 0  # сырники кончаются в полдень каждый четвёртый день
        for hour in range(8, 20):
            for _ in range(5):
                items = [("Круассан классический", 1, 180)]
                if not short or hour < 12:
                    items.append(("Сырники", 1, 340))
                _receipt(conn, dt.datetime(day.year, day.month, day.day, hour, 0), items)
    conn.commit()
    so = demand.sellout_days(conn, 30, dt.date(2026, 7, 28))
    conn.close()
    assert len(so.get("Сырники", {})) >= 5, "обрыв продаж раньше обычного — распроданность"
    assert len(so.get("Круассан классический", {})) == 0, "ровные продажи — не распроданность"


def test_morning_item_is_not_a_false_sellout():
    """Завтрак обрывается рано КАЖДЫЙ день — это не распроданность.

    Без этой проверки система рисует владельцу несуществующую упущенную
    выручку и раздувает заказ на треть.
    """
    conn = db.get_conn()
    _wipe(conn)
    for d in range(28):
        day = dt.date(2026, 7, 1) + dt.timedelta(days=d)
        for hour in range(8, 20):
            for _ in range(6):
                items = [("Овсяная каша", 1, 220)] if hour < 11 else []
                _receipt(
                    conn,
                    dt.datetime(day.year, day.month, day.day, hour, 0),
                    items or [("Американо 250", 1, 150)],
                )
    conn.commit()
    so = demand.sellout_days(conn, 30, dt.date(2026, 7, 28))
    lost = demand.lost_sales_report(conn, 30, dt.date(2026, 7, 28))
    conn.close()
    assert not so.get("Овсяная каша"), "утренний профиль продаж — не распроданность"
    assert lost["total_per_month"] == 0, "нельзя показывать упущенную выручку, которой нет"


def test_weekend_item_keeps_its_batch():
    """Позиция «только по субботам» не должна получать заказ в разы ниже спроса."""
    conn = db.get_conn()
    _wipe(conn)
    for d in range(56):
        day = dt.date(2026, 6, 1) + dt.timedelta(days=d)
        n = 10 if day.weekday() == 5 else 0
        for i in range(max(n, 4)):
            when = dt.datetime(day.year, day.month, day.day, 9 + i % 8, 0)
            items = [("Чизкейк Нью-Йорк", 1, 320)] if i < n else [("Латте 350", 1, 260)]
            _receipt(conn, when, items)
    conn.commit()
    sat = demand.case_order(conn, dt.date(2026, 7, 25))
    _drop_caches()
    mon = demand.case_order(conn, dt.date(2026, 7, 27))
    conn.close()
    rec_sat = next((i["recommended"] for i in sat["items"] if i["name"] == "Чизкейк Нью-Йорк"), 0)
    rec_mon = next((i["recommended"] for i in mon["items"] if i["name"] == "Чизкейк Нью-Йорк"), 0)
    assert rec_sat >= 6, f"на субботу заказ {rec_sat} при спросе 10 — позиция недозаказана"
    assert rec_sat > rec_mon, "субботний заказ должен быть выше понедельничного"


def test_new_item_is_not_crushed_by_history_length():
    """Новинка появилась вчера — её нельзя делить на 56 дней чужой истории."""
    conn = db.get_conn()
    _wipe(conn)
    for d in range(40):
        day = dt.date(2026, 6, 20) + dt.timedelta(days=d)
        for i in range(40):
            when = dt.datetime(day.year, day.month, day.day, 8 + i % 10, 0)
            items = [("Круассан классический", 1, 180)]
            if d >= 38:  # новинка живёт всего два дня
                items.append(("Брауни", 1, 230))
            _receipt(conn, when, items)
    conn.commit()
    order = demand.case_order(conn, dt.date(2026, 7, 30))
    conn.close()
    rec = next((i["recommended"] for i in order["items"] if i["name"] == "Брауни"), 0)
    assert rec >= 25, f"новинка продаётся по 40 шт/день, а заказ {rec}"


def test_waste_entry_overrides_sellout_guess():
    """Если непроданное было — позиция точно не кончилась, что бы ни думала эвристика."""
    conn = db.get_conn()
    _wipe(conn)
    for d in range(14):
        day = dt.date(2026, 7, 1) + dt.timedelta(days=d)
        conn.execute(
            "INSERT INTO waste(date,name,qty,unit,amount,kind,src) "
            "VALUES(?,?,?,'шт',?,'food','user')",
            (day.isoformat(), "Синнабон", 5, 1300),
        )
        for hour in range(8, 20):
            for _ in range(5):
                items = [("Синнабон", 1, 260)] if hour < 12 else [("Латте 350", 1, 260)]
                _receipt(conn, dt.datetime(day.year, day.month, day.day, hour, 0), items)
    conn.commit()
    so = demand.sellout_days(conn, 30, dt.date(2026, 7, 14))
    conn.close()
    assert not so.get("Синнабон"), "списания есть — значит не распродано"


def test_single_leftover_does_not_erase_sellout():
    """Одна списанная витринная позиция (брак, образец) не гасит сигнал за день."""
    conn = db.get_conn()
    _wipe(conn)
    for d in range(28):
        day = dt.date(2026, 7, 1) + dt.timedelta(days=d)
        short = d % 3 == 0
        if short:
            conn.execute(
                "INSERT INTO waste(date,name,qty,unit,amount,kind,src) "
                "VALUES(?,'Синнабон',1,'шт',260,'food','user')",
                (day.isoformat(),),
            )
        for hour in range(8, 20):
            for _ in range(6):
                items = [("Латте 350", 1, 260)]
                if not short or hour < 12:
                    items.append(("Синнабон", 1, 260))
                _receipt(conn, dt.datetime(day.year, day.month, day.day, hour, 0), items)
    conn.commit()
    so = demand.sellout_days(conn, 30, dt.date(2026, 7, 28))
    conn.close()
    assert so.get("Синнабон"), "одна списанная штука не должна отменять распроданность"


def test_returns_do_not_hide_sellouts():
    """Возврат вечером не должен изображать, что позиция продавалась до закрытия."""
    conn = db.get_conn()
    _wipe(conn)
    for d in range(28):
        day = dt.date(2026, 7, 1) + dt.timedelta(days=d)
        for hour in range(8, 20):
            for _ in range(6):
                items = [("Латте 350", 1, 260)]
                if hour < 12 or d % 3:
                    items.append(("Синнабон", 1, 260))
                _receipt(conn, dt.datetime(day.year, day.month, day.day, hour, 0), items)
        if d % 3 == 0:  # поздний возврат в день распродажи
            _receipt(
                conn, dt.datetime(day.year, day.month, day.day, 19, 40), [("Синнабон", -1, 260)]
            )
    conn.commit()
    so = demand.sellout_days(conn, 30, dt.date(2026, 7, 28))
    conn.close()
    assert so.get("Синнабон"), "возврат не задаёт час последней ПРОДАЖИ"


def test_returns_do_not_crash_the_reports():
    """Возвратов больше, чем продаж, — отчёты обязаны устоять, а не упасть."""
    conn = db.get_conn()
    _wipe(conn)
    for d in range(21):
        day = dt.date(2026, 7, 1) + dt.timedelta(days=d)
        for i in range(6):
            qty = -3 if (day.weekday() == 2 and i < 4) else 1
            _receipt(
                conn,
                dt.datetime(day.year, day.month, day.day, 9 + i, 0),
                [("Чизкейк Нью-Йорк", qty, 320)],
            )
    conn.commit()
    order = demand.case_order(conn, dt.date(2026, 7, 22))
    digest = orchestrator.data_digest(conn)
    conn.close()
    assert all(i["recommended"] >= 0 for i in order["items"])
    assert digest["totals"]["checks"] > 0


def test_order_survives_without_any_waste_input():
    """Бариста ни разу не ввёл списания — система обязана работать полноценно.

    Текучесть бариста — 4–6 месяцев. Продукт, который держится на ежевечернем
    ручном вводе, перестаёт работать через месяц вместе с человеком.
    """
    conn = db.get_conn()
    target = analytics.last_day_with_data(conn) + dt.timedelta(days=1)
    with_waste = {i["name"]: i["recommended"] for i in demand.case_order(conn, target)["items"]}
    conn.execute("DELETE FROM waste")
    conn.commit()
    _drop_caches()
    without = {i["name"]: i["recommended"] for i in demand.case_order(conn, target)["items"]}
    conn.close()
    assert without, "без списаний заказ не должен исчезать"
    assert set(without) == set(with_waste), "без списаний не должны пропадать позиции"
    total_a = sum(with_waste.values()) or 1
    total_b = sum(without.values())
    assert abs(total_b - total_a) / total_a < 0.25, "без списаний заказ не должен разъезжаться"


def test_case_order_never_offers_service_items():
    conn = db.get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO menu_items(name,category,kind,stocked) "
        "VALUES('Пакет-майка','Прочее','food',1)"
    )
    conn.commit()
    _drop_caches()
    order = demand.case_order(conn)
    conn.execute("DELETE FROM menu_items WHERE name='Пакет-майка'")
    conn.commit()
    conn.close()
    assert not any("Пакет" in i["name"] for i in order["items"])


def test_order_override_is_persisted():
    conn = db.get_conn()
    order = demand.case_order(conn)
    name = order["items"][0]["name"]
    target = dt.date.fromisoformat(order["target"])
    demand.set_order_override(conn, target, name, 42)
    _drop_caches()
    again = demand.case_order(conn)
    conn.execute("DELETE FROM case_order_override")
    conn.commit()
    conn.close()
    _drop_caches()
    row = next(i for i in again["items"] if i["name"] == name)
    assert row["recommended"] == 42 and row.get("adjusted")


def test_lost_sales_is_revenue_only():
    conn = db.get_conn()
    lost = demand.lost_sales_report(conn)
    conn.close()
    assert lost["total_per_month"] >= 0
    for i in lost["items"]:
        assert "cost" not in i and "margin" not in i


# ======================================================================
# Расход сырья и заявка
# ======================================================================
def test_consumption_matches_recipe_exactly():
    """100 латте размера M — это ровно 100×18 г зерна. Ни больше, ни меньше."""
    conn = db.get_conn()
    _wipe(conn)
    day = dt.date(2026, 7, 1)
    for i in range(100):
        _receipt(
            conn,
            dt.datetime(day.year, day.month, day.day, 8 + i % 10, i % 60),
            [("Латте 350", 1, 260)],
            channel="takeaway",
        )
    conn.commit()
    cons = supply.consumption(conn, 7, day)
    conn.close()
    beans = cons["items"]["Зерно кофе"]
    milk = cons["items"]["Молоко обычное"]
    assert beans["used"] == pytest.approx(100 * 18)
    assert milk["used"] == pytest.approx(100 * 200)
    assert cons["items"]["Стакан M"]["used"] == 100


def test_consumption_follows_the_milk_in_the_receipt():
    """Если налили овсяное — расход обычного молока не растёт."""
    conn = db.get_conn()
    _wipe(conn)
    day = dt.date(2026, 7, 1)
    for i in range(50):
        _receipt(
            conn,
            dt.datetime(day.year, day.month, day.day, 9, i % 60),
            [("Латте 350 на овсяном", 1, 320)],
        )
    conn.commit()
    cons = supply.consumption(conn, 7, day)["items"]
    conn.close()
    assert cons["Молоко овсяное"]["used"] == pytest.approx(50 * 200)
    assert "Молоко обычное" not in cons


def test_spilled_milk_counts_as_consumption():
    """Вылитое молоко всё равно приходится покупать."""
    conn = db.get_conn()
    _wipe(conn)
    day = dt.date(2026, 7, 1)
    for i in range(10):
        _receipt(conn, dt.datetime(day.year, day.month, day.day, 9, i), [("Латте 350", 1, 260)])
    conn.execute(
        "INSERT INTO waste(date,name,qty,unit,amount,kind,src) "
        "VALUES(?,'Молоко обычное',2,'л',190,'milk','user')",
        (day.isoformat(),),
    )
    conn.commit()
    cons = supply.consumption(conn, 7, day)["items"]
    conn.close()
    assert cons["Молоко обычное"]["used"] == pytest.approx(10 * 200 + 2000)


def test_order_quantity_does_not_double_count_lead_time():
    """Срок поставки влияет на КОГДА заказать, а не на СКОЛЬКО.

    Если прибавлять его к объёму каждый раз, запас растёт с каждым циклом, и
    через месяц под стойкой стоит месячный запас зерна на деньги из оборота.
    """
    conn = db.get_conn()
    rep = supply.reorder(conn, horizon_days=7)
    beans = next(i for i in rep["items"] if i["name"] == "Зерно кофе")
    conn.close()
    # без пересчёта остатков заказ покрывает ровно горизонт, а не горизонт+поставку
    assert beans["need"] <= beans["per_day"] * 7 * 1.25 + 0.01
    assert beans["urgency"] == "plan"


def test_perishables_are_not_ordered_for_two_weeks():
    """Двухнедельный запас молока — это не запас, это списание."""
    conn = db.get_conn()
    rep = supply.reorder(conn, horizon_days=21)
    milk = next(i for i in rep["items"] if i["name"] == "Молоко обычное")
    beans = next(i for i in rep["items"] if i["name"] == "Зерно кофе")
    conn.close()
    assert milk["need"] < milk["per_day"] * 10, "скоропорт заказан на слишком долгий срок"
    assert beans["need"] > beans["per_day"] * 15, "у зерна срока годности нет, ограничивать нечего"


def test_stock_count_turns_plan_into_a_deadline():
    """Пересчитали остаток — система говорит, на сколько хватит и когда встанет."""
    conn = db.get_conn()
    supply.record_stock(conn, "зерно", 2.0)
    rep = supply.reorder(conn)
    beans = next(i for i in rep["items"] if i["name"] == "Зерно кофе")
    conn.execute("DELETE FROM stock_counts")
    conn.commit()
    conn.close()
    assert beans["days_left"] is not None and beans["days_left"] < 2
    assert beans["urgency"] == "critical", "закончится раньше поставки — это «горит»"
    assert rep["counted"] is True


def test_reorder_without_stock_says_so_instead_of_guessing():
    conn = db.get_conn()
    rep = supply.reorder(conn)
    conn.close()
    assert rep["counted"] is False
    assert rep["note"] and "остат" in rep["note"].lower()
    assert all(i["days_left"] is None for i in rep["items"])


def test_case_positions_are_not_in_the_supplier_order():
    """Витрина заказывается отдельно и каждый день — у неё другой цикл."""
    conn = db.get_conn()
    rep = supply.reorder(conn)
    names = {i["name"] for i in rep["items"]}
    conn.close()
    assert "Круассан классический" not in names
    assert "Зерно кофе" in names


def test_order_draft_is_ready_to_forward():
    conn = db.get_conn()
    draft = supply.order_draft(conn)
    conn.close()
    assert draft["text"], "заявка должна быть готовым текстом"
    assert "Зерно кофе" in draft["text"]
    assert "Кофе:" in draft["text"] or "Молоко:" in draft["text"]


def test_maintenance_reports_overdue_tasks():
    conn = db.get_conn()
    due = supply.maintenance_due(conn)
    assert due, "на свежей базе регламент ни разу не отмечали — всё просрочено"
    task = supply.mark_done(conn, "калибров")
    after = supply.maintenance_due(conn)
    conn.close()
    assert task and "алибров" in task
    assert task not in [d["task"] for d in after]


# ======================================================================
# Смена и пропускная способность
# ======================================================================
def test_capacity_is_the_shops_own_record():
    conn = db.get_conn()
    cap = staffing.hourly_capacity(conn)
    hours = analytics.hour_breakdown(conn)
    conn.close()
    assert cap["known"]
    busiest = max(h["cups"] / max(1, h["days"]) for h in hours)
    assert cap["cups_per_hour"] >= busiest, "потолок не может быть ниже среднего часа"


def test_clipping_is_measured_not_assumed():
    """Прирост от второго бариста заявляется, только если упор в потолок доказан."""
    conn = db.get_conn()
    sp = staffing.shift_plan(conn)
    conn.close()
    if sp["scenario"]:
        assert sp["scenario"]["clipped_share"] >= staffing.CLIP_CONFIRMED
        assert sp["scenario"]["assumption"], "сценарий обязан нести своё допущение"
        assert sp["scenario"]["confirmed_hours"]
    else:
        assert sp["note"], "нет сценария — должно быть объяснение почему"


def test_even_flow_gets_no_second_barista():
    """Ровный поток — второго человека предлагать не за что."""
    conn = db.get_conn()
    _wipe(conn)
    for d in range(30):
        day = dt.date(2026, 7, 1) + dt.timedelta(days=d)
        for hour in range(8, 20):
            for i in range(4):
                _receipt(
                    conn,
                    dt.datetime(day.year, day.month, day.day, hour, i * 10),
                    [("Латте 350", 1, 260)],
                )
    conn.commit()
    sp = staffing.shift_plan(conn, 30, dt.date(2026, 7, 30))
    conn.close()
    assert sp["scenario"] is None
    assert sp["note"]


def test_shift_answer_is_computed_not_hardcoded():
    conn = db.get_conn()
    txt = orchestrator.answer_shift(conn)
    last = analytics.last_day_with_data(conn)
    hours = [h for h in analytics.hour_breakdown(conn, 56, last) if h["checks"] > 0]
    conn.close()
    peak = max(hours, key=lambda x: x["checks"])["hour"]
    assert f"{peak:02d}:00" in txt, "в ответе должен быть реальный пик кофейни"


# ======================================================================
# Аналитика кофейни
# ======================================================================
def test_attach_rate_ignores_modifier_lines():
    """«Латте + овсяное молоко» — это чек без еды."""
    conn = db.get_conn()
    _wipe(conn)
    day = dt.date(2026, 7, 1)
    for d in range(20):
        for i in range(30):
            when = dt.datetime(day.year, day.month, day.day, 8 + i % 10, i % 60) + dt.timedelta(
                days=d
            )
            items = [("Латте 350", 1, 260), ("Овсяное молоко", 1, 60)]
            if i < 6:
                items.append(("Круассан классический", 1, 180))
            _receipt(conn, when, items)
    conn.commit()
    a = analytics.attach_rate(conn, 30, day + dt.timedelta(days=19))
    conn.close()
    assert a["rate"] == pytest.approx(6 / 30, abs=0.02), (
        "модификатор посчитан как еда — attach-rate завышен"
    )


def test_attach_scenario_is_modest_and_labelled():
    """Оценка потенциала — по собственной медиане, и всегда с допущением."""
    conn = db.get_conn()
    a = analytics.attach_rate(conn)
    conn.close()
    if a["scenario"]:
        assert a["scenario"]["target_rate"] <= a["best_hour"]["rate"], (
            "цель не должна быть равна лучшему часу — это недоказуемая цифра"
        )
        assert a["scenario"]["assumption"]


def test_guests_and_barista_report_absence_honestly():
    """Нет идентификатора гостя — так и сказать, а не показать нули."""
    conn = db.get_conn()
    conn.execute("UPDATE receipts SET guest=NULL")
    conn.commit()
    g = analytics.guests(conn)
    conn.close()
    _reseed()  # число чеков не изменилось, автовосстановление не сработает
    assert g["available"] is False and g["note"]


def test_guests_are_counted_when_the_till_provides_them():
    conn = db.get_conn()
    g = analytics.guests(conn)
    conn.close()
    assert g["available"] is True
    assert 0 < g["repeat_share"] <= 1
    assert g["guests"] > 0


def test_size_and_milk_mix_sum_to_one():
    conn = db.get_conn()
    for mix in (analytics.size_mix(conn), analytics.milk_mix(conn)):
        assert abs(sum(x["share"] for x in mix) - 1.0) < 0.02
    conn.close()


def test_iced_share_follows_the_season():
    """Доля холодных напитков растёт к лету — это видно по своей же истории."""
    conn = db.get_conn()
    ic = analytics.iced_share(conn)
    conn.close()
    assert 0 <= ic["share"] <= 1
    assert ic["by_month"], "разбивка по месяцам должна быть"


def test_category_shares_stay_sane_with_big_return():
    conn = db.get_conn()
    last = analytics.last_day_with_data(conn)
    _receipt(conn, dt.datetime(last.year, last.month, last.day, 12, 0), [("Латте 350", -50, 260)])
    conn.commit()
    cats = analytics.revenue_by_category(conn, last)
    conn.close()
    assert all(0 <= c["share"] <= 1 for c in cats)


def test_revenue_by_category_sums_to_100():
    conn = db.get_conn()
    last = analytics.last_day_with_data(conn)
    cats = analytics.revenue_by_category(conn, last)
    conn.close()
    assert abs(sum(c["share"] for c in cats) - 1.0) < 0.01


def test_sales_summary_splits_payment():
    conn = db.get_conn()
    last = analytics.last_day_with_data(conn)
    s = analytics.sales_summary(conn, last)
    conn.close()
    assert s["revenue"] > 0 and s["checks"] > 0
    assert abs(s["cash"] + s["card"] - s["revenue"]) < 1
    assert s["cups"] > 0


# ======================================================================
# Списания
# ======================================================================
def test_case_waste_is_priced_by_the_price_tag():
    conn = db.get_conn()
    last = analytics.last_day_with_data(conn)
    res = catalog.add_waste(conn, "сырники", 4, last)
    w = catalog.waste_report(conn, last)
    conn.close()
    assert res and res["kind"] == "food" and res["unit"] == "шт"
    assert res["amount"] > 0
    assert w["case_total"] > 0


def test_milk_waste_is_priced_by_cost_not_by_menu():
    """Вылитое молоко — это расход по себестоимости, а не непроданная выручка."""
    conn = db.get_conn()
    costing.set_price(conn, "Молоко обычное", 100)  # 100 ₽ за литр
    last = analytics.last_day_with_data(conn)
    res = catalog.add_waste(conn, "молоко обычное", 1.5, last)
    conn.close()
    assert res and res["kind"] == "milk" and res["unit"] == "л"
    assert res["amount"] == pytest.approx(150, abs=1)


def test_waste_report_separates_case_and_raw():
    """Витрина по ценнику и сырьё по себестоимости не складываются в одну строку."""
    conn = db.get_conn()
    last = analytics.last_day_with_data(conn)
    catalog.add_waste(conn, "сырники", 2, last)
    catalog.add_waste(conn, "молоко обычное", 1, last)
    w = catalog.waste_report(conn, last)
    conn.close()
    assert w["case"] and w["raw"]
    assert w["case_total"] > 0 and w["raw_total"] > 0


def test_waste_rejects_garbage():
    conn = db.get_conn()
    assert catalog.add_waste(conn, "чт", 5) is None  # слишком короткое имя
    assert catalog.add_waste(conn, "сырники", 0) is None  # ноль
    assert catalog.add_waste(conn, "сырники", -3) is None  # минус
    assert catalog.add_waste(conn, "сырники", 99999) is None  # неправдоподобно
    assert catalog.add_waste(conn, "нетакоготовара", 5) is None
    conn.close()


def test_milk_waste_absence_is_reported_as_blind_spot():
    """Если вылитое молоко не отмечают, продукт говорит, что этой потери не видит."""
    conn = db.get_conn()
    conn.execute("DELETE FROM waste WHERE kind='milk'")
    conn.commit()
    mw = catalog.milk_waste(conn)
    conn.close()
    assert mw["tracked"] is False and mw["note"]


# ======================================================================
# Оркестратор: маршрутизация и ввод
# ======================================================================
@pytest.mark.parametrize(
    "text,intent",
    [
        ("что заказать на завтра", "case"),
        ("какая маржа", "margin"),
        ("на сколько хватит зерна", "supply"),
        ("еда к кофе", "attach"),
        ("сколько недопродали", "lost"),
        ("сверка кассы", "cash"),
        ("нужен ли второй бариста", "shift"),
        ("списания", "waste"),
        ("что пьют", "drinks"),
        ("выручка по категориям", "revenue"),
    ],
)
def test_routing_does_not_confuse_topics(text, intent):
    assert orchestrator.route(text) == intent


def test_all_buttons_answer_without_crashing():
    conn = db.get_conn()
    for label in orchestrator.BUTTONS:
        txt = orchestrator.answer(conn, label, allow_write=False)
        assert txt and len(txt) > 20, f"кнопка «{label}» ничего не ответила"
    conn.close()


def test_waste_question_does_not_write():
    """«Покажи списания за 3 дня» — это вопрос, а не ввод."""
    conn = db.get_conn()
    before = conn.execute("SELECT COUNT(*) c FROM waste").fetchone()["c"]
    orchestrator.answer(conn, "покажи списания за 3 дня")
    after = conn.execute("SELECT COUNT(*) c FROM waste").fetchone()["c"]
    conn.close()
    assert before == after


def test_unparsed_waste_command_does_not_answer_with_report():
    """Непонятый ввод списания обязан сказать «не понял», а не показать отчёт.

    Иначе бариста уверен, что списание записано, а его нет.
    """
    conn = db.get_conn()
    txt = orchestrator.answer(conn, "списание 5 круассан")
    conn.close()
    assert "не понял" in txt.lower()


def test_web_is_read_only():
    conn = db.get_conn()
    before = conn.execute("SELECT COUNT(*) c FROM waste").fetchone()["c"]
    txt = orchestrator.answer(conn, "списание круассаны 3", allow_write=False)
    after = conn.execute("SELECT COUNT(*) c FROM waste").fetchone()["c"]
    conn.close()
    assert before == after
    assert "только в Telegram" in txt


def test_price_command_updates_margin():
    conn = db.get_conn()
    txt = orchestrator.answer(conn, "цена зерно 2500")
    row = conn.execute(
        "SELECT pack_price, price_src FROM ingredients WHERE name='Зерно кофе'"
    ).fetchone()
    conn.close()
    assert "✅" in txt and row["pack_price"] == 2500 and row["price_src"] == "owner"


def test_stock_command_records_and_answers_days_left():
    conn = db.get_conn()
    txt = orchestrator.answer(conn, "остаток зерно 3")
    conn.execute("DELETE FROM stock_counts")
    conn.commit()
    conn.close()
    assert "✅" in txt and "хватит" in txt.lower()


def test_milk_waste_phrase_is_understood():
    conn = db.get_conn()
    n_before = conn.execute("SELECT COUNT(*) c FROM waste WHERE kind='milk'").fetchone()["c"]
    txt = orchestrator.answer(conn, "вылил молоко 1,5")
    n_after = conn.execute("SELECT COUNT(*) c FROM waste WHERE kind='milk'").fetchone()["c"]
    conn.close()
    assert n_after == n_before + 1 and "✅" in txt


def test_item_names_do_not_break_markdown():
    conn = db.get_conn()
    _receipt(conn, dt.datetime(2026, 7, 1, 9, 0), [("Кофе 3_в_1 *акция*", 1, 100)])
    conn.commit()
    txt = orchestrator._p("Кофе 3_в_1 *акция*")
    conn.close()
    assert "\\_" in txt and "\\*" in txt
    assert orchestrator.plain(txt) == "Кофе 3_в_1 *акция*"


def test_empty_database_raises_alarm_not_cheerful_zero():
    conn = db.get_conn()
    for t in ("receipt_items", "receipts"):
        conn.execute(f"DELETE FROM {t}")
    conn.commit()
    _drop_caches()
    brief = orchestrator.morning_brief(conn)
    st = health.status_text(conn)
    conn.close()
    assert "Нет данных" in brief
    assert "нет данных" in st.lower()


@pytest.mark.parametrize(
    "question,item",
    [
        ("какая маржа у рафа", "Раф"),
        ("фудкост чая", "Чай"),
        ("сколько стоит какао", "Какао"),
        ("сколько зарабатываем на латте", "Латте"),
        ("маржа круассан миндальный", "Круассан миндальный"),
        ("какая маржа у капучино", "Капучино"),
    ],
)
def test_question_about_one_item_gets_that_item(question, item):
    """«Какая маржа у рафа» — это вопрос про раф, а не про всё меню.

    Общий отчёт здесь бесполезен: владелец спросил про одну чашку и должен
    получить её цифры, а не искать строку глазами.
    """
    conn = db.get_conn()
    txt = orchestrator.plain(orchestrator.answer(conn, question, allow_write=False))
    conn.close()
    assert txt.startswith(item + ":"), f"«{question}» ответил не про {item}: {txt[:60]}"
    assert "маржа" in txt and "фудкост" in txt


@pytest.mark.parametrize(
    "question",
    [
        "какой график",  # «раф» внутри «график»
        "какая маржа",  # «какая» похоже на «какао»
        "во сколько пик",
        "сколько недопродали",
    ],
)
def test_item_matching_has_no_false_positives(question):
    """Слово названия не должно находиться внутри чужого слова."""
    conn = db.get_conn()
    found = orchestrator._item_in_text(conn, question)
    conn.close()
    assert found is None, f"«{question}» ошибочно опознан как позиция «{found}»"


def test_item_answer_admits_unknown_purchase_price():
    conn = db.get_conn()
    _wipe(conn)
    for i in range(30):
        _receipt(
            conn,
            dt.datetime(2026, 7, 1, 9, 0) + dt.timedelta(days=i % 10, hours=i % 8),
            [("Круассан с лососем", 1, 300)],
        )
    conn.commit()
    txt = orchestrator.plain(orchestrator._item_money_answer(conn, "Круассан с лососем"))
    conn.close()
    assert "не могу" in txt.lower() and "цена круассан с лососем" in txt.lower()


def test_local_analytics_answers_without_ai():
    conn = db.get_conn()
    for q in (
        "в какой день недели больше всего выручки?",
        "во сколько пик продаж?",
        "что продаётся лучше всего?",
        "какой средний чек за 2 недели?",
        "как дела?",
    ):
        a = orchestrator.local_analytics(conn, q)
        assert a, f"нет офлайн-ответа на «{q}»"
    conn.close()


def test_digest_has_coffee_specific_numbers():
    conn = db.get_conn()
    txt = orchestrator._digest_text(conn)
    conn.close()
    for needle in (
        "Фудкост",
        "Attach-rate",
        "Молоко",
        "Расход сырья",
        "Пропускная способность",
        "Напитки за период",
    ):
        assert needle in txt, f"в срезе для ИИ нет раздела «{needle}»"


# ======================================================================
# Регрессии, найденные сплошным аудитом продукта
# ======================================================================
def test_zero_price_position_does_not_break_the_milk_report():
    """Позиция с нулевой ценой (акция) — прочерк в ячейке, а не падение раздела."""
    conn = db.get_conn()
    last = analytics.last_day_with_data(conn)
    for i in range(6):
        _receipt(
            conn,
            dt.datetime(last.year, last.month, last.day, 9, i),
            [("Латте 350 на овсяном", 1, 0)],
        )
    conn.commit()
    _drop_caches()
    txt = orchestrator.answer_milk(conn)  # раньше падал на None * 100
    conn.close()
    _reseed()
    assert "молоко" in txt.lower()
    assert orchestrator._pct(None) == "—"


def test_margin_revenue_matches_the_till():
    """«Выручка» в разделе маржи — это выручка заведения, а не часть её.

    Владелец сверяет цифру с кассой. Раньше там стояла сумма только по
    напиткам и витрине — на несколько процентов меньше, и расхождение он
    воспринимал как ошибку системы.
    """
    conn = db.get_conn()
    last = analytics.last_day_with_data(conn)
    start = last - dt.timedelta(days=55)
    real = conn.execute(
        "SELECT SUM(i.qty*i.price) r FROM receipts rr JOIN receipt_items i "
        "ON i.receipt_id=rr.id WHERE substr(rr.ts,1,10) BETWEEN ? AND ?",
        (start.isoformat(), last.isoformat()),
    ).fetchone()["r"]
    t = costing.totals(conn)
    conn.close()
    assert abs(t["revenue"] - real) <= 1, "выручка раздела не совпала с кассовой"
    assert t["revenue_menu"] <= t["revenue"]
    assert 0 <= t["coverage"] <= 1


def test_ambiguous_write_off_asks_instead_of_guessing():
    """Два круассана в витрине — «списание круассан 3» обязано переспросить.

    Молча выбранная позиция хуже отказа: списание гасит признак
    распроданности, и по настоящему кончившемуся круассану упущенная выручка
    перестаёт считаться, а по невиновному растут «остатки».
    """
    conn = db.get_conn()
    res = catalog.add_waste(conn, "круассан", 3)
    txt = orchestrator.plain(orchestrator.answer(conn, "списание круассан 3"))
    n = conn.execute("SELECT COUNT(*) c FROM waste WHERE src='user'").fetchone()["c"]
    conn.close()
    assert res and res.get("ambiguous"), "неоднозначность не распознана"
    assert len(res["ambiguous"]) >= 2
    assert "уточните" in txt.lower()
    assert n == 0, "при неоднозначности в базу писать нельзя"


def test_unambiguous_write_off_still_works():
    conn = db.get_conn()
    res = catalog.add_waste(conn, "круассан миндальный", 3)
    conn.close()
    assert res and res.get("name") == "Круассан миндальный"


@pytest.mark.parametrize(
    "phrase,expect",
    [
        ("калибровку", "Калибровка"),
        ("промывку групп", "Промывка"),
        ("замену картриджа", "Замена картриджа"),
        ("чистку кофемолки", "Чистка кофемолки"),
    ],
)
def test_maintenance_understands_word_endings(phrase, expect):
    """«Сделал калибровку» — это «Калибровка помола» из регламента."""
    conn = db.get_conn()
    task = supply.mark_done(conn, phrase)
    conn.close()
    assert task and task.startswith(expect), f"«{phrase}» не найдено, получено {task}"


def test_maintenance_phrase_works_end_to_end():
    conn = db.get_conn()
    txt = orchestrator.answer(conn, "сделал калибровку")
    conn.close()
    assert txt.startswith("✅"), txt[:80]


def test_negative_price_is_rejected_with_a_clear_message():
    """«цена зерно -500» раньше уходило в раздел заявки, и владелец был уверен,
    что цену задал."""
    conn = db.get_conn()
    txt = orchestrator.answer(conn, "цена зерно -500")
    price = conn.execute("SELECT pack_price FROM ingredients WHERE name='Зерно кофе'").fetchone()
    conn.close()
    assert "больше нуля" in txt
    assert price["pack_price"] > 0


def test_implausible_stock_count_is_rejected():
    """Опечатка в остатке давала «хватит на 71 миллион дней» и глушила заявку."""
    conn = db.get_conn()
    res = supply.record_stock(conn, "зерно", 99999999)
    txt = orchestrator.answer(conn, "остаток зерно 99999999")
    n = conn.execute("SELECT COUNT(*) c FROM stock_counts").fetchone()["c"]
    conn.close()
    assert res and res.get("error") == "implausible"
    assert "проверьте число" in txt.lower()
    assert n == 0


def test_stock_confirmation_names_the_unit():
    conn = db.get_conn()
    txt = orchestrator.answer(conn, "остаток зерно 3")
    conn.close()
    assert "3 кг" in txt, txt[:80]


def test_busy_database_gets_a_human_explanation():
    """Занятая база — это очередь, а не поломка, и сказать надо именно так."""
    import sqlite3

    from coffeeos import bot

    busy = bot._error_text(sqlite3.OperationalError("database is locked"))
    broken = bot._error_text(sqlite3.DatabaseError("file is not a database"))
    other = bot._error_text(ValueError("что угодно"))
    assert "занята" in busy and "ничего не потерялось" in busy
    assert "backups" in broken
    assert "что-то пошло не так" in other


def test_short_writes_from_several_connections_do_not_collide():
    """Бот, автозагрузка и сайт пишут одновременно — это штатный режим."""
    import threading

    errors = []

    def writer(tag):
        c = db.get_conn()
        try:
            for i in range(50):
                c.execute("INSERT INTO kv(key,value) VALUES(?,?)", (f"cc-{tag}-{i}", "x"))
                c.commit()
        except Exception as e:  # noqa: BLE001
            errors.append(f"{type(e).__name__}: {e}")
        finally:
            c.close()

    threads = [threading.Thread(target=writer, args=(t,)) for t in "ABC"]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    conn = db.get_conn()
    n = conn.execute("SELECT COUNT(*) c FROM kv WHERE key LIKE 'cc-%'").fetchone()["c"]
    conn.execute("DELETE FROM kv WHERE key LIKE 'cc-%'")
    conn.commit()
    conn.close()
    assert not errors, errors
    assert n == 150


def test_long_answers_fit_into_a_telegram_message():
    """Ответ длиннее 4096 символов Telegram просто отклонит.

    Проверяем на раздутом каталоге: у реальной кофейни в меню бывает 150+
    позиций, и «каталог» или «цены» легко перерастают лимит.
    """
    conn = db.get_conn()
    for i in range(150):
        name = f"Десерт с довольно длинным названием №{i}"
        conn.execute(
            "INSERT OR IGNORE INTO menu_items(name,category,kind,stocked) "
            "VALUES(?,'Витрина','food',1)",
            (name,),
        )
        conn.execute(
            "INSERT OR IGNORE INTO ingredients"
            "(name,unit,pack_qty,pack_price,pack_name,category,lead_days,"
            "min_packs,price_src) VALUES(?,'pcs',1,0,'шт','case',1,0,'unknown')",
            (name,),
        )
    conn.commit()
    _drop_caches()
    try:
        for label, fn in (
            ("каталог", orchestrator._catalog_text),
            ("цены", orchestrator._prices_text),
            ("заявка", orchestrator.answer_supply),
            ("витрина", orchestrator.answer_case),
            ("маржа", orchestrator.answer_margin),
        ):
            txt = fn(conn)
            assert len(txt) <= 4096, f"«{label}»: {len(txt)} символов — Telegram отклонит"
    finally:
        conn.execute("DELETE FROM menu_items WHERE name LIKE 'Десерт с довольно%'")
        conn.execute("DELETE FROM ingredients WHERE name LIKE 'Десерт с довольно%'")
        conn.commit()
        conn.close()
        _drop_caches()


def test_placeholder_modifier_does_not_create_phantom_items(tmp_path):
    """Касса кладёт в пустую колонку модификаторов «-» или «нет».

    Приклеенные к названию, они плодят мнимые позиции: «Латте 350»,
    «Латте 350 -» и «Латте 350 нет» выглядят как три разных товара.
    """
    from coffeeos import import_receipts as ir

    csv = (
        "Дата и время;Номер чека;Наименование;Модификаторы;Количество;Цена\n"
        "2026-08-01 08:00:00;1;Латте 350;-;1;260\n"
        "2026-08-01 08:05:00;2;Латте 350;нет;1;260\n"
        "2026-08-01 08:10:00;3;Латте 350;;1;260\n"
    )
    ir.import_csv(_write(str(tmp_path), "ph.csv", csv), reset=True)
    conn = db.get_conn()
    names = {r["name"] for r in conn.execute("SELECT name FROM receipt_items")}
    conn.close()
    _reseed()
    assert names == {"Латте 350"}, names


# ======================================================================
# Импорт реальных выгрузок
# ======================================================================
def _write(tmp, name, text, encoding="utf-8"):
    path = os.path.join(tmp, name)
    with open(path, "w", encoding=encoding, newline="") as fh:
        fh.write(text)
    return path


def test_import_poster_style_with_modifiers(tmp_path):
    from coffeeos import import_receipts as ir

    csv = (
        "Дата и время;Номер чека;Наименование;Модификаторы;Количество;Цена;Сумма;"
        "Тип оплаты;Сотрудник;Тип заказа\n"
        "2026-08-01 08:12:00;1001;Латте 350;овсяное молоко;1;320;320;Карта;Аня;С собой\n"
        "2026-08-01 08:12:00;1001;Круассан классический;;1;180;180;Карта;Аня;С собой\n"
        "2026-08-01 09:40:00;1003;Айс американо 450;;1;190;190;Наличные;Аня;В зале\n"
    )
    path = _write(str(tmp_path), "poster.csv", csv)
    res = ir.import_csv(path, reset=True)
    conn = db.get_conn()
    rows = list(
        conn.execute(
            "SELECT i.base,i.kind,i.size,i.milk,i.iced,r.barista,r.channel,r.payment "
            "FROM receipt_items i JOIN receipts r ON r.id=i.receipt_id ORDER BY i.id"
        )
    )
    conn.close()
    assert res["receipts"] == 2 and res["items"] == 3
    assert rows[0]["base"] == "Латте" and rows[0]["milk"] == "овсяное"
    assert rows[0]["barista"] == "Аня" and rows[0]["channel"] == "takeaway"
    assert rows[1]["kind"] == "food"
    assert rows[2]["iced"] == 1 and rows[2]["channel"] == "here"
    assert rows[2]["payment"] == "cash"


def test_import_evotor_style_separate_date_and_time(tmp_path):
    from coffeeos import import_receipts as ir

    csv = (
        "Дата,Время,Наименование,Кол-во,Цена,Сумма,Тип оплаты\n"
        '01.08.2026,08:12,"Латте 0,35 овсяное",1,320,320,БЕЗНАЛИЧНЫМИ\n'
        "01.08.2026,10:05,Раф ванильный 450,1,360,360,БЕЗНАЛИЧНЫМИ\n"
    )
    path = _write(str(tmp_path), "evotor.csv", csv)
    res = ir.import_csv(path, reset=True)
    conn = db.get_conn()
    rows = list(conn.execute("SELECT base,size,milk FROM receipt_items ORDER BY id"))
    conn.close()
    assert res["items"] == 2
    assert rows[0]["base"] == "Латте" and rows[0]["milk"] == "овсяное"
    assert rows[1]["base"] == "Раф" and rows[1]["size"] == "L"


def test_import_dedup_is_safe_to_repeat(tmp_path):
    from coffeeos import import_receipts as ir

    csv = (
        "Дата и время;Номер чека;Наименование;Количество;Цена\n"
        "2026-08-01 08:12:00;1001;Латте 350;1;260\n"
        "2026-08-01 08:15:00;1002;Капучино 250;2;200\n"
    )
    path = _write(str(tmp_path), "d.csv", csv)
    ir.import_csv(path, reset=True)
    second = ir.import_csv(path, reset=False)
    conn = db.get_conn()
    n = conn.execute("SELECT COUNT(*) c FROM receipts").fetchone()["c"]
    conn.close()
    assert n == 2 and second["receipts"] == 0 and second["dupes"] == 2


def test_import_cp1251_and_tabs(tmp_path):
    from coffeeos import import_receipts as ir

    csv = (
        "Дата и время\tНомер чека\tНаименование\tКол-во\tЦена\n"
        "2026-08-01 08:12:00\t1\tЛатте 350\t1\t260\n"
    )
    path = _write(str(tmp_path), "w.csv", csv, encoding="cp1251")
    res = ir.import_csv(path, reset=True)
    assert res["items"] == 1 and res["encoding"] == "cp1251"


def test_import_uses_sum_when_price_is_zero(tmp_path):
    from coffeeos import import_receipts as ir

    csv = (
        "Дата и время;Номер чека;Наименование;Количество;Цена;Сумма\n"
        "2026-08-01 08:12:00;1;Латте 350;2;0;520\n"
    )
    path = _write(str(tmp_path), "z.csv", csv)
    ir.import_csv(path, reset=True)
    conn = db.get_conn()
    row = conn.execute("SELECT qty,price FROM receipt_items").fetchone()
    conn.close()
    assert row["price"] == 260


def test_import_returns_are_subtracted(tmp_path):
    from coffeeos import import_receipts as ir

    csv = (
        "Дата и время;Номер чека;Тип операции;Наименование;Количество;Цена\n"
        "2026-08-01 08:12:00;1;Продажа;Латте 350;2;260\n"
        "2026-08-01 12:00:00;2;Возврат;Латте 350;1;260\n"
    )
    path = _write(str(tmp_path), "r.csv", csv)
    res = ir.import_csv(path, reset=True)
    conn = db.get_conn()
    total = conn.execute("SELECT SUM(qty*price) s FROM receipt_items").fetchone()["s"]
    conn.close()
    assert res["returns"] == 1 and total == 260


def test_import_reports_what_it_cannot_compute(tmp_path):
    from coffeeos import import_receipts as ir

    csv = (
        "Дата;Наименование;Количество;Цена\n"
        "01.08.2026;Латте 350;1;260\n"
        "01.08.2026;Капучино 250;1;200\n"
    )
    path = _write(str(tmp_path), "poor.csv", csv)
    res = ir.import_csv(path, reset=True)
    conn = db.get_conn()
    q = analytics.data_quality(conn)
    conn.close()
    assert res["no_time_column"] and res["no_receipt_column"]
    assert not q["has_time"] and q["warnings"]


def test_import_failure_keeps_history(tmp_path):
    from coffeeos import import_receipts as ir

    conn = db.get_conn()
    before = conn.execute("SELECT COUNT(*) c FROM receipts").fetchone()["c"]
    conn.close()
    path = _write(str(tmp_path), "bad.csv", "какая-то ерунда без заголовков\n")
    with pytest.raises(ir.ImportError_):
        ir.import_csv(path, reset=True)
    conn = db.get_conn()
    after = conn.execute("SELECT COUNT(*) c FROM receipts").fetchone()["c"]
    conn.close()
    assert after == before, "неудачный импорт не должен стирать историю"


def test_manual_waste_survives_first_real_import(tmp_path):
    from coffeeos import import_receipts as ir

    conn = db.get_conn()
    catalog.add_waste(conn, "сырники", 3)
    conn.close()
    csv = (
        "Дата и время;Номер чека;Наименование;Количество;Цена\n"
        "2026-08-01 08:12:00;1;Латте 350;1;260\n"
    )
    ir.import_csv(_write(str(tmp_path), "real.csv", csv), reset=False)
    conn = db.get_conn()
    n = conn.execute("SELECT COUNT(*) c FROM waste WHERE src='user'").fetchone()["c"]
    demo = conn.execute("SELECT COUNT(*) c FROM waste WHERE src='demo'").fetchone()["c"]
    conn.close()
    assert n >= 1, "ручной ввод персонала — реальные данные, их нельзя терять"
    assert demo == 0, "демо-списания должны быть стёрты первой реальной выгрузкой"


def test_rescan_reparses_history_after_catalog_fix(tmp_path):
    """Владелец поправил вид позиции — исправление должно дойти до истории."""
    conn = db.get_conn()
    _wipe(conn)
    for i in range(20):
        _receipt(conn, dt.datetime(2026, 7, 1, 9, i), [("Комбо дня", 1, 300)])
    conn.commit()
    assert conn.execute("SELECT kind FROM receipt_items LIMIT 1").fetchone()["kind"] == "goods"
    conn.execute("UPDATE menu_items SET kind='food', stocked=1 WHERE name='Комбо дня'")
    conn.commit()
    res = catalog.rescan(conn)
    kinds = {r["kind"] for r in conn.execute("SELECT kind FROM receipt_items")}
    conn.close()
    assert res["rows"] == 20 and kinds == {"food"}


# ======================================================================
# Доступ, здоровье, резервные копии, конфигурация
# ======================================================================
def test_bot_is_closed_when_allowlist_empty():
    """Пустой список допущенных не должен делать выручку публичной."""
    from coffeeos import bot

    old = (config.BRIEF_CHAT_IDS, config.STAFF_CHAT_IDS)
    try:
        config.BRIEF_CHAT_IDS, config.STAFF_CHAT_IDS = [], []

        class _Chat:
            id = 12345

        class _Upd:
            effective_chat = _Chat()

        assert bot.is_allowed(_Upd()) is False
    finally:
        config.BRIEF_CHAT_IDS, config.STAFF_CHAT_IDS = old


def test_supplier_has_no_analytics_access():
    from coffeeos import bot

    old = (config.BRIEF_CHAT_IDS, config.STAFF_CHAT_IDS, config.SUPPLIER_CHAT_ID)
    try:
        config.BRIEF_CHAT_IDS = ["111"]
        config.STAFF_CHAT_IDS = ["222"]
        config.SUPPLIER_CHAT_ID = "999"
        assert "999" not in bot.allowed_ids()
    finally:
        config.BRIEF_CHAT_IDS, config.STAFF_CHAT_IDS, config.SUPPLIER_CHAT_ID = old


def test_health_reports_freshness():
    conn = db.get_conn()
    s = health.data_status(conn)
    conn.close()
    assert s["total"] > 0 and s["last_ts"]
    assert s["demo"] is True


def test_alerts_are_not_repeated_every_check():
    conn = db.get_conn()
    conn.execute("DELETE FROM kv WHERE key='alert_last'")
    conn.execute("DELETE FROM receipt_items")
    conn.execute("DELETE FROM receipts")
    conn.commit()
    first = health.alert_if_broken(conn)
    second = health.alert_if_broken(conn)
    conn.close()
    assert first and second is None, "одну и ту же беду не повторяем каждые 6 часов"


def test_future_dated_receipt_does_not_break_reports():
    conn = db.get_conn()
    _receipt(conn, dt.datetime(2031, 1, 1, 10, 0), [("Латте 350", 1, 260)])
    conn.commit()
    _drop_caches()
    last = analytics.last_day_with_data(conn)
    n = analytics.future_dated_count(conn)
    conn.close()
    assert last.year < 2031, "чек из будущего не должен становиться «последним днём»"
    assert n >= 1


def test_backup_creates_and_rotates(tmp_path):
    from coffeeos import backup

    old = config.BACKUP_DIR
    try:
        config.BACKUP_DIR = str(tmp_path)
        for _ in range(4):
            res = backup.make_backup(keep=2)
            assert res["ok"]
        rows = backup.list_backups()
        assert len(rows) == 2, "ротация должна оставлять ровно keep копий"
        assert rows[0]["size_mb"] > 0
    finally:
        config.BACKUP_DIR = old


def test_db_uses_wal():
    conn = db.get_conn()
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert mode.lower() == "wal"


def test_migrations_do_not_swallow_errors():
    import sqlite3

    conn = db.get_conn()
    with pytest.raises(sqlite3.OperationalError):
        db._add_column(conn, "no_such_table", "x", "TEXT")
    conn.close()


def test_config_survives_bad_values(monkeypatch):
    monkeypatch.setenv("BACKUP_KEEP", "не число")
    assert config._int_env("BACKUP_KEEP", 14) == 14
    monkeypatch.setenv("TARGET_FOODCOST", "30 %")
    assert config._share_env("TARGET_FOODCOST", 0.3) == 0.3


def test_timezone_is_single_for_whole_project():
    assert config.today() == config.now().date()


# ======================================================================
# Честность продукта
# ======================================================================
def _project_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _all_sources():
    root = _project_root()
    src = ""
    for fn in os.listdir(os.path.join(root, "coffeeos")):
        if fn.endswith(".py"):
            src += open(os.path.join(root, "coffeeos", fn), encoding="utf-8").read()
    return src


def test_env_example_keys_are_all_read_by_code():
    """Настройка, расписанная в .env.example, но нигде не читаемая, — обман."""
    root = _project_root()
    src = _all_sources()
    env_example = open(os.path.join(root, ".env.example"), encoding="utf-8").read()
    for line in env_example.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=")[0].strip()
        assert key in src, f"{key} расписан в .env.example, но нигде не читается"


def test_dashboard_has_no_hardcoded_numbers():
    """В дашборде не должно быть выдуманных сотрудников, сумм и статусов."""
    page = open(os.path.join(_project_root(), "live.html"), encoding="utf-8").read()
    for ghost in ("Марат", "Эвотор подключено", "вторым продавцом с 16:00", "1 240 чеков"):
        assert ghost not in page
    assert "/api/summary" in page, "дашборд обязан брать данные с сервера"


def test_scenarios_always_carry_their_assumption():
    """Любая оценочная цифра обязана нести рядом своё допущение."""
    conn = db.get_conn()
    sp = staffing.shift_plan(conn)
    at = analytics.attach_rate(conn)
    conn.close()
    for sc in (sp.get("scenario"), at.get("scenario")):
        if sc:
            assert sc.get("assumption"), "сценарий без допущения читается как факт"


def test_no_bakery_leftovers_in_product_text():
    """Продукт пережил пивот целиком: пекарных формулировок в интерфейсе нет."""
    root = _project_root()
    facing = [
        open(os.path.join(root, f), encoding="utf-8").read() for f in ("live.html", "README.md")
    ]
    # Старое имя не должно остаться нигде, включая код.
    for t in facing + [_all_sources()]:
        assert "ПекарьОС" not in t
    # А вот пекарные термины в тексте, который видит владелец, — недопустимы.
    # В коде упоминание старой таблицы допустимо: миграция обязана объяснить,
    # откуда она переносит данные.
    for t in facing:
        low = t.lower()
        for ghost in ("план выпечки", "печём", "продавец", "пекарьос"):
            assert ghost not in low, f"в интерфейсе осталось «{ghost}»"
    # На дашборде пекарни нет вообще — там только цифры кофейни.
    # (В README слово уместно: там объясняется, почему в кофейне
    # себестоимость считается, а в пекарне — нет.)
    assert "пекарн" not in facing[0].lower()


def test_readme_promises_match_the_code():
    root = _project_root()
    docs = open(os.path.join(root, "README.md"), encoding="utf-8").read().lower()
    src = _all_sources().lower()
    for phrase, needle in (
        ("attach-rate", "attach_rate"),
        ("фудкост", "foodcost"),
        ("заявка поставщику", "order_draft"),
        ("витрин", "case_order"),
    ):
        if phrase in docs:
            assert needle in src, f"README обещает «{phrase}», а кода нет"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
