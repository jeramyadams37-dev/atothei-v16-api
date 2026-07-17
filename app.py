import gevent.monkey
gevent.monkey.patch_all()

from psycogreen.gevent import patch_psycopg
patch_psycopg()

import os
import secrets
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

connected_users = {}
active_sessions = {}  # token -> username, survives reconnects
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
    
    if data.get('burn'):
        schedule_burn(mid, data['room'], burn_sec)

@socketio.on('react_msg')
def on_react(data):
    new_count = db.add_reaction(data['id'])
    emit('reaction_update', {'id': data['id'], 'reactions': new_count}, to=data['room'])

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

    room = db.consume_invite(code, u)
    if not room:
        emit('channel_error', {'message': 'Invalid or expired invite code.'})
        return

    join_room(room)
    if room not in room_members:
        room_members[room] = set()
    room_members[room].add(request.sid)

    emit('invite_joined', {'room': room})
    emit('history', db.get_history(room))
    broadcast_users()
    broadcast_room_count(room)
    socketio.emit('sys_msg', {'room': room, 'text': f'{u} joined via invite'}, to=room)

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
