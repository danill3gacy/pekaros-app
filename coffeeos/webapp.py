"""Веб-слой: JSON-API на живых данных из БД + живой дашборд кофейни.

Запуск:  uvicorn coffeeos.webapp:app --host 0.0.0.0 --port 8000
"""

import base64
import binascii
import os
import secrets
from datetime import timedelta

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse

from . import (
    analytics,
    catalog,
    config,
    costing,
    db,
    demand,
    health,
    orchestrator,
    staffing,
    supply,
)
from .analytics import last_day_with_data

app = FastAPI(title="КофейняОС API", docs_url=None, redoc_url=None, openapi_url=None)


def _unauthorized():
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Нужен логин и пароль",
        headers={"WWW-Authenticate": 'Basic charset="UTF-8"'},
    )


def auth(request: Request):
    """Если в .env задан WEB_PASSWORD — сайт закрыт логином и паролем.
    Заголовок разбираем сами и декодируем как UTF-8: стандартный разбор в
    библиотеке принимает только латиницу, из-за чего русский пароль не работал."""
    if not config.WEB_PASSWORD:
        return True  # пароль не задан — доступ открыт
    header = request.headers.get("Authorization", "")
    scheme, _, param = header.partition(" ")
    if scheme.lower() != "basic" or not param:
        raise _unauthorized()
    try:
        raw = base64.b64decode(param)
    except (ValueError, binascii.Error):
        raise _unauthorized()
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        decoded = raw.decode("latin-1")  # некоторые браузеры шлют так
    user, _, password = decoded.partition(":")

    def eq(a, b):  # сравнение байтами: пароль может быть кириллицей
        return secrets.compare_digest(str(a).encode("utf-8"), str(b).encode("utf-8"))

    if not (eq(user, config.WEB_USER) and eq(password, config.WEB_PASSWORD)):
        raise _unauthorized()
    return True


DASHBOARD_FILE = os.path.join(os.path.dirname(__file__), "..", "live.html")

db.init_db()  # гарантируем, что таблицы есть, даже если запущен только веб

if not config.WEB_PASSWORD:
    import logging

    logging.getLogger("coffeeos.web").warning(
        "Сайт запущен БЕЗ пароля — данные кофейни увидит любой, кто знает адрес. "
        "Задайте WEB_PASSWORD в .env для боевого сервера."
    )


@app.get("/api/health")
def api_health(_=Depends(auth)):
    return JSONResponse(health.data_status())


@app.get("/api/ask")
def api_ask(q: str = "", _=Depends(auth)):
    if not q.strip():
        return JSONResponse({"answer": "Задайте вопрос."})
    conn = db.get_conn()
    try:
        # в вебе разметку Telegram показываем как обычный текст;
        # allow_write=False — GET-запрос не должен ничего писать в базу
        ans = orchestrator.plain(orchestrator.answer(conn, q, allow_write=False))
        return JSONResponse({"answer": ans})
    finally:
        conn.close()


@app.get("/api/summary")
def api_summary(_=Depends(auth)):
    conn = db.get_conn()
    try:
        last = last_day_with_data(conn)
        s = analytics.sales_summary(conn, last)
        prev = analytics.sales_summary(conn, last - timedelta(days=7))
        delta = ((s["revenue"] - prev["revenue"]) / prev["revenue"] * 100) if prev["revenue"] else 0
        data = {
            "venue": config.VENUE_NAME,
            "date": s["date"],
            "revenue": round(s["revenue"]),
            "checks": s["checks"],
            "avg_check": round(s["avg"]),
            "revenue_delta_pct": round(delta, 1),
            "cups": round(s["cups"]),
            "cash": round(s["cash"]),
            "card": round(s["card"]),
            "top": analytics.top_positions(conn, last, 6),
            "by_category": analytics.revenue_by_category(conn, last),
            "hourly": analytics.hourly(conn, last),
            "week": analytics.week_trend(conn, last),
            "waste": catalog.waste_report(conn, last),
            "milk_waste": catalog.milk_waste(conn),
            # деньги кофейни: маржа каждой чашки — то, чего нет ни в кассе, ни в ОФД
            "economics": costing.totals(conn),
            "menu_matrix": costing.menu_matrix(conn),
            "high_foodcost": costing.high_foodcost(conn),
            "milk": costing.milk_economics(conn),
            # витрина: конечный запас, ради него и считается упущенная выручка
            "case_order": demand.case_order(conn),
            "lost_today": demand.sellouts_for_day(conn, last),
            "lost": demand.lost_sales_report(conn),
            "forecast": demand.forecast_checks(conn),
            # руки: где кофейня упирается в потолок
            "shift": staffing.shift_plan(conn),
            "load": staffing.load_profile(conn),
            # напитки и еда к ним
            "attach": analytics.attach_rate(conn),
            "basket": analytics.basket(conn),
            "drinks": analytics.drink_mix(conn, n=8),
            "sizes": analytics.size_mix(conn),
            "iced": analytics.iced_share(conn),
            # закупка
            "supply": supply.reorder(conn),
            "maintenance": supply.maintenance_due(conn),
            # что система честно НЕ может посчитать на этих данных
            "data_quality": analytics.data_quality(conn),
        }
        return JSONResponse(data)
    finally:
        conn.close()


