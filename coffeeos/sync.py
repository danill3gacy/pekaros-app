"""Автозагрузка чеков — чтобы каждый чек попадал в систему сам, без ручных команд.

Два режима (задаются в .env через SYNC_MODE):
  • folder — система сама подхватывает CSV-файлы из папки SYNC_FOLDER.
             Успешно загруженные уезжают в `_imported`, битые — в `_failed`
             (чтобы не блокировать очередь и не грузиться заново по кругу).
  • http   — система сама забирает чеки по API кассы/ОФД (SYNC_URL + SYNC_TOKEN).

Дубли исключаются на уровне импорта: чек опознаётся по (дата + номер чека), а
без номера — по содержимому. Повторная загрузка тех же данных безопасна.
"""
import os
import glob
import shutil
import json
import csv
import time
import tempfile
import urllib.request
import urllib.error
import logging
from datetime import datetime
from . import config, db
from .import_receipts import import_csv, ImportError_

log = logging.getLogger("coffeeos.sync")

STABLE_AGE_SEC = 5          # файл считаем дописанным, если он не менялся столько секунд


def _move(src, dst_dir):
    """Перенести файл, не затирая одноимённые (добавляем метку времени)."""
    os.makedirs(dst_dir, exist_ok=True)
    base = os.path.basename(src)
    dst = os.path.join(dst_dir, base)
    if os.path.exists(dst):
        stamp = config.now().strftime("%Y%m%d-%H%M%S")
        root, ext = os.path.splitext(base)
        dst = os.path.join(dst_dir, f"{root}__{stamp}{ext}")
    shutil.move(src, dst)
    return dst


def sync_folder(folder):
    """Импортировать все новые CSV из папки."""
    if not folder:
        return {"files": 0, "items": 0, "dupes": 0, "note": "SYNC_FOLDER не задан"}
    if not os.path.isdir(folder):
        log.warning("Папка автозагрузки не найдена: %s", folder)
        return {"files": 0, "items": 0, "dupes": 0, "note": f"папка не найдена: {folder}"}

    imported_dir = os.path.join(folder, "_imported")
    failed_dir = os.path.join(folder, "_failed")
    # регистронезависимые ФС (macOS) отдают файл дважды — убираем повторы
    files = sorted({os.path.realpath(p) for p in
                    glob.glob(os.path.join(folder, "*.csv")) + glob.glob(os.path.join(folder, "*.CSV"))})

    processed = total = dupes = failed = 0
    for f in files:
        try:
            # файл могут ещё дописывать — пропускаем до следующего цикла
            if time.time() - os.path.getmtime(f) < STABLE_AGE_SEC:
                log.info("Файл %s ещё пишется — отложен до следующего цикла", os.path.basename(f))
                continue
            res = import_csv(f, reset=False)
            total += res["items"]
            dupes += res.get("dupes", 0)
            processed += 1
            # Уточнённые чеки (updated) — это тоже успешная загрузка: файл с
            # одними исправлениями не должен уезжать в карантин.
            if not res["items"] and not res.get("dupes") and not res.get("updated"):
                # ничего не распознано — в карантин, чтобы можно было разобраться
                dst = _move(f, failed_dir)
                failed += 1
                log.warning("Из файла %s не распознано ни одной позиции "
                            "(битых строк: %s, непонятных дат: %s). Перенесён в _failed.",
                            os.path.basename(f), res.get("skipped", 0), res.get("bad_date", 0))
            else:
                _move(f, imported_dir)
                log.info("Загружен %s: +%s позиций, уточнено чеков %s, %s дублей пропущено",
                         os.path.basename(f), res["items"], res.get("updated", 0),
                         res.get("dupes", 0))
        except ImportError_ as e:
            failed += 1
            try:
                _move(f, failed_dir)
            except Exception:
                pass
            log.warning("Файл %s не разобран (%s). Перенесён в _failed.", os.path.basename(f), e)
        except BaseException as e:                    # noqa: BLE001 — ловим и SystemExit
            failed += 1
            try:
                _move(f, failed_dir)
            except Exception:
                pass
            log.warning("Ошибка загрузки %s: %r. Перенесён в _failed.", os.path.basename(f), e)
    return {"files": processed, "items": total, "dupes": dupes, "failed": failed}


