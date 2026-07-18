import gevent.monkey
gevent.monkey.patch_all()

from psycogreen.gevent import patch_psycopg
patch_psycopg()

import os
import secrets
import requests
import socket
import subprocess
import threading
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room
import database as db

app = Flask(__name__)
app.config['SECRET_KEY'] = 'atothei_v16_complete'
MAX_BUFFER = 100 * 1024 * 1024
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent', max_http_buffer_size=MAX_BUFFER)

# Initialize DB at import time so it runs under gunicorn too
db.init_db()
ADMIN_USERNAME = "Jeramy"
ADMIN_ALERTS_ROOM = "🚨 Admin Alerts"
db.ensure_admin_channel(ADMIN_USERNAME, ADMIN_ALERTS_ROOM)

connected_users = {}
active_sessions = {}  # token -> username, survives reconnects
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
user_active_room = {}  # sid -> room currently being viewed

SUSPICIOUS_PATTERNS = [
    "kill you", "i know where you live", "i'll hurt you", "send nudes",
    "your address is", "buy drugs", "sell drugs", "wire me money",
    "gift card code", "child porn", "csam"
]

def check_keywords(text):
    lowered = (text or "").lower()
    return any(p in lowered for p in SUSPICIOUS_PATTERNS)

def call_gemini_moderation(text):
    if not GEMINI_API_KEY:
        return {"action": "allow", "reason": "no API key configured"}
    try:
        prompt = (
            "You are a chat moderation classifier. Given the message below, respond ONLY with JSON: "
            '{"action": "allow" or "suspend", "reason": "short explanation"}. '
            "Use 'suspend' only for genuine harassment, threats, or illegal activity - not mild rudeness or jokes.\n\n"
            f"Message: {text}"
        )
        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}",
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=8
        )
        data = resp.json()
        raw = data["candidates"][0]["content"]["parts"][0]["text"]
        raw = raw.strip().strip("`").replace("json", "", 1).strip()
        import json as _json
        parsed = _json.loads(raw)
        if parsed.get("action") not in ("allow", "suspend"):
            parsed["action"] = "allow"
        return parsed
    except Exception as e:
        return {"action": "allow", "reason": f"moderation check failed: {e}"}

def notify_user(username, ntype, content, room=None):
    db.add_notification(username, ntype, content, room)
    for sid, uname in connected_users.items():
        if uname == username:
            socketio.emit('notification', {'type': ntype, 'content': content, 'room': room}, to=sid)

room_members = {}

def start_public_tunnel():
    print("\n[INIT] Starting Global Tunnel...")
    cmd = "ssh -R 80:localhost:5000 localhost.run -o StrictHostKeyChecking=no -o ServerAliveInterval=60"
    def run_ssh():
        process = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in process.stdout:
            line = line.strip()
            if line:
                print(f"[SSH] {line}")
            if ("localhost.run" in line or "lhr.life" in line) and "http" in line and "docs" not in line:
                try:
                    url = line[line.find("http"):].split(" ")[0]
                    print(f"\n✅ GLOBAL LINK: {url}")
                except:
                    pass
    t = threading.Thread(target=run_ssh)
    t.daemon = True
    t.start()

def schedule_burn(msg_id, room, seconds):
    def do_burn():
        db.delete_message(msg_id)
        socketio.emit('burn_msg', {'id': msg_id, 'room': room}, to=room)
    t = threading.Timer(seconds, do_burn)
    t.daemon = True
    t.start()

def get_all_active_users_data():
    active_names = list(set(connected_users.values()))
    users_data = []
    for name in active_names:
        ava = db.get_user_avatar(name)
        users_data.append({'name': name, 'avatar': ava})
    return users_data

def broadcast_users():
    socketio.emit('update_users', get_all_active_users_data())

def broadcast_room_count(room):
    members = room_members.get(room, set())
    names = list(set(connected_users.get(sid, '') for sid in members if sid in connected_users))
    names = [n for n in names if n]
    socketio.emit('room_users', {'room': room, 'count': len(names), 'names': names}, to=room)

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/api/snap', methods=['POST'])
def snap_api():
    data = request.get_json(silent=True) or {}
    text = str(data.get('text', ''))[:250]
    room = str(data.get('room', 'general'))
    return jsonify({
        'caption': text + ' · via Atothei',
        'attachmentUrl': request.host_url,
        'room': room,
        'status': 'ok'
    })

