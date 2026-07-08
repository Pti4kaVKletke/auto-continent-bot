"""
settings_service.py — управление настройками бота через Railway env-переменные.

Идея: в Telegram-меню Александры есть раздел «⚙️ Настройки», где кнопками
переключаются рабочие параметры (модель Claude, версия агента, флаги
SKIP_PDF/SKIP_DRIVE и т.п.). Каждое изменение отправляется в Railway
GraphQL API через serviceVariablesUpsert — Railway автоматически передеплоит
сервис, и бот перезапустится уже с новыми значениями.

Данные-первичны: список настроек SETTINGS описывает всё, что доступно в меню.
Чтобы добавить новую настройку — просто дописать словарь в SETTINGS.

Требуемые Railway env-переменные (уже есть в проекте):
  RAILWAY_API_TOKEN       — токен Railway API
  RAILWAY_SERVICE_ID      — ID сервиса
  RAILWAY_ENVIRONMENT_ID  — ID окружения (production)
"""

import json
import logging
import os
import urllib.request

logger = logging.getLogger(__name__)

# ─── РЕЕСТР НАСТРОЕК ─────────────────────────────────────────────────────────
# Каждая настройка: {
#   "key":     имя env-переменной
#   "label":   что показать в меню
#   "default": значение когда env-переменная не задана
#   "options": [{"label": показать, "value": реальное значение}, ...]
# }

SETTINGS: list[dict] = [
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


# ─── ЧТЕНИЕ ТЕКУЩИХ ЗНАЧЕНИЙ ─────────────────────────────────────────────────

def get_current_value(setting: dict) -> str:
    """Текущее значение env-переменной с фолбэком на default."""
    return os.environ.get(setting["key"], setting["default"])


def get_current_label(setting: dict) -> str:
    """Человекочитаемая метка текущей опции.
    Если env-переменная имеет значение, которого нет в options — вернём
    само значение (чтобы пользователь увидел, что там кастомное)."""
    current = get_current_value(setting)
    for opt in setting["options"]:
        if opt["value"] == current:
            return opt["label"]
    return f"(кастом) {current}"


def get_setting_by_index(i: int) -> dict | None:
    if 0 <= i < len(SETTINGS):
        return SETTINGS[i]
    return None


def get_option_by_index(setting: dict, j: int) -> dict | None:
    opts = setting.get("options", [])
    if 0 <= j < len(opts):
        return opts[j]
    return None


# ─── ЗАПИСЬ В RAILWAY ────────────────────────────────────────────────────────

def set_railway_variable(name: str, value: str) -> tuple[bool, str]:
    """Обновляет env-переменную через Railway GraphQL API.
    Возвращает (ok, error_message). При ok=True error_message пустая.

    После успешного вызова Railway автоматически передеплоит сервис —
    бот перезапустится и подхватит новое значение. Локально также
    обновляем os.environ, чтобы последующие чтения в этом процессе
    (до рестарта) видели новое значение.
    """
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

    # Успех — обновляем локальный os.environ, чтобы в этом процессе
    # (до рестарта Railway) чтения возвращали свежее значение.
    os.environ[name] = value
    logger.info(f"settings: переменная {name}={value} сохранена в Railway")
    return True, ""


# ─── ЛОГИРОВАНИЕ ТЕКУЩИХ ЗНАЧЕНИЙ ПРИ СТАРТЕ ─────────────────────────────────

def log_current_settings() -> None:
    """Вывести в логи все текущие значения — удобно после рестарта видеть
    что реально подхватилось из Railway env."""
    lines = ["Текущие настройки бота:"]
    for s in SETTINGS:
        lines.append(f"  {s['key']} = {get_current_value(s)}")
    logger.info("\n".join(lines))
