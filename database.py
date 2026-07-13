import os
import datetime
import hashlib
import secrets
import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ.get("DATABASE_URL")

def get_conn():
    return psycopg2.connect(DATABASE_URL)

def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def init_db():
    with get_conn() as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, password TEXT, avatar_data TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS messages (id SERIAL PRIMARY KEY, room TEXT, "user" TEXT, avatar TEXT, text TEXT, file_name TEXT, file_type TEXT, file_data TEXT, is_encrypted INTEGER, reactions INTEGER DEFAULT 0, burn INTEGER DEFAULT 0, burn_seconds INTEGER DEFAULT 10, timestamp TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS channels (name TEXT PRIMARY KEY, owner TEXT, is_private INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        c.execute('''CREATE TABLE IF NOT EXISTS channel_members (channel TEXT, username TEXT, PRIMARY KEY (channel, username))''')
        c.execute('''CREATE TABLE IF NOT EXISTS invites (code TEXT PRIMARY KEY, channel TEXT, created_by TEXT, max_uses INTEGER, uses INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        conn.commit()

def save_user(user, pwd):
    try:
        with get_conn() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO users (username, password, avatar_data) VALUES (%s, %s, %s)", (user, hash_pw(pwd), ""))
            conn.commit()
        return True
    except psycopg2.IntegrityError:
        return False

def verify_user(user, pwd):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT password FROM users WHERE username = %s", (user,))
        row = c.fetchone()
        return row and row[0] == hash_pw(pwd)

def update_avatar(user, avatar_data):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE users SET avatar_data = %s WHERE username = %s", (avatar_data, user))
        conn.commit()

def get_user_avatar(user):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT avatar_data FROM users WHERE username = %s", (user,))
        row = c.fetchone()
        return row[0] if row else ""

def save_message(room, user, avatar, text, file_data=None, is_enc=0, is_burn=0, burn_seconds=10):
    fname = file_data['name'] if file_data else None
    ftype = file_data['type'] if file_data else None
    fdata = file_data['data'] if file_data else None
    ts = datetime.datetime.now().strftime("%I:%M %p")

    with get_conn() as conn:
        c = conn.cursor()
        c.execute(
            'INSERT INTO messages (room, "user", avatar, text, file_name, file_type, file_data, is_encrypted, burn, burn_seconds, timestamp) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id',
            (room, user, avatar, text, fname, ftype, fdata, 1 if is_enc else 0, 1 if is_burn else 0, burn_seconds, ts)
        )
        new_id = c.fetchone()[0]
        conn.commit()
        return new_id, ts

def delete_message(msg_id):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM messages WHERE id = %s", (msg_id,))
        conn.commit()

def add_reaction(msg_id):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE messages SET reactions = reactions + 1 WHERE id = %s", (msg_id,))
        c.execute("SELECT reactions FROM messages WHERE id = %s", (msg_id,))
        row = c.fetchone()
        conn.commit()
        return row[0] if row else 0

def clear_room(room):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM messages WHERE room = %s", (room,))
        conn.commit()

def get_history(room):
    history = []
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(
            'SELECT id, "user", avatar, text, file_name, file_type, file_data, is_encrypted, reactions, burn, burn_seconds, timestamp FROM messages WHERE room = %s ORDER BY id DESC LIMIT 50',
            (room,)
        )
        rows = c.fetchall()
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
        with get_conn() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO channels (name, owner, is_private) VALUES (%s, %s, %s)", (name, owner, 1 if is_private else 0))
            c.execute("INSERT INTO channel_members (channel, username) VALUES (%s, %s) ON CONFLICT DO NOTHING", (name, owner))
            conn.commit()
        return True
    except psycopg2.IntegrityError:
        return False

def channel_exists(name):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT 1 FROM channels WHERE name = %s", (name,))
        return c.fetchone() is not None

def is_channel_private(name):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT is_private FROM channels WHERE name = %s", (name,))
        row = c.fetchone()
        return bool(row[0]) if row else False

def is_channel_owner(name, username):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT owner FROM channels WHERE name = %s", (name,))
        row = c.fetchone()
        return row and row[0] == username

def is_member(channel, username):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT 1 FROM channel_members WHERE channel = %s AND username = %s", (channel, username))
        return c.fetchone() is not None

def add_member(channel, username):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO channel_members (channel, username) VALUES (%s, %s) ON CONFLICT DO NOTHING", (channel, username))
        conn.commit()

def create_invite(channel, created_by, max_uses=None):
    code = secrets.token_urlsafe(6)
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO invites (code, channel, created_by, max_uses) VALUES (%s, %s, %s, %s)", (code, channel, created_by, max_uses))
        conn.commit()
    return code

def consume_invite(code, username):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT channel, max_uses, uses FROM invites WHERE code = %s", (code,))
        row = c.fetchone()
        if not row:
            return None
        channel, max_uses, uses = row
        if max_uses is not None and uses >= max_uses:
            return None
        c.execute("UPDATE invites SET uses = uses + 1 WHERE code = %s", (code,))
        c.execute("INSERT INTO channel_members (channel, username) VALUES (%s, %s) ON CONFLICT DO NOTHING", (channel, username))
        conn.commit()
        return channel

def delete_channel(name, username):
    if not is_channel_owner(name, username):
        return False
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM channels WHERE name = %s", (name,))
        c.execute("DELETE FROM channel_members WHERE channel = %s", (name,))
        c.execute("DELETE FROM invites WHERE channel = %s", (name,))
        c.execute("DELETE FROM messages WHERE room = %s", (name,))
        conn.commit()
    return True
