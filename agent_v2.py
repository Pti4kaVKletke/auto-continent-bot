"""
agent_v2.py — DocumentAgent с полноценным циклом tool-use.

Архитектура v2:
  LLM → tool_use → tool_result обратно в LLM → следующий ход → ... → end_turn

В отличие от v1 (один вызов LLM → инструмент → ответ пользователю),
здесь LLM видит результаты своих инструментов и может принимать
решения на основе полученных данных: например, после find_deal
самостоятельно составить update_deal с нужными полями.

Совместимость: полностью совместим с bot.py, drive_service, gsheets_service,
doc_builder, memory — никаких изменений в других модулях не требуется.
Переключение: Railway env AGENT_VERSION=v2.
"""

import asyncio
import contextvars
import logging
import random
from pathlib import Path

import anthropic
import httpx

import memory
from agent import DocumentAgent as _AgentV1

logger = logging.getLogger(__name__)

# Защита от бесконечного цикла
MAX_ITERATIONS = 10

# Терминальные инструменты — после их выполнения LLM получает финальный ход
# с tool_choice=none, чтобы только сформулировать текстовый ответ и не вызвать
# что-то ещё (защита от случайного повторного создания документов).
TERMINAL_TOOLS = {
    "create_contract",
    "generate_docs",
    "import_deal",
    "cancel_deal",
    "complete_deal",
    "add_payment",       # результат уже содержит баланс, повторный вызов не нужен
    "remove_payment",
}

# ── ContextVar-хранилище состояния, изолированное per-async-task ─────────
# Решает race condition: при параллельных update'ах от Telegram каждый handler
# работает в своём asyncio.Task, contextvars автоматически изолируются.
# Родительский класс agent.py пишет `self._current_chat_id = ...` и
# `self._pending_check = ...` — через property ниже это уходит в ContextVar,
# не трогая instance __dict__.
_chat_id_ctx = contextvars.ContextVar("agent_chat_id", default="")
_pending_check_ctx = contextvars.ContextVar("agent_pending_check", default=None)

# ── Классификация ошибок API ─────────────────────────────────────────────
_HTTPX_RETRIABLE = (
    httpx.RemoteProtocolError,
    httpx.ReadTimeout,
    httpx.ConnectTimeout,
    httpx.ConnectError,
    httpx.ReadError,
)


def _is_retriable_anthropic(e: BaseException) -> bool:
    """Транзиентная ошибка Anthropic API — есть смысл повторить."""
    if isinstance(e, anthropic.APIConnectionError):  # сюда же APITimeoutError
        return True
    if isinstance(e, anthropic.RateLimitError):
        return True
    if isinstance(e, anthropic.InternalServerError):
        return True
    if isinstance(e, anthropic.APIStatusError):
        code = getattr(e, "status_code", 0) or 0
        return code >= 500 or code == 529  # 5xx + overloaded
    return False


def _retry_after_seconds(e: BaseException):
    """Читает заголовок Retry-After из 429 ответа, если он есть.

    Возвращает количество секунд (float) или None.
    """
    resp = getattr(e, "response", None)
    if resp is None:
        return None
    val = resp.headers.get("retry-after") if hasattr(resp, "headers") else None
    if not val:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


