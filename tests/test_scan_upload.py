# -*- coding: utf-8 -*-
"""Загрузка скана через бота: тип по имени файла и фото → PDF."""
from PIL import Image

from scan_upload import (auto_scan_type, contract_numbers_in, images_to_pdf,
                         is_image_name)

ALL = ["ag", "dkp", "receipt", "report", "act", "bundle", "invoice", "id", "other"]


def test_auto_type_by_name():
    assert auto_scan_type("Подп_ДКП_ТС_110826001.pdf", "110826001", ALL) == ("dkp", None)
    assert auto_scan_type("подп акт.PDF", "110826001", ALL) == ("act", None)
    assert auto_scan_type("Скан_АД_110826001.pdf", "110826001", ALL) == ("bundle", None)
    assert auto_scan_type("Подп_САГ_Договор_260926901.pdf", "260926901", ALL) == ("ag", None)


def test_no_auto_for_unsigned_or_unknown():
    assert auto_scan_type("photo.jpg", "110826001", ALL) == (None, None)
    assert auto_scan_type("ДКП_ТС_110826001.pdf", "110826001", ALL) == (None, None)  # без «Подп»


def test_other_deal_number_disables_auto():
    code, warn = auto_scan_type("Подп_ДКП_ТС_120826002.pdf", "110826001", ALL)
    assert code is None and "120826002" in warn and "110826001" in warn


def test_kg_only_type_not_auto_for_regular_deal():
    allowed = [c for c in ALL if c != "spec"]
    assert auto_scan_type("Подп_Спец_110826001.pdf", "110826001", allowed) == (None, None)


def test_contract_numbers():
    assert contract_numbers_in("Подп_ДКП_110826001_2.pdf") == {"110826001"}
    assert contract_numbers_in("scan_1788979504799.jpg") == set()  # 13 цифр — не номер


def test_is_image_name():
    assert is_image_name("photo.jpg") and is_image_name("A.PNG")
    assert not is_image_name("Подп_АГ.pdf")


def test_images_to_pdf_multipage(tmp_path):
    paths = []
    for i, size in enumerate([(300, 400), (5000, 3000)]):
        p = tmp_path / f"p{i}.png"
        Image.new("RGBA", size, (255, 255, 255, 255)).save(p)
        paths.append(str(p))
    out = images_to_pdf(paths, str(tmp_path / "out.pdf"))
    data = open(out, "rb").read()
    assert data.startswith(b"%PDF")
    assert data.count(b"/Type /Page\n") + data.count(b"/Type /Page ") + data.count(b"/Type/Page") >= 2


# ───────── несколько документов в одном файле ─────────
import asyncio
from types import SimpleNamespace

import agent as agent_mod


def test_multi_name_and_codes_roundtrip():
    name = agent_mod.scan_multi_name(["act", "ag", "report", "dkp"]) + "_110826001.pdf"
    assert name == "Подп_АГ+ДКП+Отч+Акт_110826001.pdf"   # порядок — как в списке типов
    assert agent_mod.scan_codes_of(name) == {"ag", "dkp", "report", "act"}
    # только неподписываемые — без «Подп_», как и обычная платёжка
    assert agent_mod.scan_multi_name(["invoice", "id"]) == "Плат+Док"
    assert agent_mod.scan_codes_of("Плат+Док_110826001.pdf") == frozenset()


def test_multi_status_counts_every_document():
    names = ["Подп_АГ+ДКП+Расп_1.pdf", "Подп_Акт_Отчет_агента_1.pdf"]   # старое имя тоже
    assert agent_mod.scan_status_text(names) == "5/5 ✓"
    assert agent_mod.scan_status_text(["Подп_АГ+ДКП+Плат_1.pdf"]) == \
        "2/5 · нет: расписка, акт, отчёт агента"
    # обычные файлы считаются как раньше
    assert agent_mod.scan_status_text(["Подп_Акт_1.pdf"]) == "1/5 · нет: аг договор + поручение, дкп, расписка, отчёт агента"


def test_auto_type_for_multi_name():
    code, warn = auto_scan_type("Подп_АГ+ДКП+Акт_110826001.pdf", "110826001", ALL)
    assert code == ("ag", "dkp", "act") and warn is None
    code, warn = auto_scan_type("Подп_АГ+ДКП_120826002.pdf", "110826001", ALL)
    assert code is None and "120826002" in warn