@socketio.on('disconnect')
def on_disco():
    u = connected_users.get(request.sid, '')
    if request.sid in connected_users:
        del connected_users[request.sid]
    for r in list(room_members.keys()):
        if request.sid in room_members[r]:
            room_members[r].discard(request.sid)
            broadcast_room_count(r)
            if u:
                socketio.emit('sys_msg', {'room': r, 'text': f'{u} left'}, to=r)
    broadcast_users()

@socketio.on('auth')
def handle_auth(data):
    u, p, t = data['user'], data['pass'], data['type']
    success = False
    if t == 'register':
        if db.save_user(u, p):
            success = True
        else:
            emit('auth_response', {'success': False, 'message': 'Username taken'})
    else:
        if db.verify_user(u, p):
            suspended, reason = db.is_suspended(u)
            if suspended:
                emit('auth_response', {'success': False, 'message': f'Account suspended: {reason}'})
                return
            success = True
        else:
            emit('auth_response', {'success': False, 'message': 'Wrong username or password'})
            
    if success:
        connected_users[request.sid] = u
        token = secrets.token_hex(16)
        active_sessions[token] = u
        ava = db.get_user_avatar(u)
        emit('auth_response', {'success': True, 'user': u, 'avatar': ava, 'token': token})
        broadcast_users()

@socketio.on('resume_session')
def handle_resume_session(data):
    token = data.get('token')
    u = active_sessions.get(token)
    if u:
        connected_users[request.sid] = u
        ava = db.get_user_avatar(u)
        emit('resume_response', {'success': True, 'user': u, 'avatar': ava})
        broadcast_users()
    else:
        emit('resume_response', {'success': False})

@socketio.on('join_channel')
def on_join(data):
    r = data['room']
    u = connected_users.get(request.sid, '')

    if db.channel_exists(r) and db.is_channel_private(r):
        if not u or not db.is_member(r, u):
            emit('join_error', {'room': r, 'message': 'This channel is private. You need an invite to join.'})
            return

    join_room(r)
    if r not in room_members:
        room_members[r] = set()
    room_members[r].add(request.sid)
    user_active_room[request.sid] = r
    
    emit('history', db.get_history(r))
    broadcast_users()
    broadcast_room_count(r)
    
    if u:
        socketio.emit('sys_msg', {'room': r, 'text': f'{u} joined'}, to=r)

@socketio.on('leave_channel')
def on_leave(data):
    r = data['room']
    leave_room(r)
    if r in room_members:
        room_members[r].discard(request.sid)
        broadcast_users()
        broadcast_room_count(r)
        u = connected_users.get(request.sid, '')
        if u:
            socketio.emit('sys_msg', {'room': r, 'text': f'{u} left'}, to=r)

@socketio.on('send_msg')
def on_msg(data):
    text = data.get('text') or ''
    sender = data['user']

    if not data.get('isEncrypted') and check_keywords(text):
        verdict = call_gemini_moderation(text)
        db.log_moderation_flag(data['room'], sender, text, verdict.get('reason', ''), verdict.get('action', 'allow'))
        if verdict.get('action') == 'suspend':
            db.suspend_user(sender, verdict.get('reason', 'Flagged by AI moderator'))
            emit('account_suspended', {'reason': verdict.get('reason', 'Flagged by AI moderator')})
            notify_user(
                ADMIN_USERNAME, 'moderation',
                f"Auto-suspended {sender} in #{data['room']}: {verdict.get('reason', '')}",
                ADMIN_ALERTS_ROOM
            )
            return

    ava = db.get_user_avatar(data['user'])
    burn_sec = int(data.get('burnSeconds', 10))
    mid, ts = db.save_message(
        data['room'], data['user'], ava, data.get('text'), 
        data.get('file'), data.get('isEncrypted', False), 
        data.get('burn', False), burn_sec
    )
    
    data['id'] = mid
    data['avatar'] = ava
    data['reactions'] = 0
    data['time'] = ts	
    data['burnSeconds'] = burn_sec
    emit('message', data, to=data['room'])

    preview = (data.get('text') or '')[:60] or '(attachment)'
    for sid, uname in list(connected_users.items()):
        if uname == data['user']:
            continue
        if sid in room_members.get(data['room'], set()) and user_active_room.get(sid) != data['room']:
            notify_user(uname, 'message', f"{data['user']} in #{data['room']}: {preview}", data['room'])
    
    if data.get('burn'):
        schedule_burn(mid, data['room'], burn_sec)

