# -*- coding: utf-8 -*-
"""Разделение общего файла на отдельные сканы: разметка страниц, ручной ввод, загрузка."""
import asyncio
import io
from types import SimpleNamespace

import pytest
from PIL import Image

import cb_deal
from scan_split import (format_range, parse_ranges, prepare_source,
                        ranges_from_page_labels, split_pdf)

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz


def make_pdf(path, pages):
    d = fitz.open()
    for _ in range(pages):
        d.new_page()
    d.save(str(path))
    d.close()
    return str(path)


def n_pages(data):
    with fitz.open(stream=data, filetype="pdf") as d:
        return d.page_count


# ───────── чистые функции ─────────
def test_ranges_from_labels_ok_and_invalid():
    labels = ["ag", "ag", "ag", "dkp", "dkp", "act", "other"]
    assert ranges_from_page_labels(labels, ["ag", "dkp", "act"]) == \
        {"ag": (1, 3), "dkp": (4, 5), "act": (6, 6)}
    assert ranges_from_page_labels(["ag", "dkp", "ag"], ["ag", "dkp"]) is None   # не подряд
    assert ranges_from_page_labels(["ag", "ag"], ["ag", "dkp"]) is None           # ДКП не найден
    assert ranges_from_page_labels(["ag", "act"], ["ag", "dkp"]) is None          # чужой код
    assert ranges_from_page_labels(None, ["ag"]) is None


def test_parse_ranges():
    r, err = parse_ranges("АГ 1-4, ДКП 5-6; Расп 7", ["ag", "dkp", "receipt"], 7)
    assert err is None and r == {"ag": (1, 4), "dkp": (5, 6), "receipt": (7, 7)}
    r, err = parse_ranges("аг договор 1", ["ag"], 3)       # «аг договор» → не знаем слово
    assert r is None and err
    assert parse_ranges("АГ 1-9", ["ag"], 5)[1]             # за пределами
    assert "пересек" in parse_ranges("АГ 1-3, ДКП 3-4", ["ag", "dkp"], 5)[1]
    assert "Не указаны" in parse_ranges("АГ 1-3", ["ag", "dkp"], 5)[1]
    assert "не отмечен" in parse_ranges("АГ 1-3, Акт 4", ["ag"], 5)[1]
    assert format_range((2, 2)) == "стр. 2" and format_range((1, 4)) == "стр. 1–4"


def test_split_pdf_and_prepare_source(tmp_path):
    src = make_pdf(tmp_path / "a.pdf", 6)
    parts = split_pdf(src, {"ag": (1, 4), "dkp": (5, 6)}, str(tmp_path), "t")
    assert n_pages(open(parts["ag"], "rb").read()) == 4
    assert n_pages(open(parts["dkp"], "rb").read()) == 2
    # фото → PDF
    imgs = []
    for i in range(3):
        p = tmp_path / f"i{i}.jpg"
        Image.new("RGB", (80, 120)).save(p)
        imgs.append(str(p))
    pending = {"filepath": imgs[0], "filename": "photo.jpg", "pages": imgs}
    path, n = prepare_source(pending, str(tmp_path / "src.pdf"))
    assert n == 3 and path.endswith("src.pdf")
    pending = {"filepath": src, "filename": "a.pdf", "pages": None}
    assert prepare_source(pending, str(tmp_path / "x.pdf")) == (src, 6)


# ───────── сценарий в боте ─────────
class _Msg:
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, **kw):
        self.sent.append((text, kw))
        return SimpleNamespace(message_id=len(self.sent))


class _Q:
    def __init__(self, msg):
        self.message = msg

    async def edit_message_text(self, text, **kw):
        self.message.sent.append((text, kw))

    async def edit_message_reply_markup(self, **kw):
        pass


DEAL = {"buyer_name": "Иванов", "car_model": "X", "car_vin": "V",
        "Папка Drive": "https://drive.google.com/drive/folders/F1"}


def fake_agent(uploads, labels):
    async def get_deal(num):
        return DEAL

    async def list_scan_files(num):
        return "S", []

    async def upload_file(path, name, folder):
        uploads.append((name, open(path, "rb").read()))
        return "link"

    async def folder(name, parent):
        return "SCANS"

    async def refresh(num):
        return "статус"

    async def classify(path, codes):
        return labels

    return SimpleNamespace(
        sheets=SimpleNamespace(get_deal=get_deal), list_scan_files=list_scan_files,
        refresh_scan_status=refresh, classify_scan_pages=classify,
        drive=SimpleNamespace(upload_file=upload_file, _get_or_create_folder=folder))


