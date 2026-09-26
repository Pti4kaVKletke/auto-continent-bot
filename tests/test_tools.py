# -*- coding: utf-8 -*-
"""Скрипты предпросмотра из tools/ должны запускаться: бот их не использует,
поэтому при переделках они ломаются незаметно (так было с preview_invoice.py
до 26.09.2026)."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _run(*args, **env):
    e = dict(os.environ, DB_PATH=str(Path(tempfile.mkdtemp()) / "t.db"), **env)
    r = subprocess.run([sys.executable, *args], cwd=ROOT, env=e,
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    return r.stdout


@pytest.mark.parametrize("variant", ["v1", "v2"])
def test_preview_invoice(variant):
    out = _run("tools/preview_invoice.py", "--no-pdf", "--variant", variant)
    assert f"Собираю вариантом: {variant}" in out
    assert "Счёт_превью_direct.xlsx" in out and "Счёт_превью_corr.xlsx" in out


def test_preview_closing():
    out = _run("tools/preview_closing.py")
    assert "база 4 531 683.17" in out
