# -*- coding: utf-8 -*-
"""
Общая настройка тестов: корень репозитория в sys.path, временная база SQLite,
шаблоны из папки templates. Сеть, Telegram, Google и Claude тестам не нужны —
проверяются только расчёты и сборка документов.

Запуск:  python -m pytest -q
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="ac_tests_"))
os.environ["DB_PATH"] = str(_TMP / "test.db")
os.environ["TEMPLATES_DIR"] = str(ROOT / "templates")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("STRICT_PLACEHOLDERS", "1")

import memory  # noqa: E402

memory.init_db()