@app.post("/api/case/adjust")
async def api_case_adjust(request: Request, _=Depends(auth)):
    """Владелец поправил количество в витрине — сохраняем решение в базу."""
    body = await request.json()
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="не указана позиция")
    try:
        qty = int(round(float(body.get("qty"))))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="количество должно быть числом")
    if not 0 <= qty <= 10000:
        raise HTTPException(status_code=400, detail="неправдоподобное количество")
    conn = db.get_conn()
    try:
        day = body.get("date") or demand.case_order(conn)["target"]
        res = demand.set_order_override(conn, _as_date(day), name, qty)
        return JSONResponse({"ok": True, "name": res["name"], "recommended": res["qty"]})
    finally:
        conn.close()


@app.post("/api/case/approve")
async def api_case_approve(request: Request, _=Depends(auth)):
    """Утвердить заказ витрины и отправить его смене в Telegram."""
    conn = db.get_conn()
    try:
        plan = orchestrator.approve_case_order(conn)
    finally:
        conn.close()
    sent = _send_to_staff(plan["text"])
    return JSONResponse(
        {
            "ok": True,
            "target": plan["target"],
            "approved_at": config.now().strftime("%H:%M"),
            "sent_to": sent,
        }
    )


@app.post("/api/order/send")
async def api_order_send(request: Request, _=Depends(auth)):
    """Отправить заявку поставщику (или вернуть текст, если получатель не задан)."""
    conn = db.get_conn()
    try:
        draft = supply.order_draft(conn)
    finally:
        conn.close()
    if not draft["text"]:
        return JSONResponse({"ok": False, "reason": "заказывать нечего"})
    text = f"📦 Заявка от «{config.VENUE_NAME}»:\n\n{draft['text']}"
    sent = _send_telegram(config.SUPPLIER_CHAT_ID, text) if config.SUPPLIER_CHAT_ID else 0
    return JSONResponse({"ok": True, "sent": sent, "text": draft["text"]})


def _as_date(value):
    from datetime import date as _d

    try:
        y, m, dd = map(int, str(value)[:10].split("-"))
        return _d(y, m, dd)
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="неверная дата")


def _send_telegram(chat_id, text):
    if not (config.BOT_TOKEN and chat_id):
        return 0
    import json as _json
    import logging
    import urllib.request

    url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage"
    payload = _json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10):
            return 1
    except Exception as e:  # noqa: BLE001
        logging.getLogger("coffeeos.web").warning("send to %s failed: %s", chat_id, e)
        return 0


def _send_to_staff(text):
    """Отправить сообщение смене в Telegram. Возвращает, скольким дошло."""
    if not (config.BOT_TOKEN and config.STAFF_CHAT_IDS):
        return 0
    import json as _json
    import logging
    import urllib.request

    sent = 0
    url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage"
    for cid in config.STAFF_CHAT_IDS:
        payload = _json.dumps({"chat_id": cid, "text": text, "parse_mode": "Markdown"}).encode(
            "utf-8"
        )
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=10):
                sent += 1
        except Exception as e:  # noqa: BLE001
            logging.getLogger("coffeeos.web").warning("send to %s failed: %s", cid, e)
    return sent


@app.get("/", response_class=HTMLResponse)
def live(_=Depends(auth)):
    try:
        with open(DASHBOARD_FILE, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return (
            "<h1>КофейняОС</h1><p>Файл дашборда live.html не найден рядом с проектом. "
            "Данные доступны в <code>/api/summary</code>.</p>"
        )