# ───────── сводка и сценарий загрузки ─────────
import cb_deal  # noqa: E402
from scan_upload import deal_summary  # noqa: E402

DEAL = {"buyer_name": "Иванов Иван Иванович", "car_model": "Zeekr 001",
        "car_vin": "LC0CE4CB1R0000001", "Сумма Договора": "1500000", "currency": "руб",
        "Папка Drive": "https://drive.google.com/drive/folders/FOLDER1"}


def test_deal_summary():
    t = deal_summary(DEAL, "110826001")
    assert "Сделка найдена: 110826001" in t and "Иванов Иван Иванович" in t
    assert "Zeekr 001" in t and "LC0CE4CB1R0000001" in t and "1 500 000" in t


class _Msg:
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, **kw):
        self.sent.append((text, kw))
        return SimpleNamespace(message_id=len(self.sent))


def _fake_agent(deal, uploads):
    async def get_deal(num):
        return deal if num == "110826001" else None

    async def list_scan_files(num):
        return "S", []

    async def upload_file(path, name, folder):
        uploads.append((name, open(path, "rb").read(8)))

    async def folder(name, parent):
        return "SCANS"

    async def refresh(num):
        return "1/5 · нет: ..."

    return SimpleNamespace(
        sheets=SimpleNamespace(get_deal=get_deal),
        drive=SimpleNamespace(upload_file=upload_file, _get_or_create_folder=folder),
        list_scan_files=list_scan_files, refresh_scan_status=refresh)


def _run(coro):
    return asyncio.run(coro)


def test_begin_unknown_deal(monkeypatch, tmp_path):
    monkeypatch.setattr(cb_deal, "agent", _fake_agent(DEAL, []))
    msg, ctx = _Msg(), SimpleNamespace(user_data={})
    ok = _run(cb_deal.begin_scan_upload(msg, ctx, "1", "999999999", None,
                                        str(tmp_path / "x.jpg"), "photo.jpg", show_summary=True))
    assert ok is False and "не найдена" in msg.sent[0][0]
    assert "pending_scan" not in ctx.user_data


def test_begin_asks_type_for_photo_and_builds_pdf(monkeypatch, tmp_path):
    uploads = []
    monkeypatch.setattr(cb_deal, "agent", _fake_agent(DEAL, uploads))
    pages = []
    for i in range(3):
        p = tmp_path / f"p{i}.jpg"
        Image.new("RGB", (200, 300), (i * 50, 0, 0)).save(p)
        pages.append(str(p))
    msg, ctx = _Msg(), SimpleNamespace(user_data={})
    ok = _run(cb_deal.begin_scan_upload(msg, ctx, "1", "110826001", None,
                                        pages[0], "photo.jpg", pages=pages, show_summary=True))
    assert ok is True
    assert "Сделка найдена" in msg.sent[0][0]          # сводка
    assert "Файлов: 3" in msg.sent[1][0]               # вопрос про тип
    pending = ctx.user_data["pending_scan"]
    assert pending["folder_id"] == "FOLDER1" and len(pending["pages"]) == 3
    # выбрали «Платёжка» → PDF с нужным именем
    _run(cb_deal.upload_scan(msg, "1", pending, "invoice"))
    assert uploads == [("Платежка_110826001.pdf", b"%PDF-1.4")] or \
        (uploads[0][0] == "Платежка_110826001.pdf" and uploads[0][1].startswith(b"%PDF"))


def test_begin_auto_type_by_name(monkeypatch, tmp_path):
    uploads = []
    monkeypatch.setattr(cb_deal, "agent", _fake_agent(DEAL, uploads))
    f = tmp_path / "Подп_ДКП_ТС_110826001.pdf"
    f.write_bytes(b"%PDF-1.4 test")
    msg, ctx = _Msg(), SimpleNamespace(user_data={})
    _run(cb_deal.begin_scan_upload(msg, ctx, "1", "110826001", "F", str(f),
                                   f.name))
    assert uploads[0][0] == "Подп_ДКП_ТС_110826001.pdf"
    assert "pending_scan" not in ctx.user_data


