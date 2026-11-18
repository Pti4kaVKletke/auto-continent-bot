"""
error_text.py — понятные пользователю расшифровки ошибок вместо общего
«Произошла ошибка». Чистая функция, без Telegram: explain_error(err) → текст.
"""


def is_file_too_big(err) -> bool:
    return "file is too big" in str(err).lower()


def explain_error(err) -> str:
    raw = str(err or "")
    low = raw.lower()
    name = type(err).__name__

    if is_file_too_big(err):
        return ("Файл больше 20 МБ — Telegram не даёт боту скачать такой файл. "
                "Сожмите PDF (для сканов хватает 150 dpi, обычно 1–3 МБ) или положите "
                "файл прямо в папку «Сканы» сделки на Google Drive.")
    if "can't parse entities" in low or "can't find end of the entity" in low:
        return ("Telegram не смог отобразить ответ из-за спецсимволов в тексте "
                "(_ * [ `). Данные при этом не потеряны — повторите действие или откройте сделку заново.")
    if "message is too long" in low:
        return "Ответ получился слишком длинным для одного сообщения Telegram. Попробуйте сузить запрос (период, статус)."
    if "bot was blocked" in low or "forbidden" in low:
        return "Telegram запретил боту писать в этот чат (бот заблокирован или удалён из чата)."
    if "query is too old" in low or "query id is invalid" in low:
        return "Кнопка устарела. Откройте меню заново и нажмите её ещё раз."
    if name in ("TimedOut", "ReadTimeout", "ConnectTimeout") or "timed out" in low:
        return "Истекло время ожидания ответа от Telegram или Google. Повторите действие через минуту."
    if name in ("NetworkError", "ConnectError") or "connection" in low:
        return "Проблема со связью с Telegram/Google. Повторите действие через минуту."
    if "429" in raw or "rate limit" in low or "quota" in low or "ratelimit" in low:
        return "Превышен лимит запросов (Google или Claude). Подождите минуту и повторите."
    if "credit balance" in low or "billing" in low:
        return "На аккаунте Anthropic закончились средства или не оплачен тариф — пополните баланс в консоли Anthropic."
    if "overloaded" in low or "529" in raw:
        return "Сервис Claude сейчас перегружен. Повторите через минуту."
    if "invalid x-api-key" in low or "authentication" in low or "401" in raw:
        return "Ключ доступа отклонён (Claude или Google). Проверьте переменные окружения в Railway."
    if "httperror 403" in low or "insufficient permission" in low or "forbidden" in low:
        return "Нет прав доступа к файлу или таблице Google. Проверьте, что сервисному аккаунту открыт доступ."
    if "httperror 404" in low or "file not found" in low or "not found" in low:
        return "Файл или папка на Google Drive не найдены (возможно, удалены или перенесены)."
    if "httperror 5" in low or "backend error" in low:
        return "Сбой на стороне Google. Повторите через минуту."
    if "database is locked" in low:
        return "База бота занята. Повторите действие через несколько секунд."
    return "Неизвестная ошибка. Повторите действие; если повторится — пришлите это сообщение разработчику."


def user_message(err) -> str:
    """Текст для чата: расшифровка + короткая техническая строка."""
    tech = str(err or "").strip().replace("\n", " ")
    if len(tech) > 160:
        tech = tech[:160] + "…"
    text = "⚠️ " + explain_error(err)
    if tech:
        text += f"\n\nТехнически: {type(err).__name__}: {tech}"
    return text
