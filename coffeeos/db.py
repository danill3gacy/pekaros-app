"""Слой базы данных (SQLite — ноль настройки, идеально для одной точки).
Для сети точек / масштаба легко заменить на PostgreSQL.

Схема кофейни отличается от пекарной в трёх местах, и все три существенны:

1. **Позиция чека разбирается при загрузке.** «Латте 400 мл на овсяном + сироп
   карамель» — это не уникальный товар, это Латте размера L на овсяном молоке
   с сиропом. Без разбора каталог кофейни рассыпается на сотни имён, и ни
   расход зерна, ни маржа, ни attach-rate не считаются. Разобранные атрибуты
   лежат прямо в receipt_items: разбор делается один раз при импорте, запросы
   остаются простыми, а пересобрать его можно командой `rescan`.

2. **Есть рецептура и ингредиенты.** В пекарне себестоимость считать
   невозможно: между мукой и багетом стоят тесто, расстойка и рука пекаря.
   В кофейне наоборот — латте это ровно 18 г зерна, 200 мл молока, стакан
   и крышка. Это позволяет считать
   маржу каждой чашки и точный расход на закупку.

3. **Товар делится на «делается на заказ» и «стоит на витрине».** Напиток не
   может кончиться, круассан — может. Только второе участвует в заказе витрины
   и в поиске упущенной выручки.
"""
import logging
import sqlite3

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS venues (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS receipts (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,           -- ISO datetime чека
    payment  TEXT DEFAULT 'card',     -- card|cash
    ext_id   TEXT,                    -- внешний id чека (защита от повторной загрузки)
    venue_id INTEGER NOT NULL DEFAULT 1,
    barista  TEXT,                    -- сотрудник, если касса его отдаёт
    guest    TEXT,                    -- карта лояльности / телефон, если касса их отдаёт
    channel  TEXT                     -- takeaway|here, если касса это различает
);
CREATE TABLE IF NOT EXISTS receipt_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id INTEGER NOT NULL,
    name       TEXT NOT NULL,         -- как в чеке, без изменений
    base       TEXT,                  -- нормализованное имя позиции меню («Латте»)
    category   TEXT,
    kind       TEXT,                  -- drink|food|goods|service
    size       TEXT,                  -- S|M|L
    volume_ml  INTEGER,               -- объём напитка, если распознан
    milk       TEXT,                  -- обычное|овсяное|миндальное|кокосовое|безлактозное
    mods       TEXT,                  -- модификаторы через «;» (сироп, доп. шот, декаф)
    iced       INTEGER DEFAULT 0,     -- 1 = холодный напиток
    qty        REAL NOT NULL,
    price      REAL NOT NULL,         -- цена за единицу
    FOREIGN KEY(receipt_id) REFERENCES receipts(id)
);
CREATE TABLE IF NOT EXISTS waste (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    date   TEXT NOT NULL,
    name   TEXT NOT NULL,
    qty    REAL NOT NULL,
    unit   TEXT NOT NULL DEFAULT 'шт',  -- шт | л | кг
    amount REAL NOT NULL DEFAULT 0,     -- деньги: витрина по ценнику, сырьё по себестоимости
    kind   TEXT NOT NULL DEFAULT 'food',-- food|milk|drink|ingredient
    src    TEXT NOT NULL DEFAULT 'user' -- demo | user (ручной ввод не теряем)
);
CREATE TABLE IF NOT EXISTS menu_items (
    name     TEXT PRIMARY KEY,        -- базовое имя позиции меню
    category TEXT,
    kind     TEXT NOT NULL DEFAULT 'food',   -- drink|food|goods|service
    stocked  INTEGER NOT NULL DEFAULT 0      -- 1 = стоит на витрине (конечный запас)
);
CREATE TABLE IF NOT EXISTS ingredients (
    name        TEXT PRIMARY KEY,
    unit        TEXT NOT NULL,            -- g | ml | pcs
    pack_qty    REAL NOT NULL DEFAULT 1,  -- единиц в упаковке (1000 г в пачке зерна)
    pack_price  REAL NOT NULL DEFAULT 0,  -- цена упаковки, ₽
    pack_name   TEXT DEFAULT 'уп.',       -- как называть упаковку в заявке
    category    TEXT DEFAULT 'other',     -- coffee|dairy|syrup|packaging|other
    shelf_days  REAL,                     -- срок годности после вскрытия (молоко ~3)
    lead_days   REAL NOT NULL DEFAULT 2,  -- через сколько дней приезжает поставка
    min_packs   REAL NOT NULL DEFAULT 0,  -- страховой запас, упаковок
    price_src   TEXT NOT NULL DEFAULT 'default'   -- default | owner
);
CREATE TABLE IF NOT EXISTS recipes (
    item       TEXT NOT NULL,          -- базовое имя позиции меню
    size       TEXT NOT NULL DEFAULT '*',  -- S|M|L либо '*' (любой размер)
    ingredient TEXT NOT NULL,
    qty        REAL NOT NULL,          -- в единицах ингредиента на одну порцию
    src        TEXT NOT NULL DEFAULT 'default',  -- default | owner
    PRIMARY KEY (item, size, ingredient)
);
CREATE TABLE IF NOT EXISTS stock_counts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,          -- когда посчитали
    ingredient TEXT NOT NULL,
    qty        REAL NOT NULL           -- остаток в единицах ингредиента
);
CREATE TABLE IF NOT EXISTS maintenance (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task        TEXT NOT NULL UNIQUE,
    period_days REAL NOT NULL,
    last_done   TEXT,
    note        TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS case_order_override (
    date TEXT NOT NULL,          -- на какой день заказ витрины
    name TEXT NOT NULL,          -- позиция
    qty  REAL NOT NULL,          -- сколько ставить по решению владельца
    PRIMARY KEY (date, name)
);
"""

# Индексы создаются ОТДЕЛЬНО и ПОСЛЕ миграций.
# На базе, обновляемой с пекарной версии, таблица receipt_items уже
# существует без колонки `base`: CREATE TABLE IF NOT EXISTS её не добавит,
# а CREATE INDEX ... ON receipt_items(base) в том же скрипте упадёт с
# «no such column» — и обновление ломает рабочую базу целиком.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_items_receipt ON receipt_items(receipt_id);
CREATE INDEX IF NOT EXISTS idx_items_name ON receipt_items(name);
CREATE INDEX IF NOT EXISTS idx_items_base ON receipt_items(base);
CREATE INDEX IF NOT EXISTS idx_items_kind ON receipt_items(kind);
CREATE INDEX IF NOT EXISTS idx_receipt_ts ON receipts(ts);
CREATE INDEX IF NOT EXISTS idx_waste_date ON waste(date);
CREATE INDEX IF NOT EXISTS idx_stock_ing ON stock_counts(ingredient, ts);
"""


def get_conn():
    # timeout + WAL: бот, автозагрузка и веб работают с базой одновременно
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        conn.execute("PRAGMA journal_mode=WAL")   # читатели не блокируются писателем
    except sqlite3.Error as e:
        # на сетевых дисках WAL недоступен — это не повод падать, но и молчать
        # об этом не надо: без WAL бот и сайт будут блокировать друг друга
        logging.getLogger("coffeeos.db").warning(
            "Режим WAL недоступен (%s) — при одновременной работе бота и сайта "
            "возможны задержки на записи.", e)
    return conn


def _columns(conn, table):
    """Список колонок таблицы."""
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _add_column(conn, table, column, definition):
    """Добавить колонку, если её ещё нет.

    Проверяем схему явно, а не ловим исключение от ALTER TABLE: `except: pass`
    глушил и настоящие ошибки (заблокированная база, повреждённый файл), и
    миграция молча «проходила», оставляя базу в нерабочем виде.
    """
    if column in _columns(conn, table):
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    return True


def init_db(conn=None, seed_reference=True):
    own = conn is None
    conn = conn or get_conn()
    had_tables = _tables(conn)
    conn.executescript(SCHEMA)
    _migrate(conn, had_tables)          # колонки — до индексов по ним
    conn.executescript(INDEXES)
    conn.execute("INSERT OR IGNORE INTO venues(id,name) VALUES(1,?)", (config.VENUE_NAME,))
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_receipt_extid "
                 "ON receipts(ext_id) WHERE ext_id IS NOT NULL")
    conn.commit()
    if seed_reference:
        # справочники (типовые ингредиенты, рецепты, регламент обслуживания)
        # наполняются один раз и дальше правятся владельцем
        from . import reference
        reference.ensure_loaded(conn)
    if own:
        conn.close()


