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
# уровень → максимальная длинная сторона фото, px (для JPG; ~ А4 при dpi уровня)
IMAGE_MAX_SIDE = {"gentle": 2600, "balanced": 2000, "strong": 1600}
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


def _level_name() -> str:
    name = _setting("SCAN_COMPRESS_LEVEL", DEFAULT_LEVEL).strip().lower()
    return name if name in LEVELS else DEFAULT_LEVEL


def compress_image(src: str, dst: str) -> tuple:
    """Уменьшает JPEG-фото (паспорт, декларация с телефона) → dst, формат остаётся JPEG.

    Возвращает (путь, до, после); если выигрыша нет или файл не JPEG/упало — путь src."""
    before = os.path.getsize(src)
    if not _enabled() or not src.lower().endswith((".jpg", ".jpeg")):
        return src, before, before
    try:
        from PIL import Image, ImageOps
        _dpi, quality = level_params()
        side = IMAGE_MAX_SIDE[_level_name()]
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)      # поворот с телефона
            if im.mode != "RGB":
                im = im.convert("RGB")
            if max(im.size) > side:
                im.thumbnail((side, side))
            im.save(dst, "JPEG", quality=quality, optimize=True)
        after = os.path.getsize(dst)
        if after <= before * (1 - MIN_GAIN):
            return dst, before, after
    except Exception as e:
        logger.warning(f"Сжатие фото не удалось, грузим как есть: {e}")
    if os.path.exists(dst):
        try:
            os.remove(dst)
        except OSError:
            pass
    return src, before, before


def compress_file(src: str) -> tuple:
    """Сжимает файл по расширению (PDF или JPEG) во временный файл рядом.
    Возвращает (путь, до, после): путь равен src, если сжатие не пригодилось."""
    dst = os.path.join(os.path.dirname(src) or ".", f"zip_{os.path.basename(src)}")
    low = src.lower()
    if low.endswith(".pdf"):
        return compress_pdf(src, dst)
    if low.endswith((".jpg", ".jpeg")):
        return compress_image(src, dst)
    size = os.path.getsize(src)
    return src, size, size


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
