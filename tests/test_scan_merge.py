# -*- coding: utf-8 -*-
"""Единый PDF «Комплект_<номер>.pdf»: порядок, имя, статус, замена прежнего."""
import asyncio
import io
from types import SimpleNamespace

import pytest
from PIL import Image

import agent as agent_mod
import cb_deal
from scan_merge import merge_parts, merged_name, plan_merge


def F(name, i, mime="application/pdf"):
    return {"id": f"id{i}", "name": name, "mimeType": mime}


def _pdf(pages=1):
    import pymupdf as fitz
    d = fitz.open()
    for _ in range(pages):
        d.new_page()
    b = d.tobytes()
    d.close()
    return b


def test_status_ignores_merged_file():
    names = ["Подп_АГ_Договор_110826001.pdf", "Комплект_110826001 (1 из 5, нет ДКП Расп Отч Акт).pdf"]
    assert agent_mod.is_merged_scan(names[1]) and not agent_mod.is_merged_scan(names[0])
    # единый файл не даёт «(+1)» и не считается документом
    assert agent_mod.scan_status_text(names) == agent_mod.scan_status_text(names[:1])
    assert agent_mod.scan_status_text([names[1]]) == "нет"


def test_plan_order_name_and_missing():
    files = [F("Подп_Акт_110826001.pdf", 1), F("Платежка_110826001.pdf", 2),
             F("Подп_АГ_Договор_110826001.pdf", 3), F("Подп_ДКП_ТС_110826001.pdf", 4)]
    plan = plan_merge(files)
    assert [f["name"] for f in plan["items"]] == [
        "Подп_АГ_Договор_110826001.pdf", "Подп_ДКП_ТС_110826001.pdf", "Подп_Акт_110826001.pdf"]
    assert plan["skipped"] == ["Платежка_110826001.pdf"]
    assert plan["missing"] == ["receipt", "report"]
    assert merged_name("110826001", plan) == "Комплект_110826001 (3 из 5, нет Расп Отч).pdf"


def test_plan_complete_with_multi_file_and_dups():
    files = [F("Подп_АГ+ДКП+Расп_1.pdf", 1), F("Подп_Акт_Отчет_агента_1.pdf", 2),
             F("Подп_Акт_1.pdf", 3), F("Подп_Акт_1_2.pdf", 4)]
    plan = plan_merge(files)
    assert plan["missing"] == [] and merged_name("110826001", plan) == "Комплект_110826001.pdf"
    assert plan["dups"] == ["act"]


def test_merge_parts_pdf_and_image(tmp_path):
    img = io.BytesIO()
    Image.new("RGB", (100, 140)).save(img, "JPEG")
    out = str(tmp_path / "m.pdf")
    pages, bad = merge_parts([("a.pdf", _pdf(2), False), ("b.jpg", img.getvalue(), True),
                              ("broken.pdf", b"not a pdf", False)], out)
    assert pages == 3 and bad == ["broken.pdf"]


def _fake(files, store, trashed, downloads):
    async def list_scan_files(num):
        return "SCANS", files

    async def get_deal(num):
        return {}

    async def upload_file(path, name, folder):
        store.append((name, open(path, "rb").read(5)))
        return "https://drive/x"

    async def trash_file(fid):
        trashed.append(fid)
        return True

    async def download(fid):
        return downloads.get(fid, b"")

    async def refresh(num):
        return "статус"

    return SimpleNamespace(
        list_scan_files=list_scan_files, refresh_scan_status=refresh,
        sheets=SimpleNamespace(get_deal=get_deal),
        drive=SimpleNamespace(upload_file=upload_file, trash_file=trash_file,
                              download_file_bytes=download))


def test_build_merged_replaces_old(monkeypatch):
    old = F("Комплект_110826001 (2 из 5, нет Расп Акт Отч).pdf", 9)
    files = [F("Подп_АГ_Договор_110826001.pdf", 1), F("Подп_ДКП_ТС_110826001.pdf", 2), old]
    store, trashed = [], []
    monkeypatch.setattr(cb_deal, "agent", _fake(files, store, trashed,
                                                 {"id1": _pdf(2), "id2": _pdf(1)}))
    r = asyncio.run(cb_deal.build_merged_scan("110826001"))
    assert r["ok"] and r["pages"] == 3
    assert store[0][0] == "Комплект_110826001 (2 из 5, нет Расп Акт Отч).pdf"
    assert store[0][1] == b"%PDF-"
    assert trashed == ["id9"]                      # старый единый файл — в корзину
    assert r["missing_labels"] and "Расписка" in r["missing_labels"]


def test_build_merged_aborts_on_unreadable(monkeypatch):
    files = [F("Подп_АГ_Договор_110826001.pdf", 1), F("Подп_ДКП_ТС_110826001.pdf", 2)]
    store, trashed = [], []
    monkeypatch.setattr(cb_deal, "agent", _fake(files, store, trashed,
                                                 {"id1": _pdf(1), "id2": b"garbage"}))
    r = asyncio.run(cb_deal.build_merged_scan("110826001"))
    assert not r["ok"] and not store and not trashed
