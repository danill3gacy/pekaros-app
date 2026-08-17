"""Конфигурация: читается из переменных окружения / .env файла."""

import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def now() -> datetime:
    """Текущее время В ЧАСОВОМ ПОЯСЕ КОФЕЙНИ.

    Единая точка на весь проект. Раньше бот жил по TIMEZONE, а аналитика,
    списания и резервные копии — по времени сервера: на VPS в UTC «сегодня»
    у бота и у отчётов расходилось на три часа, и вечерние чеки попадали в
    следующий день.
    """
    try:
        return datetime.now(ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow")))
    except Exception:
        return datetime.now()


def today() -> date:
    """Сегодняшняя дата в часовом поясе кофейни."""
    return now().date()


# --- Telegram ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
# Кому слать утреннюю сводку (chat_id через запятую). Узнать свой id: напишите боту /start
BRIEF_CHAT_IDS = [c.strip() for c in os.getenv("BRIEF_CHAT_IDS", "").split(",") if c.strip()]
# Кому слать напоминание продавцу отметить списания (по умолчанию — те же)
STAFF_CHAT_IDS = [
    c.strip() for c in os.getenv("STAFF_CHAT_IDS", "").split(",") if c.strip()
] or list(BRIEF_CHAT_IDS)
# Сводка приходит до открытия: утренний поток «на работу» начинается в
# 7:30–8:00, и решение по витрине принимается ещё раньше.
BRIEF_TIME = os.getenv("BRIEF_TIME", "06:30")  # утренняя сводка, HH:MM
CLOSE_TIME = os.getenv("CLOSE_TIME", "21:00")  # вечернее напоминание, HH:MM
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")

# --- Заведение ---
VENUE_NAME = os.getenv("VENUE_NAME", "Кофейня")
OWNER_NAME = os.getenv("OWNER_NAME", "Саша")

# Telegram chat_id поставщика (по желанию). Если задан — заявка уходит ему прямо в Telegram.
# Если пусто — бот оформит заявку, а владелец перешлёт её поставщику (WhatsApp и т.п.).
SUPPLIER_CHAT_ID = os.getenv("SUPPLIER_CHAT_ID", "").strip()

# --- Данные ---
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "..", "coffeeos.db"))
LOG_PATH = os.getenv("LOG_PATH", os.path.join(os.path.dirname(__file__), "..", "coffeeos.log"))


# --- Автозагрузка чеков ---
# off    — выключено (грузите вручную: python -m coffeeos import файл.csv)
# folder — система сама подхватывает CSV из папки SYNC_FOLDER каждые SYNC_INTERVAL_MIN минут
# http   — система сама забирает чеки по API кассы/ОФД (SYNC_URL + SYNC_TOKEN)
def _int_env(name: str, default: int | str) -> int:
    """Число из .env; пустое, кривое значение или комментарий в строке не роняют запуск."""
    try:
        return int(str(os.getenv(name, default)).split("#")[0].strip())
    except (TypeError, ValueError):
        return int(default)


SYNC_MODE = os.getenv("SYNC_MODE", "off").strip().lower()
SYNC_INTERVAL_MIN = _int_env("SYNC_INTERVAL_MIN", 60)
SYNC_FOLDER = os.getenv("SYNC_FOLDER", "").strip()
SYNC_URL = os.getenv("SYNC_URL", "").strip()
SYNC_TOKEN = os.getenv("SYNC_TOKEN", "").strip()

# --- Резервные копии базы ---
BACKUP_DIR = (
    os.getenv("BACKUP_DIR", "").split("#")[0].strip()
)  # пусто = папка backups рядом с базой
BACKUP_KEEP = _int_env("BACKUP_KEEP", 14)  # сколько копий хранить
BACKUP_HOUR = _int_env("BACKUP_HOUR", 4)  # во сколько делать копию (ночью)

# --- Доступ к веб-дашборду ---
# Если задать пароль, сайт спросит логин/пароль (логин по умолчанию — owner).
# Пусто = без пароля (удобно для локальной проверки, но не для боевого сервера).
WEB_USER = os.getenv("WEB_USER", "owner").strip() or "owner"
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "").strip()

# --- Самоконтроль ---
# Если данные не обновлялись дольше этого числа часов — бот предупреждает владельца
DATA_STALE_HOURS = _int_env("DATA_STALE_HOURS", 28)


# --- Экономика ---
# В отличие от пекарни, в кофейне себестоимость порции считается точно:
# латте — это 18 г зерна, 200 мл молока, стакан и крышка. Поэтому продукт
# считает маржу каждой позиции. Цены сырья живут в базе (таблица ingredients)
# и правятся из бота, а не в .env: их несколько десятков и они меняются.
def _share_env(name: str, default: float | None = None) -> float | None:
    """Доля 0..1 из .env. Принимает и «30», и «0.3» — владельцу удобнее проценты."""
    raw = str(os.getenv(name, "")).split("#")[0].strip().replace("%", "").replace(",", ".")
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        return default
    if v > 1:
        v = v / 100.0
    return min(0.95, max(0.0, v))