# Имена полей, которые встречаются в API разных касс/ОФД
FIELD_ALIASES = {
    "ts":      ["ts", "date", "datetime", "dateTime", "date_time", "timestamp", "created_at", "receipt_date"],
    "receipt": ["receipt", "receipt_id", "receiptId", "id", "number", "fiscalDocNumber", "doc_number"],
    "name":    ["name", "item", "product", "itemName", "product_name", "title"],
    "qty":     ["qty", "quantity", "cnt", "count", "amount_qty"],
    "price":   ["price", "unit_price", "pricePerUnit"],
    "sum":     ["sum", "total", "amount", "sum_total"],
    "payment": ["payment", "pay_type", "paymentType", "payment_type"],
}


def _pick(obj, key):
    for alias in FIELD_ALIASES[key]:
        if isinstance(obj, dict) and obj.get(alias) not in (None, ""):
            return obj[alias]
    return ""


def _json_to_csv(rows, path):
    """Разложить JSON-чеки в CSV. Цена и сумма пишутся в РАЗНЫЕ колонки, чтобы
    сумма позиции не была принята за цену за единицу."""
    recognised = 0
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["Дата и время", "Номер чека", "Наименование", "Количество", "Цена", "Сумма", "Тип оплаты"])
        for x in rows:
            name = _pick(x, "name")
            ts = _pick(x, "ts")
            if name and ts:
                recognised += 1
            w.writerow([ts, _pick(x, "receipt"), name, _pick(x, "qty") or 1,
                        _pick(x, "price"), _pick(x, "sum"), _pick(x, "payment")])
    return recognised


def sync_http(url, token):
    """Забрать чеки по API (JSON) и загрузить их с дедупом."""
    if not url:
        return {"items": 0, "dupes": 0, "note": "SYNC_URL не задан"}
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        log.warning("API кассы ответил ошибкой %s (%s)", e.code, url)
        return {"items": 0, "dupes": 0, "error": f"HTTP {e.code}"}
    except Exception as e:
        log.warning("Не удалось обратиться к API кассы: %r", e)
        return {"items": 0, "dupes": 0, "error": "нет связи с API"}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("API вернул не JSON (первые 200 символов): %s", raw[:200])
        return {"items": 0, "dupes": 0, "error": "ответ не JSON"}

    rows = data if isinstance(data, list) else (
        data.get("items") or data.get("receipts") or data.get("data") or data.get("results") or [])
    if not isinstance(rows, list):
        rows = []
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    try:
        recognised = _json_to_csv(rows, path)
        log.info("API вернул %s записей, распознано %s", len(rows), recognised)
        if not recognised:
            if rows:
                log.warning("Поля API не распознаны. Пример записи: %s. "
                            "Добавьте имена полей в FIELD_ALIASES в coffeeos/sync.py", str(rows[0])[:200])
            return {"items": 0, "dupes": 0, "received": len(rows), "error": "поля не распознаны" if rows else None}
        res = import_csv(path, reset=False)
        out = {"items": res["items"], "dupes": res.get("dupes", 0),
               "updated": res.get("updated", 0), "received": len(rows)}
        # Записи пришли и поля распознались, но ни одна строка не легла в базу
        # из-за непонятной даты — это поломка, а не «всё в порядке». Раньше
        # автозагрузка в таком случае молчала, а выручка просто не поступала.
        if (not res["items"] and not res.get("dupes") and not res.get("updated")
                and res.get("bad_date")):
            out["error"] = (f"дата в ответе API не распознана ({res['bad_date']} строк) — "
                            f"проверьте формат поля даты")
            log.warning("Автозагрузка: %s", out["error"])
        return out
    except ImportError_ as e:
        log.warning("Данные API не разобраны: %s", e)
        return {"items": 0, "dupes": 0, "error": str(e)}
    finally:
        if os.path.exists(path):
            os.remove(path)


def run_once():
    """Один цикл автозагрузки по текущему режиму из .env."""
    db.init_db()
    mode = (config.SYNC_MODE or "off").lower()
    if mode == "folder":
        return sync_folder(config.SYNC_FOLDER)
    if mode == "http":
        return sync_http(config.SYNC_URL, config.SYNC_TOKEN)
    return {"items": 0, "note": "автозагрузка выключена (SYNC_MODE=off)"}
