# app_web.py
"""
╔══════════════════════════════════════════════════════════════╗
║          🚀 ULTRA FAST TRANSFER WEB  v1.0                    ║
║     Web Interface → Download → GoFile.io                     ║
╚══════════════════════════════════════════════════════════════╝
"""

import os
import re
import time
import queue
import shutil
import uuid
import mimetypes
import logging
import json
import io
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import wraps
import threading

import requests
import psutil
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt

# ══════════════════════════════════════════════════════════════════════════════
#  ⚙️  CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

app = Flask(__name__)
app.secret_key = 'your-secret-key-here-change-in-production'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///users.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['PERMANENT_SESSION_LIFETIME'] = 86400
app.config['SESSION_COOKIE_SECURE'] = False
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

db = SQLAlchemy(app)
bcrypt = Bcrypt(app)

GOFILE_API_TOKEN = None
TEMP_DOWNLOAD_DIR = "temp_downloads"
DOWNLOAD_CHUNK_SIZE = 4 * 1024 * 1024
PROGRESS_INTERVAL = 1.0
GOFILE_API_BASE = "https://api.gofile.io"

# ══════════════════════════════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
#  DATABASE MODELS
# ══════════════════════════════════════════════════════════════════════════════

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    gofile_token = db.Column(db.String(200), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_admin = db.Column(db.Boolean, default=False)
    tasks = db.relationship('Task', backref='user', lazy=True)

class Task(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    url = db.Column(db.String(500), nullable=False)
    source_type = db.Column(db.String(50), nullable=False)
    filename = db.Column(db.String(200), nullable=True)
    file_size = db.Column(db.BigInteger, default=0)
    status = db.Column(db.String(50), default='queued')
    progress = db.Column(db.Float, default=0.0)
    downloaded_bytes = db.Column(db.BigInteger, default=0)
    uploaded_bytes = db.Column(db.BigInteger, default=0)
    speed = db.Column(db.Float, default=0.0)
    eta = db.Column(db.String(50), default='∞')
    gofile_link = db.Column(db.String(500), nullable=True)
    error_message = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)

# ══════════════════════════════════════════════════════════════════════════════
#  UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def fmt_size(b: float) -> str:
    if b == 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(b) < 1024:
            return f"{b:.2f} {unit}"
        b /= 1024
    return f"{b:.2f} PB"

def fmt_speed(bps: float) -> str:
    return fmt_size(bps) + "/s"

def fmt_eta(seconds: float) -> str:
    if seconds < 0 or seconds > 86400 or seconds == float('inf'):
        return "∞"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"

def safe_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", name).strip()

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

DOWNLOADABLE_EXT = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v",
    ".mp3", ".wav", ".flac", ".aac", ".ogg", ".opus", ".m4a",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".zst",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp",
    ".exe", ".dmg", ".apk", ".ipa", ".deb", ".rpm",
    ".txt", ".csv", ".json", ".xml", ".iso", ".img",
}

# ══════════════════════════════════════════════════════════════════════════════
#  AUTH DECORATORS
# ══════════════════════════════════════════════════════════════════════════════

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'Please login first'}), 401
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'Please login first'}), 401
        user = User.query.get(session['user_id'])
        if not user or not user.is_admin:
            return jsonify({'error': 'Admin access required'}), 403
        return f(*args, **kwargs)
    return decorated_function

# ══════════════════════════════════════════════════════════════════════════════
#  MEDIAFIRE RESOLVER
# ══════════════════════════════════════════════════════════════════════════════

