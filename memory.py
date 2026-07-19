import sqlite3
import json
import os
from pathlib import Path

DB_PATH = os.environ.get("DB_PATH", "/data/agent.db")

# Сколько дней хранить историю диалога и незавершённые сканы.
# История может содержать паспортные данные / ИНН, поэтому не бесконечно.
HISTORY_RETENTION_DAYS = int(os.environ.get("HISTORY_RETENTION_DAYS", "7"))


def get_conn():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            data TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS bank_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            data TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS instructions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            active INTEGER DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS pending_scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            filepath TEXT NOT NULL,
            original_name TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    conn.close()


# --- Настройки ---

def set_setting(key: str, value: str):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
        (key, value)
    )
    conn.commit()
    conn.close()


def get_setting(key: str, default=None):
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


# --- Компании/реквизиты ---

def save_company(name: str, data: dict):
    conn = get_conn()
    # Обновляем если уже есть с таким именем
    existing = conn.execute("SELECT id FROM companies WHERE name=?", (name,)).fetchone()
    if existing:
        conn.execute("UPDATE companies SET data=? WHERE name=?", (json.dumps(data, ensure_ascii=False), name))
    else:
        conn.execute("INSERT INTO companies (name, data) VALUES (?, ?)", (name, json.dumps(data, ensure_ascii=False)))
    conn.commit()
    conn.close()


def get_company(name: str) -> dict:
    conn = get_conn()
    row = conn.execute("SELECT data FROM companies WHERE name=?", (name,)).fetchone()
    conn.close()
    return json.loads(row["data"]) if row else {}


def list_companies() -> list:
    conn = get_conn()
    rows = conn.execute("SELECT name, created_at FROM companies ORDER BY name").fetchall()
    conn.close()
    return [{"name": r["name"], "created_at": r["created_at"]} for r in rows]


def delete_company(name: str):
    conn = get_conn()
    conn.execute("DELETE FROM companies WHERE name=?", (name,))
    conn.commit()
    conn.close()


# --- Профили банковских реквизитов ---

def save_bank_profile(name: str, data: dict):
    conn = get_conn()
    existing = conn.execute("SELECT id FROM bank_profiles WHERE name=?", (name,)).fetchone()
    if existing:
        conn.execute("UPDATE bank_profiles SET data=? WHERE name=?", (json.dumps(data, ensure_ascii=False), name))
    else:
        conn.execute("INSERT INTO bank_profiles (name, data) VALUES (?, ?)", (name, json.dumps(data, ensure_ascii=False)))
    conn.commit()
    conn.close()


def get_bank_profile(name: str) -> dict:
    conn = get_conn()
    row = conn.execute("SELECT data FROM bank_profiles WHERE name=?", (name,)).fetchone()
    conn.close()
    return json.loads(row["data"]) if row else {}


def list_bank_profiles() -> list:
    conn = get_conn()
    rows = conn.execute("SELECT name FROM bank_profiles ORDER BY name").fetchall()
    conn.close()
    return [r["name"] for r in rows]


def delete_bank_profile(name: str):
    conn = get_conn()
    conn.execute("DELETE FROM bank_profiles WHERE name=?", (name,))
    conn.commit()
    conn.close()


# --- Инструкции ---

def add_instruction(text: str):
    conn = get_conn()
    conn.execute("INSERT INTO instructions (text) VALUES (?)", (text,))
    conn.commit()
    conn.close()


def get_instructions() -> list:
    conn = get_conn()
    rows = conn.execute("SELECT id, text FROM instructions WHERE active=1 ORDER BY id").fetchall()
    conn.close()
    return [{"id": r["id"], "text": r["text"]} for r in rows]


def delete_instruction(instruction_id: int):
    conn = get_conn()
    conn.execute("UPDATE instructions SET active=0 WHERE id=?", (instruction_id,))
    conn.commit()
    conn.close()


# --- История диалога ---

def add_to_history(role: str, content: str):
    conn = get_conn()
    conn.execute("INSERT INTO history (role, content) VALUES (?, ?)", (role, content))
    # Оставляем только последние 50 сообщений
    conn.execute("""
        DELETE FROM history WHERE id NOT IN (
            SELECT id FROM history ORDER BY id DESC LIMIT 50
        )
    """)
    # Удаляем записи старше HISTORY_RETENTION_DAYS (могут содержать паспортные данные, ИНН и т.п.)
    conn.execute(
        "DELETE FROM history WHERE created_at < datetime('now', ?)",
        (f"-{HISTORY_RETENTION_DAYS} days",),
    )
    conn.commit()
    conn.close()


def get_history(limit: int = 20) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT role, content FROM history ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def clear_history():
    conn = get_conn()
    conn.execute("DELETE FROM history")
    conn.commit()
    conn.close()


# --- Сканы документов, ожидающие привязки к сделке ---

def add_pending_scan(chat_id: str, filepath: str, original_name: str):
    conn = get_conn()
    conn.execute(
        "INSERT INTO pending_scans (chat_id, filepath, original_name) VALUES (?, ?, ?)",
        (str(chat_id), filepath, original_name)
    )
    conn.commit()
    conn.close()


def get_pending_scans(chat_id: str) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, filepath, original_name FROM pending_scans WHERE chat_id=? ORDER BY id",
        (str(chat_id),)
    ).fetchall()
    conn.close()
    return [{"id": r["id"], "filepath": r["filepath"], "original_name": r["original_name"]} for r in rows]


def clear_pending_scans(chat_id: str):
    """Удаляет записи и сами файлы для данного chat_id (после успешной загрузки в Drive)."""
    scans = get_pending_scans(chat_id)
    for s in scans:
        try:
            p = Path(s["filepath"])
            if p.exists():
                p.unlink()
        except Exception:
            pass

    conn = get_conn()
    conn.execute("DELETE FROM pending_scans WHERE chat_id=?", (str(chat_id),))
    conn.commit()
    conn.close()


def cleanup_old_pending_scans(max_age_days: int = 7):
    """Удаляет старые незавершённые сканы (старше max_age_days) — вызывать при старте бота."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, filepath FROM pending_scans WHERE created_at < datetime('now', ?)",
        (f"-{max_age_days} days",)
    ).fetchall()

    for r in rows:
        try:
            p = Path(r["filepath"])
            if p.exists():
                p.unlink()
        except Exception:
            pass

    conn.execute("DELETE FROM pending_scans WHERE created_at < datetime('now', ?)", (f"-{max_age_days} days",))
    conn.commit()
    conn.close()
