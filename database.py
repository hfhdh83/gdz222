import sqlite3
import json

class Database:
    def __init__(self, db_file):
        self.db_file = db_file
        self.init_db()

    def init_db(self):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            # Users table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    referral_code TEXT UNIQUE,
                    referred_by TEXT,
                    requests_left INTEGER DEFAULT 0,
                    subscribed_to_channel BOOLEAN DEFAULT FALSE,
                    notifications_enabled BOOLEAN DEFAULT TRUE,
                    invited_friends_count INTEGER DEFAULT 0,
                    requests_at_start_of_day INTEGER DEFAULT 5  -- Add this line
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS broadcasts (
                    broadcast_id TEXT PRIMARY KEY,
                    text TEXT,
                    media TEXT
                )
            ''')
            # Broadcast clicks table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS broadcast_clicks (
                    broadcast_id TEXT,
                    user_id TEXT,
                    PRIMARY KEY (broadcast_id, user_id)
                )
            ''')
            # Settings table for referral bonuses
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value INTEGER
                )
            ''')
            # Initialize default referral settings
            cursor.execute('''
                INSERT OR IGNORE INTO settings (key, value)
                VALUES ('referral_requests', 10), ('bulk_referral_requests', 100)
            ''')
            # Migration: Add media column to broadcasts if missing
            cursor.execute("PRAGMA table_info(broadcasts)")
            columns = [col[1] for col in cursor.fetchall()]
            if 'media' not in columns:
                cursor.execute('ALTER TABLE broadcasts ADD COLUMN media TEXT')
                print("Added 'media' column to broadcasts table")
            conn.commit()

    def create_user(self, user_id, username, referral_code, referred_by=None, requests_left=0):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO users (user_id, username, referral_code, referred_by, requests_left,
                                 subscribed_to_channel, notifications_enabled, invited_friends_count,
                                 requests_at_start_of_day)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (user_id, username, referral_code, referred_by, requests_left, False, True, 0, requests_left))
            conn.commit()
        return self.get_user(user_id)

    def get_user(self, user_id):
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
            user = cursor.fetchone()
            return dict(user) if user else None

    def update_user(self, user_id, updates):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            set_clause = ', '.join(f'{k} = ?' for k in updates.keys())
            values = list(updates.values()) + [user_id]
            cursor.execute(f'UPDATE users SET {set_clause} WHERE user_id = ?', values)
            conn.commit()

    def get_all_user_ids(self):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT user_id FROM users')
            return [str(row[0]) for row in cursor.fetchall()]

    def get_referral_map(self):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT referral_code, user_id FROM users')
            return {row[0]: str(row[1]) for row in cursor.fetchall()}

    def add_broadcast(self, broadcast_id, text, media):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            media_json = json.dumps(media) if media else None
            cursor.execute('''
                INSERT INTO broadcasts (broadcast_id, text, media)
                VALUES (?, ?, ?)
            ''', (broadcast_id, text, media_json))
            conn.commit()

    def get_broadcasts(self):
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM broadcasts')
            broadcasts = {}
            for row in cursor.fetchall():
                broadcast_id = row['broadcast_id']
                media = json.loads(row['media']) if row['media'] else None
                cursor.execute('SELECT user_id FROM broadcast_clicks WHERE broadcast_id = ?', (broadcast_id,))
                clicks = [row[0] for row in cursor.fetchall()]
                broadcasts[broadcast_id] = {
                    'text': row['text'],
                    'media': media,
                    'clicks': clicks
                }
            return broadcasts

    def add_broadcast_click(self, broadcast_id, user_id):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR IGNORE INTO broadcast_clicks (broadcast_id, user_id)
                VALUES (?, ?)
            ''', (broadcast_id, user_id))
            conn.commit()

    def get_referral_settings(self):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT key, value FROM settings
                WHERE key IN ('referral_requests', 'bulk_referral_requests')
            ''')
            settings = {'referral_requests': 10, 'bulk_referral_requests': 100}
            for key, value in cursor.fetchall():
                settings[key] = value
            return settings

    def update_referral_settings(self, updates):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            for key, value in updates.items():
                cursor.execute('''
                    INSERT OR REPLACE INTO settings (key, value)
                    VALUES (?, ?)
                ''', (key, value))
            conn.commit()