def _migrate(conn, had_tables):
    """Миграции: старые пекарные базы и промежуточные версии кофейни.

    База с историей продаж — это единственное, чего нельзя потерять при
    обновлении. Поэтому апгрейд с пекарной версии не пересоздаёт таблицы, а
    достраивает недостающие колонки и переносит каталог.
    """
    # чеки: точка, сотрудник, гость, канал продажи
    _add_column(conn, "receipts", "ext_id", "TEXT")
    _add_column(conn, "receipts", "venue_id", "INTEGER NOT NULL DEFAULT 1")
    _add_column(conn, "receipts", "barista", "TEXT")
    _add_column(conn, "receipts", "guest", "TEXT")
    _add_column(conn, "receipts", "channel", "TEXT")
    # позиции: разбор напитка
    for col, definition in (("base", "TEXT"), ("kind", "TEXT"), ("size", "TEXT"),
                            ("volume_ml", "INTEGER"), ("milk", "TEXT"),
                            ("mods", "TEXT"), ("iced", "INTEGER DEFAULT 0")):
        _add_column(conn, "receipt_items", col, definition)
    # списания: единица измерения и вид
    _add_column(conn, "waste", "src", "TEXT NOT NULL DEFAULT 'user'")
    _add_column(conn, "waste", "unit", "TEXT NOT NULL DEFAULT 'шт'")
    _add_column(conn, "waste", "kind", "TEXT NOT NULL DEFAULT 'food'")

    # каталог: product_meta (кофейня) -> menu_items (кофейня)
    if "product_meta" in had_tables and "menu_items" in _tables(conn):
        moved = conn.execute("SELECT COUNT(*) c FROM menu_items").fetchone()["c"]
        if not moved:
            # produced=1 в кофейне означало «печём сами» — в кофейне ближайший
            # смысл это «стоит на витрине с конечным запасом»
            conn.execute(
                "INSERT OR IGNORE INTO menu_items(name,category,kind,stocked) "
                "SELECT name, category, 'food', COALESCE(produced,0) FROM product_meta")
    # план выпечки -> заказ витрины
    if "plan_override" in had_tables:
        conn.execute("INSERT OR IGNORE INTO case_order_override(date,name,qty) "
                     "SELECT date,name,qty FROM plan_override")


def kv_set(conn, key, value):
    conn.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, str(value)))
    conn.commit()


def kv_get(conn, key, default=None):
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default