def extract_mediafire_direct(url: str) -> str:
    try:
        r = requests.get(url, headers={"User-Agent": _UA}, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")

        btn = soup.find("a", {"id": "downloadButton"})
        if btn and btn.get("href", "").startswith("http"):
            return btn["href"]

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("http") and any(
                href.lower().endswith(ext) for ext in DOWNLOADABLE_EXT
            ):
                return href

        script_patterns = [
            r'"direct_download_url"\s*:\s*"([^"]+)"',
            r'window\.location\.href\s*=\s*"([^"]+)"',
            r'<a[^>]+href="([^"]+)"[^>]*>Download</a>',
        ]
        
        for pattern in script_patterns:
            m = re.search(pattern, r.text)
            if m:
                url = m.group(1).replace("\\u0026", "&")
                if url.startswith("http"):
                    return url

        raise ValueError("Could not extract direct download link")
    except Exception as e:
        log.error(f"MediaFire extraction failed: {e}")
        raise

# ══════════════════════════════════════════════════════════════════════════════
#  DOWNLOAD ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def _resolve_url(url: str):
    headers = {"User-Agent": _UA, "Accept": "*/*"}
    final_url = url
    file_size = 0
    filename = None
    
    try:
        r = requests.head(url, headers=headers, allow_redirects=True, timeout=30)
        final_url = r.url
        if 'content-length' in r.headers:
            file_size = int(r.headers.get('content-length', 0))
        cd = r.headers.get('content-disposition', '')
        if cd:
            m = re.search(r'filename\*?=(?:UTF-8\'\')?(?:"([^"]+)"|([^\s;]+))', cd, re.I)
            if m:
                filename = safe_filename(m.group(1) or m.group(2))
    except Exception as e:
        log.warning(f"HEAD request failed: {e}")

    if not filename:
        path = urlparse(url).path
        filename = os.path.basename(path) or f"file_{uuid.uuid4().hex[:8]}"
        if "." not in filename:
            try:
                r = requests.head(url, headers=headers, timeout=10)
                if 'content-type' in r.headers:
                    ct = r.headers.get('content-type', 'application/octet-stream')
                    ext = mimetypes.guess_extension(ct.split(';')[0].strip()) or '.bin'
                    filename += ext
            except Exception:
                filename += '.bin'
        filename = safe_filename(filename)

    return final_url, filename, file_size

def _stream_download(url: str, out_dir: str, filename: str, task_id: int, callback=None) -> str:
    out_path = os.path.join(out_dir, filename)
    os.makedirs(out_dir, exist_ok=True)
    
    log.info(f"Starting download: {filename} -> {out_path}")
    
    session = requests.Session()
    session.headers.update({"User-Agent": _UA})
    
    try:
        r = session.get(url, stream=True, timeout=120)
        r.raise_for_status()
        total = int(r.headers.get('content-length', 0))
        log.info(f"Content-Length: {total} bytes")
        
        with app.app_context():
            task = db.session.get(Task, task_id)
            if task:
                task.file_size = total
                task.status = 'downloading'
                db.session.commit()
        
        with open(out_path, "wb") as f:
            downloaded = 0
            start_time = time.time()
            last_update = time.time()
            
            for chunk in r.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    
                    now = time.time()
                    if now - last_update >= 0.5:
                        last_update = now
                        
                        with app.app_context():
                            task = db.session.get(Task, task_id)
                            if task:
                                task.downloaded_bytes = downloaded
                                if total > 0:
                                    task.progress = (downloaded / total) * 100
                                task.status = 'downloading'
                                
                                elapsed = now - start_time
                                if elapsed > 0 and downloaded > 0:
                                    task.speed = downloaded / elapsed
                                    if total > 0 and task.speed > 0:
                                        remaining = (total - downloaded) / task.speed
                                        task.eta = fmt_eta(remaining)
                                
                                task.updated_at = datetime.utcnow()
                                db.session.commit()
                                
                                if callback:
                                    callback(task_id, {
                                        'progress': task.progress,
                                        'downloaded': downloaded,
                                        'total': total,
                                        'speed': task.speed,
                                        'eta': task.eta,
                                        'status': 'downloading'
                                    })
        
        log.info(f"Download complete: {out_path}")
        return out_path
        
    except Exception as e:
        log.error(f"Download failed: {e}")
        raise
    finally:
        session.close()

# ══════════════════════════════════════════════════════════════════════════════
#  GOFILE UPLOAD
# ══════════════════════════════════════════════════════════════════════════════

def _gofile_get_server() -> str:
    try:
        r = requests.get(f"{GOFILE_API_BASE}/servers", timeout=15)
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "ok":
            raise RuntimeError(f"GoFile server fetch failed: {data}")
        servers = data["data"]["servers"]
        if not servers:
            raise RuntimeError("No servers available")
        best = sorted(servers, key=lambda s: s.get("load", 0))[0]
        return best["name"]
    except Exception as e:
        log.error(f"GoFile server fetch failed: {e}")
        raise

def _gofile_upload(file_path: str, filename: str, token: str = None, task_id: int = None, callback=None) -> str:
    import ssl
    import http.client
    
    file_size = os.path.getsize(file_path)
    server = _gofile_get_server()
    
    log.info(f"Uploading to {server}.gofile.io: {filename} ({file_size} bytes)")
    
    CHUNK = 8 * 1024 * 1024
    boundary = uuid.uuid4().hex
    
    part_head = (
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; "
        f"name=\"file\"; filename=\"{filename}\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    part_tail = f"\r\n--{boundary}--\r\n".encode()
    total_len = len(part_head) + file_size + len(part_tail)
    
    extra_headers = {}
    if token:
        extra_headers["Authorization"] = f"Bearer {token}"
    
    ctx = ssl.create_default_context()
    conn = http.client.HTTPSConnection(
        f"{server}.gofile.io", 443,
        context=ctx,
        timeout=600,
    )
    
    try:
        class _Body:
            def __init__(self):
                self._buf = io.BytesIO(part_head)
                self._f = open(file_path, "rb")
                self._tail = io.BytesIO(part_tail)
                self._sent = 0
                self._phase = 0
                self._last_update = time.time()
                
            def read(self, n=CHUNK):
                if self._phase == 0:
                    data = self._buf.read(n)
                    if data:
                        return data
                    self._buf.close()
                    self._phase = 1
                    
                if self._phase == 1:
                    data = self._f.read(CHUNK)
                    if data:
                        self._sent += len(data)
                        now = time.time()
                        if now - self._last_update >= 0.5:
                            self._last_update = now
                            with app.app_context():
                                task = db.session.get(Task, task_id)
                                if task:
                                    task.uploaded_bytes = self._sent
                                    if file_size > 0:
                                        task.progress = (self._sent / file_size) * 100
                                    task.status = 'uploading'
                                    task.updated_at = datetime.utcnow()
                                    db.session.commit()
                                    if callback:
                                        callback(task_id, {
                                            'upload_progress': task.progress,
                                            'uploaded': self._sent,
                                            'total': file_size,
                                            'status': 'uploading'
                                        })
                        return data
                    self._f.close()
                    self._phase = 2
                    
                if self._phase == 2:
                    data = self._tail.read(n)
                    if data:
                        return data
                    self._tail.close()
                    self._phase = 3
                    
                return b""
        
        body_obj = _Body()
        conn.request(
            "POST",
            "/contents/uploadfile",
            body=body_obj,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(total_len),
                **extra_headers,
            },
        )
        
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace")
        if resp.status not in (200, 201):
            raise RuntimeError(f"GoFile HTTP {resp.status}: {body[:400]}")
        
        result = json.loads(body)
        if result.get("status") != "ok":
            raise RuntimeError(f"GoFile upload error: {result}")
        
        download_page = result["data"]["downloadPage"]
        log.info(f"Upload complete: {download_page}")
        return download_page
        
    except Exception as e:
        log.error(f"Upload failed: {e}")
        raise
    finally:
        try:
            conn.close()
        except:
            pass

# ══════════════════════════════════════════════════════════════════════════════
#  TASK PROCESSING
# ══════════════════════════════════════════════════════════════════════════════

task_queues = {}
active_tasks = {}
task_threads = {}
task_lock = threading.Lock()

def process_user_task(user_id):
    """Process tasks for a specific user in queue order"""
    log.info(f"Task processor started for user {user_id}")
    
    while True:
        with task_lock:
            if user_id not in task_queues or task_queues[user_id].empty():
                log.info(f"No more tasks for user {user_id}, stopping processor")
                if user_id in active_tasks:
                    del active_tasks[user_id]
                break
            
            task_id = task_queues[user_id].get()
            active_tasks[user_id] = task_id
        
        log.info(f"Processing task {task_id} for user {user_id}")
        
        try:
            process_single_task(task_id)
        except Exception as e:
            log.error(f"Error processing task {task_id}: {e}", exc_info=True)
            with app.app_context():
                task = db.session.get(Task, task_id)
                if task:
                    task.status = 'failed'
                    task.error_message = str(e)
                    db.session.commit()
        
        with task_lock:
            if user_id in active_tasks:
                del active_tasks[user_id]
        
        with task_lock:
            if user_id not in task_queues or task_queues[user_id].empty():
                log.info(f"No more tasks for user {user_id}")
                break

def process_single_task(task_id):
    """Process a single task: download → upload to GoFile"""
    with app.app_context():
        task = db.session.get(Task, task_id)
        if not task:
            log.error(f"Task {task_id} not found")
            return
    
    file_path = None
    actual_filename = None
    
    try:
        log.info(f"Processing task {task_id}: {task.url}")
        
        with app.app_context():
            task.status = 'downloading'
            task.filename = 'Resolving...'
            db.session.commit()
        
        os.makedirs(TEMP_DOWNLOAD_DIR, exist_ok=True)
        
        if task.source_type == 'mediafire':
            log.info(f"Extracting MediaFire URL: {task.url}")
            direct_url = extract_mediafire_direct(task.url)
        else:
            direct_url = task.url
        
        log.info(f"Resolved URL: {direct_url[:100]}...")
        
        with app.app_context():
            task.filename = 'Resolving file info...'
            db.session.commit()
        
        final_url, filename, file_size = _resolve_url(direct_url)
        actual_filename = filename
        log.info(f"File: {filename}, Size: {file_size} bytes")
        
        with app.app_context():
            task.filename = filename  # Set the actual filename here
            if file_size > 0:
                task.file_size = file_size
            db.session.commit()
        
        def download_callback(tid, data):
            with app.app_context():
                task = db.session.get(Task, tid)
                if task:
                    task.downloaded_bytes = data.get('downloaded', 0)
                    task.progress = data.get('progress', 0)
                    task.speed = data.get('speed', 0)
                    task.eta = data.get('eta', '∞')
                    task.status = data.get('status', 'downloading')
                    db.session.commit()
        
        log.info(f"Starting download: {filename}")
        file_path = _stream_download(final_url, TEMP_DOWNLOAD_DIR, filename, task_id, download_callback)
        log.info(f"Download complete: {file_path}")
        
        if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
            raise RuntimeError("Downloaded file is empty or missing")
        
        with app.app_context():
            user = db.session.get(User, task.user_id)
            token = user.gofile_token if user and user.gofile_token else None
        
        with app.app_context():
            task.status = 'uploading'
            task.progress = 0
            task.uploaded_bytes = 0
            db.session.commit()
        
        def upload_callback(tid, data):
            with app.app_context():
                task = db.session.get(Task, tid)
                if task:
                    task.progress = data.get('upload_progress', 0)
                    task.uploaded_bytes = data.get('uploaded', 0)
                    task.status = data.get('status', 'uploading')
                    db.session.commit()
        
        log.info(f"Starting upload to GoFile: {filename}")
        download_page = _gofile_upload(file_path, filename, token, task_id, upload_callback)
        
        # IMPORTANT: Update task as completed with all fields
        with app.app_context():
            task = db.session.get(Task, task_id)
            if task:
                task.gofile_link = download_page
                task.status = 'completed'
                task.progress = 100
                task.uploaded_bytes = task.file_size
                task.filename = filename  # Ensure filename is set
                task.completed_at = datetime.utcnow()
                task.updated_at = datetime.utcnow()
                db.session.commit()
                log.info(f"Task {task_id} marked as completed in database with filename: {filename}")
        
        log.info(f"Task {task_id} completed successfully: {download_page}")
        
    except Exception as e:
        log.error(f"Task {task_id} failed: {e}", exc_info=True)
        with app.app_context():
            task = db.session.get(Task, task_id)
            if task:
                task.status = 'failed'
                task.error_message = str(e)
                if actual_filename:
                    task.filename = actual_filename
                db.session.commit()
        raise
    finally:
        try:
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
                log.info(f"Cleaned up: {file_path}")
        except Exception as e:
            log.warning(f"Cleanup failed: {e}")

def start_task_processor(user_id):
    """Start the task processor thread for a user if not already running"""
    with task_lock:
        if user_id in task_threads and task_threads[user_id] and task_threads[user_id].is_alive():
            log.info(f"Task processor already running for user {user_id}")
            return
        
        log.info(f"Starting task processor for user {user_id}")
        thread = threading.Thread(target=process_user_task, args=(user_id,))
        thread.daemon = True
        thread.start()
        task_threads[user_id] = thread

# ══════════════════════════════════════════════════════════════════════════════
#  ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/')
def index():
    if 'user_id' in session:
        user = db.session.get(User, session['user_id'])
        if user:
            return redirect(url_for('dashboard'))
    return redirect(url_for('login_page'))

@app.route('/login')
def login_page():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('login.html')

@app.route('/register')
def register_page():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('register.html')

@app.route('/api/register', methods=['POST'])
def register():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'Invalid JSON data'}), 400
            
        username = data.get('username')
        email = data.get('email')
        password = data.get('password')
        
        if not username or not email or not password:
            return jsonify({'error': 'All fields required'}), 400
        
        if User.query.filter_by(username=username).first():
            return jsonify({'error': 'Username already exists'}), 400
        
        if User.query.filter_by(email=email).first():
            return jsonify({'error': 'Email already exists'}), 400
        
        password_hash = bcrypt.generate_password_hash(password).decode('utf-8')
        user = User(username=username, email=email, password_hash=password_hash)
        db.session.add(user)
        db.session.commit()
        
        log.info(f"User registered: {username}")
        return jsonify({'message': 'User created successfully'}), 201
    except Exception as e:
        log.error(f"Registration error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/login', methods=['POST'])
def login():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'Invalid JSON data'}), 400
            
        username = data.get('username')
        password = data.get('password')
        
        if not username or not password:
            return jsonify({'error': 'Username and password required'}), 400
        
        user = User.query.filter_by(username=username).first()
        if not user:
            return jsonify({'error': 'Invalid credentials'}), 401
        
        if not bcrypt.check_password_hash(user.password_hash, password):
            return jsonify({'error': 'Invalid credentials'}), 401
        
        session.clear()
        session['user_id'] = user.id
        session['username'] = user.username
        session['is_admin'] = user.is_admin
        session.permanent = True
        
        log.info(f"User logged in: {username}")
        return jsonify({
            'message': 'Login successful',
            'user': {
                'id': user.id,
                'username': user.username,
                'email': user.email,
                'is_admin': user.is_admin
            }
        })
    except Exception as e:
        log.error(f"Login error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'message': 'Logged out successfully'})

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login_page'))
    user = db.session.get(User, session['user_id'])
    if not user:
        session.clear()
        return redirect(url_for('login_page'))
    return render_template('dashboard.html')

@app.route('/admin')
def admin_dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login_page'))
    user = db.session.get(User, session['user_id'])
    if not user or not user.is_admin:
        return redirect(url_for('dashboard'))
    return render_template('admin.html')

@app.route('/api/user')
def get_current_user():
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    
    user = db.session.get(User, session['user_id'])
    if not user:
        session.clear()
        return jsonify({'error': 'User not found'}), 401
        
    return jsonify({
        'id': user.id,
        'username': user.username,
        'email': user.email,
        'is_admin': user.is_admin
    })

@app.route('/api/task', methods=['POST'])
@login_required
def create_task():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'Invalid JSON data'}), 400
            
        url = data.get('url')
        
        if not url:
            return jsonify({'error': 'URL is required'}), 400
        
        if 'mediafire.com' in url.lower():
            source_type = 'mediafire'
        elif re.match(r'^https?://', url, re.I):
            source_type = 'direct'
        else:
            return jsonify({'error': 'Unsupported URL'}), 400
        
        user_id = session['user_id']
        
        task = Task(
            user_id=user_id,
            url=url,
            source_type=source_type,
            status='queued'
        )
        db.session.add(task)
        db.session.commit()
        
        log.info(f"Task {task.id} created for user {user_id}: {url}")
        
        with task_lock:
            if user_id not in task_queues:
                task_queues[user_id] = queue.Queue()
            task_queues[user_id].put(task.id)
        
        start_task_processor(user_id)
        
        return jsonify({
            'message': 'Task created successfully',
            'task_id': task.id
        })
    except Exception as e:
        log.error(f"Task creation error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/tasks')
@login_required
def get_tasks():
    try:
        user_id = session['user_id']
        tasks = Task.query.filter_by(user_id=user_id).order_by(Task.created_at.desc()).all()
        
        return jsonify({
            'tasks': [{
                'id': t.id,
                'url': t.url,
                'source_type': t.source_type,
                'filename': t.filename or 'Unknown',
                'file_size': fmt_size(t.file_size) if t.file_size > 0 else 'Unknown',
                'status': t.status,
                'progress': t.progress,
                'downloaded': fmt_size(t.downloaded_bytes),
                'uploaded': fmt_size(t.uploaded_bytes) if t.uploaded_bytes > 0 else '0 B',
                'speed': fmt_speed(t.speed) if t.speed > 0 else '0 B/s',
                'eta': t.eta or '∞',
                'gofile_link': t.gofile_link,
                'error_message': t.error_message,
                'created_at': t.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                'completed_at': t.completed_at.strftime('%Y-%m-%d %H:%M:%S') if t.completed_at else None
            } for t in tasks]
        })
    except Exception as e:
        log.error(f"Get tasks error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/task/<int:task_id>', methods=['DELETE'])
@login_required
def delete_task(task_id):
    try:
        user_id = session['user_id']
        task = db.session.get(Task, task_id)
        
        if not task:
            return jsonify({'error': 'Task not found'}), 404
        
        if task.user_id != user_id:
            return jsonify({'error': 'Unauthorized'}), 403
        
        # Check if task is currently processing
        with task_lock:
            if user_id in active_tasks and active_tasks[user_id] == task_id:
                return jsonify({'error': 'Cannot delete task that is currently processing'}), 400
        
        db.session.delete(task)
        db.session.commit()
        
        log.info(f"Task {task_id} deleted by user {user_id}")
        return jsonify({'message': 'Task deleted successfully'})
    except Exception as e:
        log.error(f"Delete task error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/tasks/active')
@login_required
def get_active_task():
    try:
        user_id = session['user_id']
        
        with task_lock:
            if user_id in active_tasks:
                task = db.session.get(Task, active_tasks[user_id])
                if task and task.status in ['queued', 'downloading', 'uploading']:
                    return jsonify({
                        'active': True,
                        'task': {
                            'id': task.id,
                            'filename': task.filename or 'Resolving...',
                            'file_size': fmt_size(task.file_size) if task.file_size > 0 else 'Unknown',
                            'status': task.status,
                            'progress': task.progress,
                            'downloaded': fmt_size(task.downloaded_bytes),
                            'uploaded': fmt_size(task.uploaded_bytes) if task.uploaded_bytes > 0 else '0 B',
                            'speed': fmt_speed(task.speed) if task.speed > 0 else '0 B/s',
                            'eta': task.eta or '∞'
                        }
                    })
        
        queued = Task.query.filter_by(user_id=user_id, status='queued').count()
        downloading = Task.query.filter_by(user_id=user_id, status='downloading').count()
        uploading = Task.query.filter_by(user_id=user_id, status='uploading').count()
        
        return jsonify({
            'active': False,
            'queued': queued,
            'downloading': downloading,
            'uploading': uploading
        })
    except Exception as e:
        log.error(f"Get active task error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/queue/status')
@login_required
def get_queue_status():
    try:
        user_id = session['user_id']
        
        queued = Task.query.filter_by(user_id=user_id, status='queued').count()
        downloading = Task.query.filter_by(user_id=user_id, status='downloading').count()
        uploading = Task.query.filter_by(user_id=user_id, status='uploading').count()
        
        return jsonify({
            'queued': queued,
            'downloading': downloading,
            'uploading': uploading,
            'active': user_id in active_tasks
        })
    except Exception as e:
        log.error(f"Get queue status error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/set_gofile_token', methods=['POST'])
@login_required
def set_gofile_token():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'Invalid JSON data'}), 400
            
        token = data.get('token')
        
        if not token:
            return jsonify({'error': 'Token is required'}), 400
        
        user = db.session.get(User, session['user_id'])
        if user:
            user.gofile_token = token
            db.session.commit()
        
        return jsonify({'message': 'GoFile token updated successfully'})
    except Exception as e:
        log.error(f"Set token error: {e}")
        return jsonify({'error': str(e)}), 500

# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/admin/users')
@admin_required
def get_users():
    try:
        users = User.query.all()
        return jsonify({
            'users': [{
                'id': u.id,
                'username': u.username,
                'email': u.email,
                'is_admin': u.is_admin,
                'created_at': u.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                'task_count': Task.query.filter_by(user_id=u.id).count()
            } for u in users]
        })
    except Exception as e:
        log.error(f"Get users error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/user/<int:user_id>', methods=['PUT', 'DELETE'])
@admin_required
def manage_user(user_id):
    try:
        user = db.session.get(User, user_id)
        if not user:
            return jsonify({'error': 'User not found'}), 404
        
        if request.method == 'DELETE':
            Task.query.filter_by(user_id=user_id).delete()
            db.session.delete(user)
            db.session.commit()
            return jsonify({'message': 'User deleted successfully'})
        
        data = request.get_json()
        if not data:
            return jsonify({'error': 'Invalid JSON data'}), 400
            
        if data.get('is_admin') is not None:
            user.is_admin = data['is_admin']
            db.session.commit()
        
        return jsonify({'message': 'User updated successfully'})
    except Exception as e:
        log.error(f"Manage user error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/tasks')
@admin_required
def get_all_tasks():
    try:
        limit = request.args.get('limit', 100, type=int)
        tasks = Task.query.order_by(Task.created_at.desc()).limit(limit).all()
        
        return jsonify({
            'tasks': [{
                'id': t.id,
                'user_id': t.user_id,
                'username': db.session.get(User, t.user_id).username if db.session.get(User, t.user_id) else 'Unknown',
                'url': t.url,
                'source_type': t.source_type,
                'filename': t.filename,
                'status': t.status,
                'progress': t.progress,
                'gofile_link': t.gofile_link,
                'error_message': t.error_message,
                'created_at': t.created_at.strftime('%Y-%m-%d %H:%M:%S')
            } for t in tasks]
        })
    except Exception as e:
        log.error(f"Get all tasks error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/stats')
@admin_required
def get_stats():
    try:
        total_users = User.query.count()
        total_tasks = Task.query.count()
        completed = Task.query.filter_by(status='completed').count()
        failed = Task.query.filter_by(status='failed').count()
        processing = Task.query.filter(Task.status.in_(['downloading', 'uploading'])).count()
        queued = Task.query.filter_by(status='queued').count()
        
        return jsonify({
            'total_users': total_users,
            'total_tasks': total_tasks,
            'completed': completed,
            'failed': failed,
            'processing': processing,
            'queued': queued
        })
    except Exception as e:
        log.error(f"Get stats error: {e}")
        return jsonify({'error': str(e)}), 500

# ══════════════════════════════════════════════════════════════════════════════
#  SERVER STATUS
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/server/status')
def server_status():
    try:
        return jsonify({
            'cpu_percent': psutil.cpu_percent(interval=0.5),
            'memory_percent': psutil.virtual_memory().percent,
            'memory_used': psutil.virtual_memory().used,
            'memory_total': psutil.virtual_memory().total,
            'disk_usage': psutil.disk_usage('/').used,
            'disk_total': psutil.disk_usage('/').total,
            'disk_percent': psutil.disk_usage('/').percent
        })
    except Exception as e:
        log.error(f"Server status error: {e}")
        return jsonify({'error': str(e)}), 500

# ══════════════════════════════════════════════════════════════════════════════
#  CREATE DEFAULT ADMIN
# ══════════════════════════════════════════════════════════════════════════════

def create_default_admin():
    with app.app_context():
        admin = User.query.filter_by(username='user').first()
        if not admin:
            password_hash = bcrypt.generate_password_hash('pass').decode('utf-8')
            admin = User(
                username='user',
                email='admin@example.com',
                password_hash=password_hash,
                is_admin=True
            )
            db.session.add(admin)
            db.session.commit()
            log.info("Default admin user created: ")

# ══════════════════════════════════════════════════════════════════════════════
#  RUN THE APP
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    os.makedirs(TEMP_DOWNLOAD_DIR, exist_ok=True)
    
    with app.app_context():
        db.create_all()
        create_default_admin()
    
    log.info("🚀 Starting Ultra Fast Transfer Web Server...")
    log.info("📋 Access at: http://localhost:5000")
    log.info("👤 Admin: ")
    log.info("🔗 Login: http://localhost:5000/login")
    log.info("🔗 Register: http://localhost:5000/register")
    
    app.run(debug=True, host='0.0.0.0', port=5000, threaded=True)
