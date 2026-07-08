"""settings_service.py — управление настройками бота через Railway env-переменные."""
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
]


def get_current_value(setting):
    return os.environ.get(setting["key"], setting["default"])


def get_current_label(setting):
    current = get_current_value(setting)
    for opt in setting["options"]:
        if opt["value"] == current:
            return opt["label"]
    return f"(кастом) {current}"


def get_setting_by_index(i):
    if 0 <= i < len(SETTINGS):
        return SETTINGS[i]
    return None


def get_option_by_index(setting, j):
    opts = setting.get("options", [])
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


def log_current_settings():
    lines = ["Текущие настройки бота:"]
    for s in SETTINGS:
        lines.append(f"  {s['key']} = {get_current_value(s)}")
    logger.info("\n".join(lines))