# Насколько раньше обычного должна закончиться продажа позиции, чтобы система
# заподозрила, что товар РАСПРОДАН (а не что спрос кончился). Вероятностный
# порог: (1 − доля вечернего трафика) ^ (число продаж) < этого значения.
SELLOUT_P = _share_env("SELLOUT_P", 0.05) or 0.05

# Целевой уровень сервиса ВИТРИНЫ: какую долю спроса на еду стремимся закрыть.
# Напитков не касается — они делаются на заказ и кончиться не могут.
CASE_SERVICE_LEVEL = _share_env("CASE_SERVICE_LEVEL", 0.70) or 0.70

# Ориентир по foodcost для подсветки позиций. Выше — позиция подозрительная:
# либо цена низкая, либо рецепт «поплыл». Это ориентир, а не норматив.
TARGET_FOODCOST = _share_env("TARGET_FOODCOST", 0.30) or 0.30


# На сколько дней вперёд считать заявку поставщику (обычный цикл закупки).
def _int_share(name: str, default: int) -> int:
    return max(1, _int_env(name, default))


ORDER_HORIZON_DAYS = _int_share("ORDER_HORIZON_DAYS", 7)

# --- LLM (опционально: отвечает на нестандартные свободные вопросы) ---
# Совместим с любым OpenAI-совместимым API. Варианты:
#  • Локальная БЕСПЛАТНАЯ модель через Ollama (данные не покидают сервер, 152-ФЗ):
#       LLM_BASE_URL=http://localhost:11434/v1  LLM_MODEL=qwen2.5:3b  LLM_API_KEY=ollama
#  • Бесплатные тарифы Groq/OpenRouter (OpenAI-совместимые) — свой base_url и ключ.
#  • OpenAI (по умолчанию, если задан только ключ).
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "").strip()  # пусто = облако OpenAI
LLM_MODEL = os.getenv("LLM_MODEL", "").strip() or OPENAI_MODEL
LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip() or OPENAI_API_KEY
# Сколько ждать ответа модели. Для локальной qwen2.5:3b на ноутбуке ответ может
# идти 10–40 секунд, поэтому по умолчанию щедро.
LLM_TIMEOUT = _int_env("LLM_TIMEOUT", 60)


def llm_enabled() -> bool:
    """ИИ доступен, если задан ключ (облако) или базовый URL (локальная/своя модель)."""
    return bool(LLM_BASE_URL or LLM_API_KEY)


def brief_hour_minute() -> tuple[int, int]:
    h, m = BRIEF_TIME.split(":")
    return int(h), int(m)


def close_hour_minute() -> tuple[int, int]:
    h, m = CLOSE_TIME.split(":")
    return int(h), int(m)


def validate() -> list[str]:
    """Проверка настроек перед запуском бота. Возвращает список проблем."""
    problems = []
    if not BOT_TOKEN:
        problems.append("Не задан BOT_TOKEN (получите у @BotFather).")
    if not BRIEF_CHAT_IDS:
        problems.append(
            "Не задан BRIEF_CHAT_IDS — утренняя сводка никому не уйдёт, "
            "и бот НИКОГО не пустит к данным кофейни. Запустите бота, "
            "напишите ему /start, впишите показанный chat_id."
        )
    if BOT_TOKEN and not (BRIEF_CHAT_IDS or STAFF_CHAT_IDS):
        problems.append(
            "Токен бота задан, а список допущенных пуст: пока это так, "
            "бот отвечает только на /start и данные закрыты."
        )
    if not WEB_PASSWORD:
        problems.append(
            "Не задан WEB_PASSWORD — веб-дашборд открыт без пароля, "
            "выручку кофейни увидит любой, кто знает адрес сервера."
        )
    for cid in BRIEF_CHAT_IDS + STAFF_CHAT_IDS:
        if not cid.lstrip("-").isdigit():
            problems.append(f"chat_id «{cid}» не похож на число.")
    try:
        for h, m in (brief_hour_minute(), close_hour_minute()):
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError
    except Exception:
        problems.append(
            "BRIEF_TIME/CLOSE_TIME должны быть в формате ЧЧ:ММ (часы 0–23, минуты 0–59)."
        )
    # автозагрузка
    if SYNC_MODE not in ("off", "", "folder", "http"):
        problems.append(f"SYNC_MODE='{SYNC_MODE}' — допустимо off, folder или http.")
    if SYNC_MODE == "folder":
        if not SYNC_FOLDER:
            problems.append("SYNC_MODE=folder, но не задан SYNC_FOLDER (папка с выгрузками).")
        elif not os.path.isabs(SYNC_FOLDER):
            problems.append(f"SYNC_FOLDER='{SYNC_FOLDER}' — укажите полный путь (от корня).")
        elif not os.path.isdir(SYNC_FOLDER):
            problems.append(f"Папка SYNC_FOLDER не найдена: {SYNC_FOLDER}")
    if SYNC_MODE == "http" and not SYNC_URL:
        problems.append("SYNC_MODE=http, но не задан SYNC_URL (адрес API кассы/ОФД).")
    if DB_PATH and not os.path.isabs(DB_PATH):
        problems.append(
            f"DB_PATH='{DB_PATH}' задан относительным путём — бот и сайт "
            f"могут открыть разные базы. Укажите полный путь."
        )
    return problems
