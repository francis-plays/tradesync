import sqlite3
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), '../../tradesync.db')

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Create all tables if they don't exist."""
    conn = get_connection()
    c = conn.cursor()

    # Channels being monitored
    c.execute('''
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            is_active INTEGER DEFAULT 1,
            is_paused INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Every raw message received from Telegram
    c.execute('''
        CREATE TABLE IF NOT EXISTS raw_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            content TEXT,
            has_image INTEGER DEFAULT 0,
            image_path TEXT,
            received_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(message_id, channel_id)
        )
    ''')

    # Classified signals from the LLM
    c.execute('''
        CREATE TABLE IF NOT EXISTS classified_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id TEXT UNIQUE NOT NULL,
            message_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            classification TEXT NOT NULL,
            confidence TEXT,
            pair TEXT,
            direction TEXT,
            entry REAL,
            sl REAL,
            tp TEXT,
            signal_type TEXT DEFAULT 'market',
            llm_reasoning TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Open positions mapped to signals
    c.execute('''
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id TEXT NOT NULL,
            ctrader_position_id TEXT,
            channel_id TEXT NOT NULL,
            pair TEXT NOT NULL,
            direction TEXT NOT NULL,
            lot_size REAL,
            entry_price REAL,
            sl REAL,
            tp TEXT,
            status TEXT DEFAULT 'open',
            opened_at TEXT DEFAULT CURRENT_TIMESTAMP,
            closed_at TEXT
        )
    ''')

    # Pending approvals waiting for Francis to approve/reject
    c.execute('''
        CREATE TABLE IF NOT EXISTS pending_approvals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id TEXT UNIQUE NOT NULL,
            bot_message_id TEXT,
            expires_at TEXT NOT NULL,
            status TEXT DEFAULT 'waiting',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # System settings (kill switch, risk, etc.)
    c.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Full audit log
    c.execute('''
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            signal_id TEXT,
            channel_id TEXT,
            description TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Seed default settings
    defaults = [
        ('kill_switch', 'off'),
        ('risk_percent', '0.02'),
        ('approval_timeout_minutes', '10'),
    ]
    for key, value in defaults:
        c.execute('''
            INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)
        ''', (key, value))

    conn.commit()
    conn.close()
    print("✅ Database initialised successfully.")


# --- Settings helpers ---

def get_setting(key: str) -> str:
    conn = get_connection()
    row = conn.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
    conn.close()
    return row['value'] if row else None

def set_setting(key: str, value: str):
    conn = get_connection()
    conn.execute('''
        INSERT INTO settings (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
    ''', (key, value, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()

def is_kill_switch_active() -> bool:
    return get_setting('kill_switch') == 'on'


# --- Channel helpers ---

def upsert_channel(telegram_id: str, name: str):
    conn = get_connection()
    conn.execute('''
        INSERT INTO channels (telegram_id, name)
        VALUES (?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET name=excluded.name
    ''', (telegram_id, name))
    conn.commit()
    conn.close()

def is_channel_paused(telegram_id: str) -> bool:
    conn = get_connection()
    row = conn.execute('SELECT is_paused FROM channels WHERE telegram_id = ?', (telegram_id,)).fetchone()
    conn.close()
    return bool(row['is_paused']) if row else False

def set_channel_paused(telegram_id: str, paused: bool):
    conn = get_connection()
    conn.execute('UPDATE channels SET is_paused = ? WHERE telegram_id = ?', (int(paused), telegram_id))
    conn.commit()
    conn.close()


# --- Message helpers ---

def save_raw_message(message_id: str, channel_id: str, content: str,
                     has_image: bool = False, image_path: str = None):
    conn = get_connection()
    try:
        conn.execute('''
            INSERT OR IGNORE INTO raw_messages
            (message_id, channel_id, content, has_image, image_path)
            VALUES (?, ?, ?, ?, ?)
        ''', (message_id, channel_id, content, int(has_image), image_path))
        conn.commit()
    except Exception as e:
        print(f"⚠️ Could not save message: {e}")
    finally:
        conn.close()

def get_last_message_id(channel_id: str) -> str | None:
    conn = get_connection()
    row = conn.execute('''
        SELECT message_id FROM raw_messages
        WHERE channel_id = ?
        ORDER BY received_at DESC LIMIT 1
    ''', (channel_id,)).fetchone()
    conn.close()
    return row['message_id'] if row else None

def get_recent_messages(channel_id: str, limit: int = 20) -> list:
    conn = get_connection()
    rows = conn.execute('''
        SELECT content FROM raw_messages
        WHERE channel_id = ?
        ORDER BY received_at DESC LIMIT ?
    ''', (channel_id, limit)).fetchall()
    conn.close()
    return [r['content'] for r in reversed(rows)]


# --- Signal helpers ---

def save_signal(signal_id: str, message_id: str, channel_id: str,
                classification: str, confidence: str = None, pair: str = None,
                direction: str = None, entry: float = None, sl: float = None,
                tp: str = None, signal_type: str = 'market',
                llm_reasoning: str = None):
    conn = get_connection()
    conn.execute('''
        INSERT OR IGNORE INTO classified_signals
        (signal_id, message_id, channel_id, classification, confidence,
         pair, direction, entry, sl, tp, signal_type, llm_reasoning)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (signal_id, message_id, channel_id, classification, confidence,
          pair, direction, entry, sl, tp, signal_type, llm_reasoning))
    conn.commit()
    conn.close()

def update_signal_status(signal_id: str, status: str):
    conn = get_connection()
    conn.execute('UPDATE classified_signals SET status = ? WHERE signal_id = ?',
                 (status, signal_id))
    conn.commit()
    conn.close()

def is_duplicate_signal(channel_id: str, pair: str, direction: str) -> bool:
    """Check if same pair+direction is already open from this channel."""
    conn = get_connection()
    row = conn.execute('''
        SELECT id FROM positions
        WHERE channel_id = ? AND pair = ? AND direction = ? AND status = 'open'
    ''', (channel_id, pair, direction)).fetchone()
    conn.close()
    return row is not None


# --- Position helpers ---

def save_position(signal_id: str, channel_id: str, pair: str,
                  direction: str, lot_size: float, entry_price: float,
                  sl: float, tp: str, ctrader_position_id: str = None):
    conn = get_connection()
    conn.execute('''
        INSERT INTO positions
        (signal_id, ctrader_position_id, channel_id, pair, direction,
         lot_size, entry_price, sl, tp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (signal_id, ctrader_position_id, channel_id, pair, direction,
          lot_size, entry_price, sl, tp))
    conn.commit()
    conn.close()

def get_open_position_by_pair(channel_id: str, pair: str) -> dict | None:
    conn = get_connection()
    row = conn.execute('''
        SELECT * FROM positions
        WHERE channel_id = ? AND pair = ? AND status = 'open'
        ORDER BY opened_at DESC LIMIT 1
    ''', (channel_id, pair)).fetchone()
    conn.close()
    return dict(row) if row else None

def close_position(signal_id: str):
    conn = get_connection()
    conn.execute('''
        UPDATE positions SET status = 'closed', closed_at = ?
        WHERE signal_id = ?
    ''', (datetime.utcnow().isoformat(), signal_id))
    conn.commit()
    conn.close()


# --- Approval helpers ---

def save_pending_approval(signal_id: str, expires_at: str, bot_message_id: str = None):
    conn = get_connection()
    conn.execute('''
        INSERT OR REPLACE INTO pending_approvals
        (signal_id, bot_message_id, expires_at)
        VALUES (?, ?, ?)
    ''', (signal_id, bot_message_id, expires_at))
    conn.commit()
    conn.close()

def get_pending_approval(signal_id: str) -> dict | None:
    conn = get_connection()
    row = conn.execute('''
        SELECT * FROM pending_approvals WHERE signal_id = ? AND status = 'waiting'
    ''', (signal_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def update_approval_status(signal_id: str, status: str):
    conn = get_connection()
    conn.execute('UPDATE pending_approvals SET status = ? WHERE signal_id = ?',
                 (status, signal_id))
    conn.commit()
    conn.close()


# --- Event log ---

def log_event(event_type: str, description: str,
              signal_id: str = None, channel_id: str = None):
    conn = get_connection()
    conn.execute('''
        INSERT INTO events (event_type, signal_id, channel_id, description)
        VALUES (?, ?, ?, ?)
    ''', (event_type, signal_id, channel_id, description))
    conn.commit()
    conn.close()


if __name__ == '__main__':
    init_db()