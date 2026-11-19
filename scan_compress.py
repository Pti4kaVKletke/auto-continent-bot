"""
Сжатие PDF-сканов перед загрузкой на Drive.

Сканер МФУ даёт 300–600 dpi, и один лист легко весит несколько мегабайт, хотя
для чтения и печати с запасом хватает ~150 dpi. Сжимаем только картинки внутри
PDF (текст и векторная часть не трогаются), и только те, что плотнее
DPI_THRESHOLD. Результат берём, лишь когда он заметно меньше (MIN_GAIN) и число
страниц не изменилось; любая ошибка — остаётся исходный файл: сжатие никогда
не должно мешать загрузке скана.

Настраивается в меню бота «⚙️ Настройки» (хранится в БД бота, применяется сразу):
  SCAN_COMPRESS       — 1 включено / 0 выключено;
  SCAN_COMPRESS_LEVEL — gentle / balanced / strong (см. LEVELS).
"""
import logging
import os

logger = logging.getLogger(__name__)

# уровень → (dpi после сжатия, качество JPEG). Ужимаются картинки плотнее
# dpi + DPI_MARGIN: те, что и так близко к целевой плотности, не трогаем.
LEVELS = {
    "gentle":   (200, 80),
    "balanced": (150, 72),
    "strong":   (120, 60),
}
DEFAULT_LEVEL = "balanced"
DPI_MARGIN    = 50
MIN_GAIN      = 0.10     # выигрыш меньше 10% — не стоит терять качество


def _setting(key: str, default: str) -> str:
    """Значение из настроек бота (БД, затем env). Без БД — просто env/по умолчанию."""
    try:
        import settings_service
        s = {"key": key, "default": default, "storage": "db"}
        return str(settings_service.get_current_value(s))
    except Exception:
        return os.environ.get(key, default)


def _enabled() -> bool:
    return _setting("SCAN_COMPRESS", "1").strip().lower() not in ("0", "false", "no", "off")


def level_params() -> tuple:
    """(dpi, качество) по выбранному уровню; незнакомое значение → «баланс»."""
    return LEVELS.get(_setting("SCAN_COMPRESS_LEVEL", DEFAULT_LEVEL).strip().lower(),
                      LEVELS[DEFAULT_LEVEL])


def compress_pdf(src: str, dst: str) -> tuple:
    """Сжимает PDF src → dst. Возвращает (путь_итогового_файла, размер_до, размер_после).

    Если сжатие выключено, не дало выигрыша или упало — путь равен src, размеры равны."""
    before = os.path.getsize(src)
    if not _enabled():
        return src, before, before
    try:
        import pymupdf as fitz
    except ImportError:  # старые версии
        import fitz

    try:
        doc = fitz.open(src)
        try:
            pages = doc.page_count
            if not pages:
                return src, before, before
            dpi, quality = level_params()
            doc.rewrite_images(
                dpi_threshold=dpi + DPI_MARGIN,
                dpi_target=dpi,
                quality=quality,
            )
            doc.save(dst, garbage=4, deflate=True, use_objstms=True)
        finally:
            doc.close()

        # Проверяем результат: открывается и страниц столько же
        chk = fitz.open(dst)
        try:
            ok = chk.page_count == pages
        finally:
            chk.close()
        after = os.path.getsize(dst)
        if ok and after <= before * (1 - MIN_GAIN):
            return dst, before, after
    except Exception as e:
        logger.warning(f"Сжатие PDF не удалось, грузим как есть: {e}")
    if os.path.exists(dst):
        try:
            os.remove(dst)
        except OSError:
            pass
    return src, before, before


def human_size(n: int) -> str:
    """12 345 678 → «11,8 МБ»; меньше мегабайта — в КБ."""
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f}".replace(".", ",") + " МБ"
    return f"{max(1, round(n / 1024))} КБ"


def saved_note(before: int, after: int) -> str:
    """Строка для ответа бота («🗜 Сжато: 18,4 МБ → 3,1 МБ») или пусто."""
    if after >= before:
        return ""
    return f"🗜 Сжато: {human_size(before)} → {human_size(after)}"
