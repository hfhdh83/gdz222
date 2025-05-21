import json
import sqlite3
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DB_FILE = "gdz_bot.db"
JSON_FILE = "user_data_gdz_bot.json"

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                username TEXT,
                requests_left INTEGER,
                subscribed_to_channel BOOLEAN,
                referral_code TEXT UNIQUE,
                invited_friends_count INTEGER,
                referred_by TEXT,
                notifications_enabled BOOLEAN,
                requests_at_start_of_day INTEGER DEFAULT 5  -- Added new column
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS referral_map (
                code TEXT PRIMARY KEY,
                user_id TEXT,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS broadcasts (
                broadcast_id TEXT PRIMARY KEY,
                text TEXT,
                media_type TEXT,
                media_id TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS broadcast_clicks (
                broadcast_id TEXT,
                user_id TEXT,
                PRIMARY KEY (broadcast_id, user_id),
                FOREIGN KEY (broadcast_id) REFERENCES broadcasts(broadcast_id),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        ''')
        conn.commit()

def migrate_data():
    try:
        with open(JSON_FILE, 'r', encoding='utf-8') as f:
            json_data = json.load(f)
    except FileNotFoundError:
        logger.error(f"Файл {JSON_FILE} не найден.")
        return
    except json.JSONDecodeError:
        logger.error(f"Ошибка декодирования JSON из {JSON_FILE}.")
        return

    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        for user_id, user in json_data.get("users", {}).items():
            cursor.execute('''
                INSERT OR REPLACE INTO users (
                    user_id, username, requests_left, subscribed_to_channel,
                    referral_code, invited_friends_count, referred_by, 
                    notifications_enabled, requests_at_start_of_day
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                user_id,
                user.get("username", f"User_{user_id}"),
                user.get("requests_left", 5),
                user.get("subscribed_to_channel", False),
                user.get("referral_code", ""),
                user.get("invited_friends_count", 0),
                user.get("referred_by"),
                user.get("notifications_enabled", True),
                user.get("requests_left", 5)  # Initialize with requests_left
            ))
        for code, user_id in json_data.get("referral_map", {}).items():
            cursor.execute('INSERT OR REPLACE INTO referral_map (code, user_id) VALUES (?, ?)',
                          (code, user_id))
        for broadcast_id, broadcast in json_data.get("broadcasts", {}).items():
            media = broadcast.get("media")
            cursor.execute('''
                INSERT OR REPLACE INTO broadcasts (broadcast_id, text, media_type, media_id)
                VALUES (?, ?, ?, ?)
            ''', (
                broadcast_id,
                broadcast.get("text", ""),
                media["type"] if media else None,
                media["id"] if media else None
            ))
            for user_id in broadcast.get("clicks", []):
                cursor.execute('INSERT OR IGNORE INTO broadcast_clicks (broadcast_id, user_id) VALUES (?, ?)',
                              (broadcast_id, user_id))
        conn.commit()
        logger.info("Миграция данных завершена.")

if __name__ == "__main__":
    init_db()
    migrate_data()