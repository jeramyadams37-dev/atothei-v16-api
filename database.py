import sqlite3
import datetime
import hashlib
import secrets

DB_FILE = "atothei.db"

def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, password TEXT, avatar_data TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, room TEXT, user TEXT, avatar TEXT, text TEXT, file_name TEXT, file_type TEXT, file_data TEXT, is_encrypted INTEGER, reactions INTEGER DEFAULT 0, burn INTEGER DEFAULT 0, burn_seconds INTEGER DEFAULT 10, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
        c.execute('''CREATE TABLE IF NOT EXISTS channels (name TEXT PRIMARY KEY, owner TEXT, is_private INTEGER DEFAULT 0, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
        c.execute('''CREATE TABLE IF NOT EXISTS channel_members (channel TEXT, username TEXT, PRIMARY KEY (channel, username))''')
        c.execute('''CREATE TABLE IF NOT EXISTS invites (code TEXT PRIMARY KEY, channel TEXT, created_by TEXT, max_uses INTEGER, uses INTEGER DEFAULT 0, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
        conn.commit()

def save_user(user, pwd):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute("INSERT INTO users (username, password, avatar_data) VALUES (?, ?, ?)", (user, hash_pw(pwd), ""))
        return True
    except sqlite3.IntegrityError:
        return False

def verify_user(user, pwd):
    with sqlite3.connect(DB_FILE) as conn:
        row = conn.execute("SELECT password FROM users WHERE username = ?", (user,)).fetchone()
        return row and row[0] == hash_pw(pwd)

def update_avatar(user, avatar_data):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("UPDATE users SET avatar_data = ? WHERE username = ?", (avatar_data, user))

def get_user_avatar(user):
    with sqlite3.connect(DB_FILE) as conn:
        row = conn.execute("SELECT avatar_data FROM users WHERE username = ?", (user,)).fetchone()
        return row[0] if row else ""

def save_message(room, user, avatar, text, file_data=None, is_enc=0, is_burn=0, burn_seconds=10):
    fname = file_data['name'] if file_data else None
    ftype = file_data['type'] if file_data else None
    fdata = file_data['data'] if file_data else None
    ts = datetime.datetime.now().strftime("%I:%M %p")
    
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.execute(
            "INSERT INTO messages (room, user, avatar, text, file_name, file_type, file_data, is_encrypted, burn, burn_seconds, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (room, user, avatar, text, fname, ftype, fdata, 1 if is_enc else 0, 1 if is_burn else 0, burn_seconds, ts)
        )
        return cur.lastrowid, ts

def delete_message(msg_id):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("DELETE FROM messages WHERE id = ?", (msg_id,))

def add_reaction(msg_id):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("UPDATE messages SET reactions = reactions + 1 WHERE id = ?", (msg_id,))
        row = conn.execute("SELECT reactions FROM messages WHERE id = ?", (msg_id,)).fetchone()
        return row[0] if row else 0

def clear_room(room):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("DELETE FROM messages WHERE room = ?", (room,))

def get_history(room):
    history = []
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.execute(
            "SELECT id, user, avatar, text, file_name, file_type, file_data, is_encrypted, reactions, burn, burn_seconds, timestamp FROM messages WHERE room = ? ORDER BY id DESC LIMIT 50",
            (room,)
        )
        rows = cur.fetchall()
        for r in reversed(rows):
            msg = {
                'id': r[0], 'user': r[1], 'avatar': r[2], 'text': r[3], 'isEncrypted': r[7], 
                'reactions': r[8], 'burn': r[9], 'burnSeconds': r[10], 'time': r[11], 'room': room
            }
            if r[4]:
                msg['file'] = {'name': r[4], 'type': r[5], 'data': r[6]}
            history.append(msg)
    return history


def create_channel(name, owner, is_private=1):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute("INSERT INTO channels (name, owner, is_private) VALUES (?, ?, ?)", (name, owner, 1 if is_private else 0))
            conn.execute("INSERT OR IGNORE INTO channel_members (channel, username) VALUES (?, ?)", (name, owner))
        return True
    except sqlite3.IntegrityError:
        return False

def channel_exists(name):
    with sqlite3.connect(DB_FILE) as conn:
        row = conn.execute("SELECT 1 FROM channels WHERE name = ?", (name,)).fetchone()
        return row is not None

def is_channel_private(name):
    with sqlite3.connect(DB_FILE) as conn:
        row = conn.execute("SELECT is_private FROM channels WHERE name = ?", (name,)).fetchone()
        return bool(row[0]) if row else False

def is_channel_owner(name, username):
    with sqlite3.connect(DB_FILE) as conn:
        row = conn.execute("SELECT owner FROM channels WHERE name = ?", (name,)).fetchone()
        return row and row[0] == username

def is_member(channel, username):
    with sqlite3.connect(DB_FILE) as conn:
        row = conn.execute("SELECT 1 FROM channel_members WHERE channel = ? AND username = ?", (channel, username)).fetchone()
        return row is not None

def add_member(channel, username):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("INSERT OR IGNORE INTO channel_members (channel, username) VALUES (?, ?)", (channel, username))

def create_invite(channel, created_by, max_uses=None):
    code = secrets.token_urlsafe(6)
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("INSERT INTO invites (code, channel, created_by, max_uses) VALUES (?, ?, ?, ?)", (code, channel, created_by, max_uses))
    return code

def consume_invite(code, username):
    with sqlite3.connect(DB_FILE) as conn:
        row = conn.execute("SELECT channel, max_uses, uses FROM invites WHERE code = ?", (code,)).fetchone()
        if not row:
            return None
        channel, max_uses, uses = row
        if max_uses is not None and uses >= max_uses:
            return None
        conn.execute("UPDATE invites SET uses = uses + 1 WHERE code = ?", (code,))
        conn.execute("INSERT OR IGNORE INTO channel_members (channel, username) VALUES (?, ?)", (channel, username))
        return channel