class DocumentAgent(_AgentV1):
    """
    Переопределяет только process_message — всё остальное наследуется из agent.py:
    _build_system_prompt, _get_tools, _execute_tool, _build_file_message, process_file.
    """

    # ── Прозрачный маршрут состояния на ContextVar ───────────────────────
    # Родительский класс пишет `self._current_chat_id = ...` и читает через
    # getattr — это уходит в ContextVar, изолированный per-async-task.
    # Data descriptor (property с setter) имеет приоритет над instance __dict__,
    # поэтому v1-код работает без изменений.

    @property
    def _current_chat_id(self) -> str:
        return _chat_id_ctx.get()

    @_current_chat_id.setter
    def _current_chat_id(self, value):
        _chat_id_ctx.set(value or "")

    @property
    def _pending_check(self):
        return _pending_check_ctx.get()

    @_pending_check.setter
    def _pending_check(self, value):
        _pending_check_ctx.set(value)

    async def process_message(
        self,
        user_text: str,
        filepath: str = None,
        filename: str = None,
        chat_id: str = "",
        force_tool: str = None,
    ) -> dict:
        """
        Обрабатывает сообщение через agentic loop.

        force_tool: если задан — на ПЕРВОЙ итерации LLM обязана вызвать именно этот
        инструмент (tool_choice type=tool). Защита от галлюцинации Haiku, когда
        она пишет "успех" без реального вызова. Используется bot.py для гарантированных
        путей (кнопка "Добавить оплату" → force_tool="add_payment").
        """
        self._current_chat_id = chat_id
        memory.add_to_history(
            "user",
            user_text if not filepath else f"[файл: {filename}] {user_text}",
        )

        # ── Кэшируем промпт и tools один раз на весь цикл ─────────────────
        # Промпт большой и дёргает SQLite (instructions, companies, bank_profiles),
        # пересобирать его на каждой итерации — лишняя нагрузка.
        system_prompt = self._build_system_prompt()
        tools = self._get_tools()

        # ── История + санитизация consecutive same-role ───────────────────
        history = memory.get_history(limit=15)
        sanitized = []
        for h in history[:-1]:
            if sanitized and sanitized[-1]["role"] == h["role"]:
                sanitized[-1]["content"] += "\n" + h["content"]
            else:
                sanitized.append({"role": h["role"], "content": h["content"]})
        if sanitized and sanitized[0]["role"] == "assistant":
            sanitized.pop(0)

        messages = sanitized

        if filepath:
            current_content = await self._build_file_message(filepath, filename, user_text)
        else:
            current_content = user_text

        # Если предыдущий тёрн упал между add_to_history("user") и
        # add_to_history("assistant"), последним в sanitized остался "user".
        # Добавление ещё одного user-сообщения даст 400 «expected alternating roles».
        # Вставляем placeholder-ассистента, чтобы восстановить чередование.
        if messages and messages[-1]["role"] == "user":
            messages.append({"role": "assistant", "content": "…"})

        messages.append({"role": "user", "content": current_content})

        # ── Agentic loop ──────────────────────────────────────────────────
        result = {"text": "", "files": [], "success": True, "buttons": None}
        tools_called = []         # для логирования при превышении лимита
        force_text_only = False   # если был terminal/button tool — следующий ход без tools

        for iteration in range(MAX_ITERATIONS):
            # Определяем tool_choice для этой итерации:
            # - force_text_only (после terminal или кнопок) → блокируем вызовы
            # - force_tool на первой итерации → обязываем вызвать конкретный инструмент
            # - иначе → LLM решает сама
            if force_text_only:
                tc = {"type": "none"}
            elif force_tool and iteration == 0:
                tc = {"type": "tool", "name": force_tool}
            else:
                tc = None

            response = await self._call_llm(
                messages,
                system_prompt,
                tools,
                iteration,
                tool_choice=tc,
            )
            if response is None:
                # Сетевая ошибка. Если уже что-то накопили — отдаём это,
                # иначе сообщаем об ошибке.
                if result["text"] or result["files"]:
                    result["text"] = (
                        result["text"] + "\n\n⚠️ Соединение прервалось — часть операций могла не завершиться."
                    ).strip()
                    result["success"] = False
                    memory.add_to_history("assistant", result["text"])
                    return result
                return {
                    "text": "⚠️ Ошибка соединения с AI. Попробуйте ещё раз.",
                    "files": [],
                    "success": False,
                }

            # Текст этого хода
            text_parts = [b.text for b in response.content if b.type == "text"]
            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

            if text_parts:
                chunk = "\n".join(text_parts)
                result["text"] = (result["text"] + "\n" + chunk).lstrip("\n")

            # Если max_tokens обрезал ответ — предупредим пользователя
            if response.stop_reason == "max_tokens":
                result["text"] += "\n\n⚠️ Ответ был обрезан (max_tokens). Уточните запрос."

            # Нет инструментов или принудительный текст-only → выходим
            if not tool_use_blocks or response.stop_reason == "end_turn" or force_text_only:
                logger.info(
                    f"[v2] Завершён за {iteration + 1} итераций "
                    f"(stop_reason={response.stop_reason}, tools={tools_called})"
                )
                break

            # ── Выполняем инструменты ─────────────────────────────────────
            tool_results_content = []
            needs_user_input = False
            had_terminal = False

            for tool_block in tool_use_blocks:
                tools_called.append(tool_block.name)
                logger.info(f"[v2] Итерация {iteration + 1}: {tool_block.name}")

                try:
                    tool_result = await self._execute_tool(tool_block.name, tool_block.input)
                except Exception as e:
                    logger.error(
                        f"[v2] Ошибка инструмента {tool_block.name}: {e}", exc_info=True
                    )
                    tool_result = {"message": f"⚠️ Ошибка выполнения {tool_block.name}: {e}"}

                self._collect_files(tool_result, result)

                if tool_result.get("buttons"):
                    result["buttons"] = tool_result["buttons"]
                    needs_user_input = True

                if tool_block.name in TERMINAL_TOOLS:
                    had_terminal = True

                tool_results_content.append({
                    "type":        "tool_result",
                    "tool_use_id": tool_block.id,
                    # Приоритет: error → message → дефолт.
                    # error используется в _execute_tool для сигнализации проблем
                    # которые LLM должна увидеть (например, попытка создать сделку
                    # с уже существующим номером). Без этого LLM получит "Выполнено"
                    # и подумает что всё ОК.
                    "content": (
                        tool_result.get("error")
                        or tool_result.get("message")
                        or "Выполнено"
                    ),
                })

            # Добавляем ответ ассистента + результаты инструментов в историю цикла
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user",      "content": tool_results_content})

            # После кнопок или терминала — ещё один ход чтобы LLM написала текст,
            # но БЕЗ права вызвать новый инструмент.
            if needs_user_input or had_terminal:
                force_text_only = True
                continue

        else:
            logger.warning(
                f"[v2] Лимит итераций ({MAX_ITERATIONS}) исчерпан. Tools: {tools_called}"
            )
            result["success"] = False
            if not result["text"]:
                result["text"] = (
                    "⚠️ Агент достиг лимита шагов. Попробуйте переформулировать запрос."
                )

        # Дедуп файлов на случай если LLM повторила tool-call с теми же артефактами
        result["files"] = self._dedup_files(result["files"])

        memory.add_to_history("assistant", result.get("text", ""))
        result = self._maybe_inject_bank_choice(result)
        return result

    # ── Вспомогательные методы ───────────────────────────────────────────

    async def _call_llm(
        self,
        messages: list,
        system_prompt: str,
        tools: list,
        iteration: int,
        max_tokens: int = 4096,
        tool_choice: dict = None,
    ):
        """Вызывает API с тремя попытками. Возвращает response или None.

        Обрабатывает:
        - Транзиентные сетевые ошибки httpx → ретрай с backoff
        - anthropic.RateLimitError (429) → ретрай, учитываем Retry-After
        - anthropic.InternalServerError и 5xx → ретрай с backoff
        - Постоянные (400/401/403/422) → возврат None без ретрая

        Использует prompt caching: cache_control на system prompt кэширует
        и tools, и system (всё что идёт до этого блока в запросе).
        Кэш живёт 5 минут с момента последнего использования.
        Экономия на input токенах в горячем кэше — порядка 90%.
        """
        kwargs = {
            "model":      self.model,
            "max_tokens": max_tokens,
            "system":     [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "tools":      tools,
            "messages":   messages,
        }
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice

        for attempt in range(3):
            try:
                response = await self.client.with_options(timeout=120.0).messages.create(**kwargs)
                # Логируем статистику кэша — видно ли в Railway что кэш работает
                u = getattr(response, "usage", None)
                if u is not None:
                    cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
                    cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0
                    if cache_read or cache_write:
                        logger.info(
                            f"[v2] Cache: read={cache_read} write={cache_write} "
                            f"input={u.input_tokens} output={u.output_tokens}"
                        )
                return response

            except _HTTPX_RETRIABLE as e:
                if attempt == 2:
                    logger.error(f"[v2] httpx ошибка после 3 попыток: {type(e).__name__}: {e}")
                    return None
                wait = (2 ** attempt) + random.uniform(0, 0.5)
                logger.warning(
                    f"[v2] httpx ошибка (итер.{iteration + 1}, попытка {attempt + 1}/3): "
                    f"{type(e).__name__}: {e} — пауза {wait:.1f}с"
                )
                await asyncio.sleep(wait)

            except anthropic.AnthropicError as e:
                # Постоянные ошибки — нет смысла ретраить
                if not _is_retriable_anthropic(e):
                    logger.error(
                        f"[v2] Постоянная ошибка API: {type(e).__name__}: {e}"
                    )
                    return None
                if attempt == 2:
                    logger.error(f"[v2] API ошибка после 3 попыток: {type(e).__name__}: {e}")
                    return None
                # Уважаем Retry-After для 429, иначе экспоненциальный backoff
                wait = _retry_after_seconds(e)
                if wait is None:
                    wait = (2 ** attempt) + random.uniform(0, 0.5)
                wait = min(wait, 30.0)  # потолок — не блокируем пользователя надолго
                logger.warning(
                    f"[v2] API ошибка (итер.{iteration + 1}, попытка {attempt + 1}/3): "
                    f"{type(e).__name__}: {e} — пауза {wait:.1f}с"
                )
                await asyncio.sleep(wait)

        return None

    @staticmethod
    def _collect_files(tool_result: dict, result: dict) -> None:
        """Собирает file/extra_files из tool_result в result['files']."""
        if tool_result.get("file"):
            result["files"].append({
                "file":       tool_result["file"],
                "filename":   tool_result["filename"],
                "drive_link": tool_result.get("drive_link", ""),
            })
        for f_path, f_name, f_link in zip(
            tool_result.get("extra_files", []),
            tool_result.get("extra_names", []),
            tool_result.get("extra_links",
                            [""] * len(tool_result.get("extra_files", []))),
        ):
            if Path(f_path).exists():
                            result["files"].append({
                    "file":       f_path,
                    "filename":   f_name,
                    "drive_link": f_link,
                })

    @staticmethod
    def _dedup_files(files: list) -> list:
        """Удаляет дубликаты файлов по пути, сохраняя порядок."""
        seen = set()
        out = []
        for f in files:
            key = f.get("file")
            if key and key not in seen:
                seen.add(key)
                out.append(f)
        return out
