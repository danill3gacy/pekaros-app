"""Резервные копии базы.

Вся история продаж кофейни лежит в одном файле базы. Если сервер умрёт или файл
повредится — восстановить будет неоткуда. Поэтому бот раз в сутки делает копию
и хранит последние N штук.

Копия снимается штатным механизмом SQLite (не простым копированием файла),
поэтому безопасна даже если в этот момент идёт запись.
"""
import glob
import logging
import os
import sqlite3
from datetime import datetime

from . import config

log = logging.getLogger("coffeeos.backup")


def backup_dir():
    return config.BACKUP_DIR or os.path.join(os.path.dirname(config.DB_PATH), "backups")


def _made_at(path):
    """Ключ сортировки копий: время файла, а при совпадении — имя.

    Имя включает миллисекунды, поэтому копии, снятые в одну секунду, всё равно
    выстраиваются в верном порядке, и ротация не путает старую копию со свежей.
    """
    try:
        return (os.path.getmtime(path), os.path.basename(path))
    except OSError:
        return (0.0, os.path.basename(path))


def make_backup(keep=None):
    """Сделать копию базы и удалить самые старые сверх лимита."""
    keep = keep or config.BACKUP_KEEP
    src = config.DB_PATH
    if not os.path.exists(src):
        return {"ok": False, "note": "база ещё не создана"}
    d = backup_dir()
    os.makedirs(d, exist_ok=True)
    # миллисекунды в имени: две копии в одну секунду не затирают друг друга и
    # при этом сохраняют хронологический порядок в списке
    stamp = config.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    dst = os.path.join(d, f"coffeeos-{stamp}.db")
    n = 1
    while os.path.exists(dst):
        dst = os.path.join(d, f"coffeeos-{stamp}-{n:02d}.db")
        n += 1
    source = target = None
    try:
        source = sqlite3.connect(src)
        target = sqlite3.connect(dst)
        with target:
            source.backup(target)          # согласованный снимок (корректно и в режиме WAL)
    except Exception as e:
        log.warning("Резервная копия не создана: %r", e)
        for c in (target, source):
            try:
                c and c.close()
            except Exception:
                pass
        if os.path.exists(dst):            # недоделанный файл не должен занимать слот ротации
            try:
                os.remove(dst)
            except Exception:
                pass
        return {"ok": False, "error": repr(e)}
    finally:
        for c in (target, source):
            try:
                c and c.close()
            except Exception:
                pass
    # Ротация: оставляем только последние keep копий.
    # Сортировка по ВРЕМЕНИ файла, а не по имени: у копий, сделанных в одну
    # секунду, имя получает суффикс «-1», а по алфавиту «…-1.db» идёт РАНЬШЕ
    # «….db» — свежая копия попадала под удаление, и следом падал getsize.
    size_mb = os.path.getsize(dst) / 1024 / 1024
    files = sorted(glob.glob(os.path.join(d, "coffeeos-*.db")), key=_made_at)
    removed = 0
    keep = max(1, int(keep or 1))          # 0 не должен означать «хранить бесконечно»
    for old in files[:-keep]:
        if os.path.abspath(old) == os.path.abspath(dst):
            continue                       # только что созданную копию не трогаем никогда
        try:
            os.remove(old)
            removed += 1
        except Exception:
            pass
    log.info("Резервная копия: %s (%.1f МБ), удалено старых: %s", os.path.basename(dst), size_mb, removed)
    return {"ok": True, "file": dst, "size_mb": round(size_mb, 1),
            "total": len(files) - removed, "removed": removed}


def list_backups():
    d = backup_dir()
    if not os.path.isdir(d):
        return []
    out = []
    for f in sorted(glob.glob(os.path.join(d, "coffeeos-*.db")), key=_made_at, reverse=True):
        try:
            size = os.path.getsize(f)
        except OSError:                     # копию удалили прямо сейчас
            continue
        out.append({"file": os.path.basename(f),
                    "size_mb": round(size / 1024 / 1024, 1),
                    "made": datetime.fromtimestamp(os.path.getmtime(f)).strftime("%Y-%m-%d %H:%M")})
    return out
