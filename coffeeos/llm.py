"""Единая точка работы с языковой моделью (по умолчанию — Ollama qwen2.5:3b).

Задача модуля — чтобы свободный вопрос владельца НАДЁЖНО доходил до модели, а
если что-то не так (Ollama не запущена, модель не скачана, долго думает) —
бот сказал об этом человеческим языком, а не отмолчался generic-подсказкой.

Работает с любым OpenAI-совместимым сервером: Ollama (локально, бесплатно),
Groq/OpenRouter, облако OpenAI. Никаких платных ключей для Ollama не нужно.
"""

import json
import logging
import urllib.error
import urllib.request

from . import config

log = logging.getLogger("coffeeos.llm")


class LLMUnavailable(Exception):
    """ИИ настроен, но не смог ответить. В user_message — понятная владельцу причина."""

    def __init__(self, user_message):
        super().__init__(user_message)
        self.user_message = user_message


def enabled():
    return config.llm_enabled()


# ---------- диагностика причины сбоя ----------
def _hint(err):
    """Перевести техническую ошибку в понятную владельцу подсказку."""
    s = str(err).lower()
    model = config.LLM_MODEL or "qwen2.5:3b"
    local = "localhost" in (config.LLM_BASE_URL or "") or "127.0.0.1" in (config.LLM_BASE_URL or "")
    if any(
        w in s
        for w in (
            "connection refused",
            "refused",
            "max retries",
            "failed to establish",
            "connection error",
            "newconnectionerror",
            "[errno 61]",
            "[errno 111]",
            "actively refused",
            "cannot connect",
            "connecterror",
        )
    ):
        if local:
            return (
                "🔴 ИИ не отвечает: похоже, Ollama не запущена.\n"
                "Запустите в Терминале: ollama serve\n"
                f"и один раз скачайте модель: ollama pull {model}"
            )
        return "🔴 ИИ не отвечает: нет связи с сервером модели. Проверьте LLM_BASE_URL в .env."
    if any(
        w in s for w in ("rate limit", "429", "too many requests", "quota", "insufficient_quota")
    ):
        return (
            "🟠 Сервер модели ограничил частоту запросов. Подождите минуту и "
            "повторите — или переключитесь на локальную Ollama (см. README)."
        )
    # ВАЖНО: не ловим просто слово «model» — оно встречается в любых сообщениях
    # («rate limit for model gpt-4o-mini»), и владельцу советовали ollama pull
    # вместо настоящей причины
    if any(
        w in s
        for w in (
            "not found",
            "404",
            "no such model",
            "model not found",
            "does not exist",
            "unknown model",
        )
    ):
        return f"🔴 Модель «{model}» не установлена.\nСкачайте её один раз: ollama pull {model}"
    if any(w in s for w in ("timed out", "timeout", "read timed out")):
        return (
            "🟠 ИИ думает слишком долго. Для qwen2.5:3b на слабом железе это бывает — "
            "повторите вопрос или возьмите модель полегче (qwen2.5:1.5b)."
        )
    if any(w in s for w in ("unauthorized", "401", "api key", "invalid_api_key", "403")):
        return "🔴 ИИ отклонил ключ доступа. Проверьте LLM_API_KEY в .env (для Ollama подойдёт любое слово)."
    if "openai" in s and ("no module" in s or "cannot import" in s):
        return "🔴 Не установлена библиотека openai. Выполните: pip install -r requirements.txt"
    return f"🟠 ИИ временно недоступен ({err}). Кнопки и частые вопросы работают без него."


# ---------- проверка связи ----------
def ping(timeout=4):
    """Быстрая проверка: доступен ли сервер модели и есть ли нужная модель.

    Возвращает {ok, reason, models}. Сеть не трогает, если ИИ не настроен.
    """
    if not config.llm_enabled():
        return {
            "ok": False,
            "configured": False,
            "reason": "ИИ выключен — кнопки и частые вопросы работают без него.",
        }
    base = (config.LLM_BASE_URL or "https://api.openai.com/v1").rstrip("/")
    url = base + "/models"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {config.LLM_API_KEY or 'local'}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        models = [m.get("id") for m in (data.get("data") or []) if isinstance(m, dict)]
        want = config.LLM_MODEL
        # Сравниваем ТОЧНО. Проверка «по префиксу до двоеточия» считала готовой
        # соседнюю версию: при установленной qwen2.5:1.5b статус показывал
        # «qwen2.5:3b готова», а каждый вопрос падал с «модель не установлена».
        if want and models and want not in models:
            near = [m for m in models if m.split(":")[0] == want.split(":")[0]]
            hint = f" На сервере есть: {', '.join(near)}." if near else ""
            return {
                "ok": False,
                "configured": True,
                "models": models,
                "reason": f"Модель «{want}» не найдена.{hint} Скачайте: ollama pull {want}",
            }
        return {
            "ok": True,
            "configured": True,
            "models": models,
            "reason": f"{want} готова"
            + (f" (моделей на сервере: {len(models)})" if models else ""),
        }
    except urllib.error.HTTPError as e:
        return {"ok": False, "configured": True, "reason": _hint(f"http {e.code}")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "configured": True, "reason": _hint(e)}


# ---------- собственно генерация ----------
def complete(system, user, timeout=None, max_tokens=500, temperature=0.3):
    """Спросить модель. Возвращает текст ответа.

    На любой сбой бросает LLMUnavailable с понятным владельцу текстом — чтобы
    вызывающий показал этот текст, а не отмолчался.
    """
    timeout = timeout or config.LLM_TIMEOUT
    try:
        from openai import OpenAI
    except Exception as e:  # библиотека не установлена
        raise LLMUnavailable(_hint(f"openai import: {e}"))
    try:
        client = OpenAI(
            api_key=config.LLM_API_KEY or "local",
            base_url=config.LLM_BASE_URL or None,
            max_retries=0,
            timeout=timeout,
        )
        r = client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return (r.choices[0].message.content or "").strip()
    except Exception as e:  # noqa: BLE001
        log.warning("LLM error: %s", e)
        raise LLMUnavailable(_hint(e))