@socketio.on('react_msg')
def on_react(data):
    new_count = db.add_reaction(data['id'])
    emit('reaction_update', {'id': data['id'], 'reactions': new_count}, to=data['room'])
    owner = db.get_message_owner(data['id'])
    reactor = connected_users.get(request.sid, '')
    if owner and reactor and owner != reactor:
        notify_user(owner, 'reaction', f"{reactor} reacted to your message in #{data['room']}", data['room'])

@socketio.on('typing')
def on_typing(data):
    emit('display_typing', data, to=data['room'], include_self=False)

@socketio.on('update_avatar')
def on_ava(data):
    db.update_avatar(data['user'], data['avatar'])
    broadcast_users()

@socketio.on('create_channel')
def on_create_channel(data):
    name = str(data.get('name', '')).strip()
    is_private = 1 if data.get('private', True) else 0
    u = connected_users.get(request.sid, '')

    if not u:
        emit('channel_error', {'message': 'You must be logged in to create a channel.'})
        return
    if not name:
        emit('channel_error', {'message': 'Channel name cannot be empty.'})
        return

    success = db.create_channel(name, u, is_private)
    if success:
        emit('channel_created', {'name': name, 'private': bool(is_private)})
    else:
        emit('channel_error', {'message': f'Channel "{name}" already exists.'})

@socketio.on('generate_invite')
def on_generate_invite(data):
    room = data.get('room')
    max_uses = data.get('maxUses')
    u = connected_users.get(request.sid, '')

    if not u:
        emit('channel_error', {'message': 'You must be logged in to create an invite.'})
        return
    if not db.channel_exists(room) or not db.is_channel_owner(room, u):
        emit('channel_error', {'message': 'Only the channel owner can generate invites.'})
        return

    code = db.create_invite(room, u, max_uses)
    emit('invite_created', {'room': room, 'code': code})

@socketio.on('join_via_invite')
def on_join_via_invite(data):
    code = data.get('code')
    u = connected_users.get(request.sid, '')

    if not u:
        emit('channel_error', {'message': 'You must be logged in to use an invite.'})
        return

    room, created_by = db.consume_invite(code, u)
    if not room:
        emit('channel_error', {'message': 'Invalid or expired invite code.'})
        return

    join_room(room)
    if room not in room_members:
        room_members[room] = set()
    room_members[room].add(request.sid)
    user_active_room[request.sid] = room

    emit('invite_joined', {'room': room})
    emit('history', db.get_history(room))
    broadcast_users()
    broadcast_room_count(room)
    socketio.emit('sys_msg', {'room': room, 'text': f'{u} joined via invite'}, to=room)
    if created_by and created_by != u:
        notify_user(created_by, 'invite', f"{u} joined #{room} using your invite", room)

@socketio.on('get_notifications')
def on_get_notifications():
    u = connected_users.get(request.sid, '')
    if not u:
        return
    emit('notifications_list', {
        'notifications': db.get_notifications(u),
        'unread': db.get_unread_count(u)
    })

@socketio.on('mark_notifications_read')
def on_mark_notifications_read():
    u = connected_users.get(request.sid, '')
    if u:
        db.mark_notifications_read(u)

@socketio.on('admin_clear')
def on_clear(data):
    db.clear_room(data['room'])
    emit('clear_chat', data['room'], to=data['room'])


@socketio.on('delete_channel')
def on_delete_channel(data):
    room = data.get('room')
    u = connected_users.get(request.sid, '')
    if not u: return
    if room == 'Lobby':
        emit('channel_error', {'message': 'Cannot delete the Lobby.'})
        return
    if db.delete_channel(room, u):
        socketio.emit('channel_deleted', {'room': room})
    else:
        emit('channel_error', {'message': 'Only the owner can permanently delete this channel.'})

if __name__ == '__main__':
    db.init_db()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except:
        ip = "127.0.0.1"
        
    print("\n" + "="*44)
    print(" ATOTHEI V16 - MODULARIZED")
    print(" ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f" Local: http://{ip}:5000")
    start_public_tunnel()
    print("="*44 + "\n")
    
    socketio.run(app, host='0.0.0.0', port=5000)