def test_multi_select_flow(monkeypatch, tmp_path):
    """Галочки: АГ + ДКП + Платёжка → один PDF с общим именем."""
    uploads = []
    monkeypatch.setattr(cb_deal, "agent", _fake_agent(DEAL, uploads))
    p = tmp_path / "p.jpg"
    Image.new("RGB", (200, 300)).save(p)
    msg, ctx = _Msg(), SimpleNamespace(user_data={})
    _run(cb_deal.begin_scan_upload(msg, ctx, "1", "110826001", None, str(p), "photo.jpg"))

    class Q:
        message = msg
        async def edit_message_text(self, *a, **kw):
            msg.sent.append((a[0] if a else "", kw))

    upd = SimpleNamespace(effective_chat=SimpleNamespace(id=1))
    for d in ("scanmulti:open", "scanmulti:t:ag", "scanmulti:t:dkp",
              "scanmulti:t:invoice", "scanmulti:t:dkp", "scanmulti:t:dkp"):
        _run(cb_deal.on_scanmulti(upd, ctx, Q(), d))
    assert ctx.user_data["pending_scan"]["multi"] == ["ag", "invoice", "dkp"]
    _run(cb_deal.on_scanmulti(upd, ctx, Q(), "scanmulti:done"))
    assert "pending_scan" not in ctx.user_data
    assert uploads[0][0] == "Подп_АГ+ДКП+Плат_110826001.pdf"
    assert uploads[0][1].startswith(b"%PDF")


def test_multi_done_without_selection_keeps_pending(monkeypatch, tmp_path):
    monkeypatch.setattr(cb_deal, "agent", _fake_agent(DEAL, []))
    p = tmp_path / "p.jpg"
    Image.new("RGB", (50, 50)).save(p)
    msg, ctx = _Msg(), SimpleNamespace(user_data={})
    _run(cb_deal.begin_scan_upload(msg, ctx, "1", "110826001", None, str(p), "photo.jpg"))

    class Q:
        message = msg
        async def edit_message_text(self, *a, **kw):
            msg.sent.append((a[0] if a else "", kw))

    _run(cb_deal.on_scanmulti(SimpleNamespace(effective_chat=SimpleNamespace(id=1)),
                              ctx, Q(), "scanmulti:done"))
    assert "pending_scan" in ctx.user_data and "хотя бы один" in msg.sent[-1][0]


def test_pdf_and_photo_merge_into_one_payment_scan(monkeypatch, tmp_path):
    """Платёжка: PDF (2 стр.) + фото → один Платежка_<номер>.pdf из 3 страниц."""
    import pymupdf as fitz
    from scan_upload import files_to_pdf, is_scan_part_name

    pdf = tmp_path / "pay.pdf"
    d = fitz.open()
    d.new_page(); d.new_page()
    d.save(str(pdf)); d.close()
    jpg = tmp_path / "pay.jpg"
    Image.new("RGB", (200, 300)).save(jpg)

    assert is_scan_part_name("a.PDF") and is_scan_part_name("b.jpg") and not is_scan_part_name("c.docx")
    out, n = files_to_pdf([str(pdf), str(jpg)], str(tmp_path / "m.pdf"))
    assert n == 3

    uploads = []
    monkeypatch.setattr(cb_deal, "agent", _fake_agent(DEAL, uploads))
    msg, ctx = _Msg(), SimpleNamespace(user_data={})
    _run(cb_deal.begin_scan_upload(msg, ctx, "1", "110826001", None, str(jpg), "photo.jpg",
                                   pages=[str(jpg), str(pdf)]))
    pending = ctx.user_data["pending_scan"]
    assert len(pending["pages"]) == 2 and "Файлов: 2" in msg.sent[-1][0]
    _run(cb_deal.upload_scan(msg, "1", pending, "invoice"))
    assert uploads[0][0] == "Платежка_110826001.pdf"
    assert any("страниц в PDF: 3" in t for t, _ in msg.sent)


def test_single_pdf_uploaded_as_is(monkeypatch, tmp_path):
    uploads = []
    monkeypatch.setattr(cb_deal, "agent", _fake_agent(DEAL, uploads))
    f = tmp_path / "one.pdf"
    f.write_bytes(b"%PDF-1.4 original-bytes")
    msg, ctx = _Msg(), SimpleNamespace(user_data={})
    _run(cb_deal.begin_scan_upload(msg, ctx, "1", "110826001", None, str(f), "one.pdf"))
    _run(cb_deal.upload_scan(msg, "1", ctx.user_data["pending_scan"], "invoice"))
    assert uploads[0] == ("Платежка_110826001.pdf", b"%PDF-1.4")      # файл невалидный: загружен как есть, без пересборки
