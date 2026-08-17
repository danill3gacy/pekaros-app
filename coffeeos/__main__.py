"""Единая точка входа.

    python -m coffeeos seed          # заполнить демо-данными (90 дней)
    python -m coffeeos brief         # напечатать утреннюю сводку (проверка)
    python -m coffeeos vitrina       # заказ витрины на завтра
    python -m coffeeos marzha        # маржа, фудкост и разбор меню
    python -m coffeeos zakupki       # заявка поставщику: расход и что заказать
    python -m coffeeos smena         # загрузка смены и второй бариста
    python -m coffeeos upusheno      # сколько недопродали (упущенная выручка)
    python -m coffeeos nedelya       # итоги недели
    python -m coffeeos rescan        # пересобрать разбор позиций (после правок каталога)
    python -m coffeeos status        # состояние системы и свежесть данных
    python -m coffeeos llm           # проверить связь с ИИ (Ollama) и задать пробный вопрос
    python -m coffeeos ask "вопрос"  # задать вопрос оркестратору из консоли
    python -m coffeeos web           # запустить веб-дашборд (порт 8000)
    python -m coffeeos bot           # запустить Telegram-бота
    python -m coffeeos import f.csv  # загрузить реальные чеки из выгрузки
    python -m coffeeos sync          # один цикл автозагрузки (по SYNC_MODE в .env)
    python -m coffeeos backup        # сделать резервную копию базы сейчас
    python -m coffeeos backups       # список резервных копий
"""
import sys


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    # Схема нужна любой команде, читающей базу. Без этого первый же запуск на
    # чистой машине падал с «no such table: receipts» вместо понятного ответа
    # «нет данных о продажах».
    if cmd not in ("help", "llm", "ai", "ollama"):
        from . import db as _db
        _db.init_db()
    if cmd == "seed":
        from . import seed
        print("Сгенерировано чеков:", seed.seed())
    elif cmd == "brief":
        from . import db, orchestrator
        conn = db.get_conn()
        print(orchestrator.plain(orchestrator.morning_brief(conn)))
        conn.close()
    elif cmd in ("status", "health"):
        from . import health
        print(health.status_text())
    elif cmd in ("llm", "ai", "ollama"):
        # быстрая проверка связи с моделью: «ИИ работает или нет и что делать»
        from . import llm, config
        p = llm.ping()
        print(f"Сервер модели: {config.LLM_BASE_URL or 'облако OpenAI'}")
        print(f"Модель:        {config.LLM_MODEL}")
        print(("🟢 " if p["ok"] else "🔴 ") + p["reason"])
        if p.get("models"):
            print("Доступные модели на сервере:", ", ".join(p["models"][:12]))
        if p["ok"]:
            from . import db, orchestrator
            conn = db.get_conn()
            print("\nПробный вопрос — «одним словом, как идут дела?»:")
            print(orchestrator.plain(orchestrator.answer(conn, "одним словом, как идут дела?")))
            conn.close()
    elif cmd in ("nedelya", "vitrina", "marzha", "zakupki", "smena", "upusheno",
                 "eda", "napitki", "moloko"):
        from . import db, orchestrator
        fn = {"nedelya": orchestrator.weekly_brief,
              "vitrina": orchestrator.answer_case,
              "marzha": orchestrator.answer_margin,
              "zakupki": orchestrator.answer_supply,
              "smena": orchestrator.answer_shift,
              "eda": orchestrator.answer_attach,
              "napitki": orchestrator.answer_drinks,
              "moloko": orchestrator.answer_milk,
              "upusheno": orchestrator.answer_lost}[cmd]
        conn = db.get_conn()
        print(orchestrator.plain(fn(conn)))
        conn.close()
    elif cmd == "rescan":
        from . import catalog, db
        conn = db.get_conn()
        res = catalog.rescan(conn)
        conn.close()
        print(f"Пересобран разбор: уникальных названий {res['names']}, "
              f"обновлено строк {res['rows']}")
    elif cmd == "ask":
        from . import db, orchestrator
        conn = db.get_conn()
        print(orchestrator.plain(orchestrator.answer(conn, " ".join(sys.argv[2:]))))
        conn.close()
    elif cmd == "web":
        import uvicorn
        uvicorn.run("coffeeos.webapp:app", host="0.0.0.0", port=8000)
    elif cmd == "bot":
        from . import bot
        bot.main()
    elif cmd == "import":
        from . import import_receipts
        res = import_receipts.import_csv(sys.argv[2], reset="--reset" in sys.argv)
        print(f"Загружено чеков: {res['receipts']}, позиций: {res['items']}, "
              f"пропущено дублей: {res.get('dupes', 0)}, битых строк: {res.get('skipped', 0)}")
        if res.get('updated'):
            print(f"  ✎ уточнено ранее загруженных чеков: {res['updated']}")
        if res.get('bad_date'):
            print(f"  ⚠ строк с непонятной датой: {res['bad_date']} — проверьте формат колонки даты")
        if res.get('returns'):
            print(f"  ↩ возвратов учтено (вычтены из выручки): {res['returns']}")
        if res.get('no_payment_column'):
            print("  ⚠ в выгрузке нет колонки типа оплаты — всё зачтено как безнал, "
                  "сверка кассы будет неточной")
        # Раньше эти два признака вычислялись и никуда не выводились: владелец
        # не знал, почему «пик продаж 0:00» и «упущенная выручка 0 ₽».
        if res.get('no_time_column'):
            print("  ⚠ в выгрузке нет ВРЕМЕНИ продажи, только дата.\n"
                  "     Не будут считаться: утренний пик, загрузка смены, attach-rate\n"
                  "     по часам и распроданность витрины.\n"
                  "     Выгрузите чеки с колонкой времени — без них не видно ни утреннего\n"
                  "     пика, ни пустой витрины.")
        if res.get('no_receipt_column'):
            print("  ⚠ в выгрузке нет номера чека — состав чеков не восстановить.\n"
                  "     Число чеков и средний чек будут неточными.")
        if res.get('service_items'):
            print(f"  ℹ служебных позиций (пакеты, скидки) исключено из расчётов: {res['service_items']}")
    elif cmd == "backup":
        from . import backup
        print("Резервная копия:", backup.make_backup())
    elif cmd == "backups":
        from . import backup
        rows = backup.list_backups()
        if not rows:
            print("Копий пока нет. Сделать: python -m coffeeos backup")
        for r in rows:
            print(f"  {r['made']}  {r['file']}  {r['size_mb']} МБ")
    elif cmd == "sync":
        from . import sync
        res = sync.run_once()
        print("Автозагрузка:", res)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