UPD = SimpleNamespace(effective_chat=SimpleNamespace(id=1))


def run(c):
    return asyncio.run(c)


def test_auto_split_confirm_and_upload(monkeypatch, tmp_path):
    uploads = []
    labels = ["ag", "ag", "ag", "dkp", "dkp", "act"]
    monkeypatch.setattr(cb_deal, "agent", fake_agent(uploads, labels))
    f = make_pdf(tmp_path / "Подп_АГ+ДКП+Акт_110826001.pdf", 6)
    msg, ctx = _Msg(), SimpleNamespace(user_data={})
    run(cb_deal.begin_scan_upload(msg, ctx, "1", "110826001", None, f,
                                  "Подп_АГ+ДКП+Акт_110826001.pdf"))
    confirm = msg.sent[-1][0]
    assert "стр. 1–3" in confirm and "стр. 4–5" in confirm and "стр. 6" in confirm
    assert not uploads                                  # пока ничего не загружено
    run(cb_deal.on_scansplit(UPD, ctx, _Q(msg), "scansplit:ok"))
    names = [n for n, _ in uploads]
    assert names == ["Подп_АГ_Договор_110826001.pdf", "Подп_ДКП_ТС_110826001.pdf",
                     "Подп_Акт_110826001.pdf"]
    assert [n_pages(d) for _, d in uploads] == [3, 2, 1]
    assert "pending_scan" not in ctx.user_data


def test_manual_ranges_when_auto_fails(monkeypatch, tmp_path):
    uploads = []
    monkeypatch.setattr(cb_deal, "agent", fake_agent(uploads, None))   # модель не справилась
    imgs = []
    for i in range(3):
        p = tmp_path / f"i{i}.jpg"
        Image.new("RGB", (80, 120)).save(p)
        imgs.append(str(p))
    msg, ctx = _Msg(), SimpleNamespace(user_data={})
    pending = {"num": "110826001", "folder_id": "F1", "filepath": imgs[0],
               "filename": "photo.jpg", "pages": imgs, "is_kg": False}
    run(cb_deal.start_split(msg, ctx, "1", pending, ["ag", "receipt"]))
    assert ctx.user_data["awaiting_scan_ranges"] is True
    assert "автоматически" in msg.sent[-1][0]
    sp = ctx.user_data["pending_scan"]["split"]
    ranges, err = parse_ranges("АГ 1-2, Расп 3", sp["codes"], sp["n"])
    assert err is None
    sp["ranges"] = ranges
    run(cb_deal.on_scansplit(UPD, ctx, _Q(msg), "scansplit:ok"))
    assert [n for n, _ in uploads] == ["Подп_АГ_Договор_110826001.pdf",
                                       "Подп_Расписка_110826001.pdf"]
    assert [n_pages(d) for _, d in uploads] == [2, 1]


def test_keep_as_one_file(monkeypatch, tmp_path):
    uploads = []
    monkeypatch.setattr(cb_deal, "agent",
                        fake_agent(uploads, ["ag", "ag", "dkp"]))
    f = make_pdf(tmp_path / "Подп_АГ+ДКП_110826001.pdf", 3)
    msg, ctx = _Msg(), SimpleNamespace(user_data={})
    run(cb_deal.begin_scan_upload(msg, ctx, "1", "110826001", None, f,
                                  "Подп_АГ+ДКП_110826001.pdf"))
    run(cb_deal.on_scansplit(UPD, ctx, _Q(msg), "scansplit:keep"))
    assert [n for n, _ in uploads] == ["Подп_АГ+ДКП_110826001.pdf"]
    assert n_pages(uploads[0][1]) == 3


def test_too_few_pages_falls_back(monkeypatch, tmp_path):
    uploads = []
    monkeypatch.setattr(cb_deal, "agent", fake_agent(uploads, None))
    f = make_pdf(tmp_path / "one.pdf", 1)
    msg, ctx = _Msg(), SimpleNamespace(user_data={})
    pending = {"num": "110826001", "folder_id": "F1", "filepath": f,
               "filename": "one.pdf", "pages": None, "is_kg": False}
    run(cb_deal.start_split(msg, ctx, "1", pending, ["ag", "dkp"]))
    assert [n for n, _ in uploads] == ["Подп_АГ+ДКП_110826001.pdf"]
    assert any("меньше" in t for t, _ in msg.sent)
