"""Сжатие PDF-сканов: уменьшает тяжёлые сканы, не ломает остальное."""
import io
import os
import random

import pytest

import scan_compress as sc


def _heavy_pdf(path, pages=2):
    """PDF из «шумных» картинок 300 dpi — похож на скан с МФУ."""
    from PIL import Image
    rnd = random.Random(1)
    ims = []
    for _ in range(pages):
        im = Image.effect_noise((2480, 3508), 60).convert("RGB")
        ims.append(im)
    ims[0].save(path, "PDF", save_all=True, append_images=ims[1:], resolution=300.0, quality=98)


def _text_pdf(path):
    import pymupdf
    d = pymupdf.open()
    p = d.new_page()
    p.insert_text((72, 72), "Hello scan")
    d.save(path)
    d.close()


def test_heavy_pdf_shrinks_and_keeps_pages(tmp_path):
    import pymupdf
    src, dst = str(tmp_path / "a.pdf"), str(tmp_path / "b.pdf")
    _heavy_pdf(src)
    out, before, after = sc.compress_pdf(src, dst)
    assert out == dst and after < before * 0.8
    with pymupdf.open(out) as d:
        assert d.page_count == 2


def test_small_text_pdf_untouched(tmp_path):
    src, dst = str(tmp_path / "t.pdf"), str(tmp_path / "t2.pdf")
    _text_pdf(src)
    out, before, after = sc.compress_pdf(src, dst)
    assert out == src and before == after
    assert not os.path.exists(dst)


def test_broken_file_returns_original(tmp_path):
    src, dst = str(tmp_path / "bad.pdf"), str(tmp_path / "bad2.pdf")
    open(src, "wb").write(b"%PDF-1.4 not really")
    out, before, after = sc.compress_pdf(src, dst)
    assert out == src and before == after
    assert not os.path.exists(dst)


def test_can_be_disabled(tmp_path, monkeypatch):
    src, dst = str(tmp_path / "a.pdf"), str(tmp_path / "b.pdf")
    _heavy_pdf(src, pages=1)
    monkeypatch.setenv("SCAN_COMPRESS", "0")
    out, before, after = sc.compress_pdf(src, dst)
    assert out == src and before == after


def test_notes():
    assert sc.human_size(18_400_000) == "17,5 МБ"
    assert sc.human_size(500 * 1024) == "500 КБ"
    assert sc.saved_note(10, 10) == ""
    assert sc.saved_note(18 * 1024 * 1024, 3 * 1024 * 1024) == "🗜 Сжато: 18,0 МБ → 3,0 МБ"


def test_levels_and_unknown_level(monkeypatch):
    for lvl, (dpi, q) in sc.LEVELS.items():
        monkeypatch.setenv("SCAN_COMPRESS_LEVEL", lvl)
        assert sc.level_params() == (dpi, q)
    monkeypatch.setenv("SCAN_COMPRESS_LEVEL", "что-то")
    assert sc.level_params() == sc.LEVELS["balanced"]


def test_stronger_level_gives_smaller_file(tmp_path, monkeypatch):
    src = str(tmp_path / "a.pdf")
    _heavy_pdf(src, pages=1)
    sizes = {}
    for lvl in ("gentle", "strong"):
        monkeypatch.setenv("SCAN_COMPRESS_LEVEL", lvl)
        _o, _b, sizes[lvl] = sc.compress_pdf(src, str(tmp_path / f"{lvl}.pdf"))
    assert sizes["strong"] < sizes["gentle"]


def test_settings_registered_in_menu():
    import settings_service as ss
    keys = {s["key"]: s for s in ss.SETTINGS}
    for k in ("SCAN_COMPRESS", "SCAN_COMPRESS_LEVEL"):
        assert keys[k]["storage"] == "db"
    values = {o["value"] for o in keys["SCAN_COMPRESS_LEVEL"]["options"]}
    assert values == set(sc.LEVELS)
