"""Самоконтроль системы: свежесть данных и состояние.

Бот периодически проверяет, обновляются ли данные, и сам предупреждает
владельца, если что-то не так — чтобы он узнавал о проблеме раньше клиента.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from . import config, db


def _now():
    """Текущее время в часовом поясе кофейни (сервер может стоять в UTC)."""
    try:
        return datetime.now(ZoneInfo(config.TIMEZONE)).replace(tzinfo=None)
    except Exception:
        return datetime.now()


def data_status(conn=None):
    own = conn is None
    conn = conn or db.get_conn()
    try:
        row = conn.execute("SELECT MAX(ts) t, COUNT(*) c FROM receipts").fetchone()
        last_ts, total = row["t"], row["c"]
        today = conn.execute(
            "SELECT COUNT(*) c FROM receipts WHERE substr(ts,1,10)=?",
            (_now().date().isoformat(),)).fetchone()["c"]
        hours = None
        if last_ts:
            try:
                hours = (_now() - datetime.fromisoformat(last_ts)).total_seconds() / 3600
            except ValueError:
                hours = None
        stale = (hours is None) or (hours > config.DATA_STALE_HOURS)
        demo = db.kv_get(conn, "demo_data") == "1"
        from .analytics import future_dated_count
        future = future_dated_count(conn)
        return {"last_ts": last_ts, "total": total, "today": today,
                "hours_since": round(hours, 1) if hours is not None else None,
                "stale": stale, "empty": total == 0, "demo": demo, "future": future}
    finally:
        if own:
            conn.close()


def status_text(conn=None):
    s = data_status(conn)
    if s["empty"]:
        return "⚠️ В базе пока нет данных. Загрузите чеки: python -m coffeeos import выгрузка.csv"
    mark = "🟢" if not s["stale"] else "🔴"
    demo_note = ("\n⚠️ Сейчас в базе ДЕМО-данные. Загрузите выгрузку чеков: "
                 "python -m coffeeos import файл.csv") if s.get("demo") else ""
    last = s["last_ts"][:16].replace("T", " ") if s["last_ts"] else "—"
    lines = [f"{mark} *Состояние системы*",
             f"Всего чеков в базе: {s['total']}",
             f"Последний чек: {last}",
             f"Сегодня чеков: {s['today']}"]
    if s["stale"]:
        lines.append(f"⚠️ Данные не обновлялись более {config.DATA_STALE_HOURS} ч — "
                     f"проверьте выгрузку из кассы.")
    if s.get("future"):
        lines.append(f"⚠️ Чеков с датой из будущего: {s['future']} — проверьте часы на кассе.")
    b = backup_status()
    lines.append(f"💾 Резервные копии: {b['count']} шт" +
                 (f", последняя {b['age_h']} ч назад" if b["age_h"] is not None else "") +
                 ("" if b["ok"] or not b["count"] else " ⚠️ проверьте!"))
    # состояние ИИ (Ollama): владелец должен видеть, отвечает ли модель на
    # свободные вопросы, и если нет — что именно сделать
    lines.append(llm_status_line())
    return "\n".join(lines) + demo_note


def llm_status_line():
    """Одна строка про ИИ для сводки состояния."""
    from . import llm
    if not config.llm_enabled():
        return "🤖 ИИ: выключен (кнопки и частые вопросы работают без него)."
    p = llm.ping()
    if p["ok"]:
        return f"🤖 ИИ: 🟢 {config.LLM_MODEL} отвечает."
    # причина уже человеко-читаемая, но в одну строку — берём первую.
    # Свой значок ставим, только если его нет в самой причине, иначе строка
    # получалась с двумя подряд: «🤖 ИИ: 🔴 🟠 ИИ думает слишком долго…»
    reason = p["reason"].split("\n")[0].strip()
    if reason[:1] in ("🔴", "🟠", "🟢"):
        return f"🤖 ИИ: {reason}"
    return f"🤖 ИИ: 🔴 {reason}"


def backup_status():
    """Свежесть последней резервной копии."""
    try:
        from . import backup
        rows = backup.list_backups()
    except Exception:
        return {"ok": False, "count": 0, "age_h": None}
    if not rows:
        return {"ok": False, "count": 0, "age_h": None}
    try:
        made = datetime.strptime(rows[0]["made"], "%Y-%m-%d %H:%M")
        age = (datetime.now() - made).total_seconds() / 3600
    except Exception:
        age = None
    # копия считается свежей, если моложе полутора суток и не пустая
    ok = (rows[0]["size_mb"] > 0) and (age is None or age < 36)
    return {"ok": ok, "count": len(rows), "age_h": round(age, 1) if age is not None else None}


ALERT_REPEAT_HOURS = 24       # одну и ту же беду не повторяем чаще раза в сутки


def _current_problem(conn=None):
    """Что сейчас не так — (код, текст) либо None."""
    s = data_status(conn)
    if s["empty"]:
        return ("empty",
                "🔴 КофейняОС: в базе нет данных — сводки не формируются. Нужно загрузить чеки.")
    if s["stale"]:
        h = s["hours_since"]
        return ("stale",
                f"🔴 КофейняОС: данные не обновлялись "
                f"{('более ' + str(config.DATA_STALE_HOURS) + ' ч') if h is None else (str(int(h)) + ' ч')}. "
                f"Похоже, выгрузка из кассы не пришла — проверьте, пожалуйста.")
    if s.get("future"):
        return ("future",
                f"🟠 КофейняОС: в базе {s['future']} чеков с датой из будущего — "
                f"похоже, на кассе сбиты часы. Проверьте дату на кассе и перезагрузите выгрузку.")
    b = backup_status()
    if b["count"] and not b["ok"]:
        return ("backup",
                "🟠 КофейняОС: резервные копии базы давно не обновлялись или пусты. "
                "Проверьте место на диске — иначе историю продаж будет не восстановить.")
    return None


def alert_if_broken(conn=None):
    """Текст предупреждения, если что-то не так и об этом ещё не сообщали.

    Проверка идёт каждые 6 часов, а беда живёт днями. Без дедупликации владелец
    получал одно и то же сообщение четыре раза в сутки и переставал их читать —
    это опаснее, чем не прислать вовсе. Повторяем не чаще раза в сутки, а когда
    всё починилось — сообщаем об этом один раз.
    """
    own = conn is None
    conn = conn or db.get_conn()
    try:
        problem = _current_problem(conn)
        last = (db.kv_get(conn, "alert_last") or "").split("|")
        last_code, last_at = (last + ["", ""])[:2]
        now = config.now()
        if problem is None:
            if last_code:
                db.kv_set(conn, "alert_last", "")
                return "🟢 КофейняОС: всё в порядке, данные снова приходят."
            return None
        code, text = problem
        if code == last_code and last_at:
            try:
                hours = (now - datetime.fromisoformat(last_at)).total_seconds() / 3600
                if hours < ALERT_REPEAT_HOURS:
                    return None                      # уже сообщали, молчим
            except ValueError:
                pass
        db.kv_set(conn, "alert_last", f"{code}|{now.isoformat()}")
        return text
    finally:
        if own:
            conn.close()
