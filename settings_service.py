"""settings_service.py — управление настройками бота.

Настройки бывают двух видов:
  storage="env" (по умолчанию) — env-переменная на Railway; смена вызывает
      передеплой сервиса, значение подхватывается после рестарта;
  storage="db"  — значение живёт в SQLite бота (таблица settings) и
      применяется сразу, без передеплоя. Так сделано для того, что меняют
      часто и на ходу — например, вариант бланка счёта.
"""
import json
import logging
import os
import urllib.request

logger = logging.getLogger(__name__)

SETTINGS: list = [
    {
        "key":     "CLAUDE_MODEL",
        "label":   "🤖 Модель Claude",
        "default": "claude-haiku-4-5-20251001",
        "options": [
            {"label": "Haiku 4.5 · быстро, дёшево",       "value": "claude-haiku-4-5-20251001"},
            {"label": "Sonnet 5 · баланс качества/цены",  "value": "claude-sonnet-5"},
            {"label": "Opus 4.8 · максимум качества",     "value": "claude-opus-4-8"},
        ],
    },
    {
        "key":     "AGENT_VERSION",
        "label":   "🧠 Версия агента",
        "default": "v1",
        "options": [
            {"label": "v1 · один вызов LLM",             "value": "v1"},
            {"label": "v2 · multi-turn tool-use",        "value": "v2"},
        ],
    },
    {
        "key":     "SKIP_PDF",
        "label":   "📄 Генерация PDF",
        "default": "0",
        "options": [
            {"label": "Включена (генерировать PDF)",     "value": "0"},
            {"label": "Отключена (только DOCX)",         "value": "1"},
        ],
    },
    {
        "key":     "SKIP_DRIVE",
        "label":   "📁 Загрузка на Google Drive",
        "default": "0",
        "options": [
            {"label": "Включена (загружать в Drive)",    "value": "0"},
            {"label": "Отключена (только локально)",     "value": "1"},
        ],
    },
    {
        "key":     "BACKUP_KEEP_DAYS",
        "label":   "💾 Хранить бэкапы, дней",
        "default": "30",
        "options": [
            {"label": "7 дней",   "value": "7"},
            {"label": "30 дней",  "value": "30"},
            {"label": "60 дней",  "value": "60"},
            {"label": "90 дней",  "value": "90"},
        ],
    },
    {
        "key":      "INVOICE_TEMPLATE",
        "label":    "🧾 Бланк счёта",
        "default":  "v1",
        "storage":  "db",          # применяется сразу, без передеплоя
        "options_fn": lambda: _invoice_template_options(),
    },
]


def _invoice_template_options() -> list:
    """Варианты бланка счёта = комплекты шаблонов, лежащие в templates/.
    Список собирается на лету, поэтому добавленный в папку invoice_template_v3
    появляется в меню сам, без правки кода."""
    try:
        import doc_builder
        variants = doc_builder.invoice_variants()
    except Exception as e:  # pragma: no cover
        logger.warning(f"settings: не удалось прочитать варианты счёта: {e}")
        return [{"label": "v1 · основной бланк", "value": "v1"}]

    opts = []
    for v in variants:
        if v == "v1":
            opts.append({"label": "v1 · основной бланк", "value": v})
        else:
            opts.append({"label": f"{v} · invoice_template_{v}.xlsx", "value": v})
    return opts


def get_options(setting) -> list:
    """Варианты настройки: статический список или собранный функцией."""
    fn = setting.get("options_fn")
    if fn:
        try:
            return fn()
        except Exception as e:  # pragma: no cover
            logger.warning(f"settings: options_fn для {setting['key']} упал: {e}")
            return []
    return setting.get("options", [])


def get_current_value(setting):
    if setting.get("storage") == "db":
        try:
            import memory
            val = memory.get_setting(setting["key"])
            if val:
                return val
        except Exception as e:  # pragma: no cover
            logger.warning(f"settings: не прочитать {setting['key']} из БД: {e}")
    return os.environ.get(setting["key"], setting["default"])


def get_current_label(setting):
    current = get_current_value(setting)
    for opt in get_options(setting):
        if opt["value"] == current:
            return opt["label"]
    return f"(кастом) {current}"


def get_setting_by_index(i):
    if 0 <= i < len(SETTINGS):
        return SETTINGS[i]
    return None


def get_option_by_index(setting, j):
    opts = get_options(setting)
    if 0 <= j < len(opts):
        return opts[j]
    return None


def set_railway_variable(name, value):
    """Обновляет env-переменную через Railway GraphQL API.
    Возвращает (ok: bool, error_message: str)."""
    railway_token       = os.environ.get("RAILWAY_API_TOKEN", "")
    railway_service_id  = os.environ.get("RAILWAY_SERVICE_ID", "")
    railway_env_id      = os.environ.get("RAILWAY_ENVIRONMENT_ID", "")

    if not railway_token or not railway_service_id or not railway_env_id:
        msg = "не заданы RAILWAY_API_TOKEN / RAILWAY_SERVICE_ID / RAILWAY_ENVIRONMENT_ID"
        logger.warning(f"settings: {msg}")
        return False, msg

    query = """
    mutation UpsertVariables($input: ServiceVariablesInput!) {
      serviceVariablesUpsert(input: $input)
    }
    """
    variables = {
        "input": {
            "serviceId":     railway_service_id,
            "environmentId": railway_env_id,
            "variables":     {name: value},
        }
    }
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(
        "https://backboard.railway.app/graphql/v2",
        data=payload,
        headers={
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {railway_token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
    except Exception as e:
        logger.warning(f"settings: Railway API request failed: {e}")
        return False, f"Railway API: {e}"

    if "errors" in result:
        errs = result["errors"]
        logger.warning(f"settings: Railway API вернул ошибки: {errs}")
        return False, f"Railway API: {errs}"

    os.environ[name] = value
    logger.info(f"settings: переменная {name}={value} сохранена в Railway")
    return True, ""


def apply_setting(setting, value):
    """Сохраняет значение настройки. Возвращает (ok, error, needs_restart).

    Для storage="db" пишем в SQLite и сразу зеркалим в os.environ, чтобы
    текущий процесс подхватил значение без перезапуска."""
    if setting.get("storage") == "db":
        try:
            import memory
            memory.set_setting(setting["key"], value)
            os.environ[setting["key"]] = value
            logger.info(f"settings: {setting['key']}={value} сохранено в БД")
            return True, "", False
        except Exception as e:
            logger.warning(f"settings: не сохранить {setting['key']} в БД: {e}")
            return False, str(e), False

    ok, err = set_railway_variable(setting["key"], value)
    return ok, err, ok


def log_current_settings():
    lines = ["Текущие настройки бота:"]
    for s in SETTINGS:
        lines.append(f"  {s['key']} = {get_current_value(s)}")
    logger.info("\n".join(lines))
