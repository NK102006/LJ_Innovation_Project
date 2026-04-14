from flask import Flask, render_template, jsonify, request, session, redirect
from flask_socketio import SocketIO, emit
from groq import Groq
from dotenv import load_dotenv
import os
import sqlite3
import smtplib
import ssl
import random
import time
from datetime import datetime, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this")
socketio = SocketIO(app, cors_allowed_origins="*")
client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

DB_PATH = '/tmp/attendance.db'       # Vercel only allows writes to /tmp
SPEECH_DB_PATH = '/tmp/speech.db'

OTP_SENDER_EMAIL = os.environ.get("OTP_SENDER_EMAIL")
OTP_SENDER_PASSWORD = os.environ.get("OTP_SENDER_PASSWORD")

# In-memory state updated by local camera server via /update-status
camera_state = {
    "face": False,
    "expression": "neutral",
    "gesture": "none",
    "attendance": "Absent",
    "speech": "",
    "listening": False,
}

# ── DB ──────────────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS attendance
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  userid TEXT NOT NULL, date TEXT NOT NULL,
                  status TEXT NOT NULL, last_updated TEXT NOT NULL,
                  is_locked INTEGER DEFAULT 0)''')
    conn.commit(); conn.close()

def set_attendance(userid, status):
    today = date.today().isoformat()
    now = datetime.now().isoformat(timespec='seconds')
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT id, is_locked FROM attendance WHERE userid=? AND date=?', (userid, today))
    row = c.fetchone()
    if row:
        if row[1]: conn.close(); return False
        c.execute('UPDATE attendance SET status=?, last_updated=? WHERE id=?', (status, now, row[0]))
    else:
        c.execute('INSERT INTO attendance (userid, date, status, last_updated) VALUES (?,?,?,?)',
                  (userid, today, status, now))
    conn.commit(); conn.close()
    return True

def get_attendance_counts(userid):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM attendance WHERE userid=? AND status="Present"', (userid,))
    present = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM attendance WHERE userid=? AND status="Absent"', (userid,))
    absent = c.fetchone()[0]
    conn.close()
    return present, absent

# ── OTP ─────────────────────────────────────────────────────────────────────

def generate_otp():
    return str(random.randint(100000, 999999))

def send_otp_email(email, otp):
    try:
        msg = MIMEMultipart()
        msg['From'] = OTP_SENDER_EMAIL
        msg['To'] = email
        msg['Subject'] = 'Smart Attendance OTP'
        msg.attach(MIMEText(f'Your OTP is: {otp}\n\nExpires in 5 minutes.', 'plain'))
        ctx = ssl.create_default_context()
        with smtplib.SMTP('smtp.gmail.com', 587) as s:
            s.starttls(context=ctx)
            s.login(OTP_SENDER_EMAIL, OTP_SENDER_PASSWORD)
            s.sendmail(OTP_SENDER_EMAIL, email, msg.as_string())
        return True
    except Exception as e:
        print(f"Email error: {e}"); return False

# ── Routes ───────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('front_page.html')

@app.route('/login')
def login_page():
    return render_template('middle_page.html')

@app.route('/otp')
def otp_page():
    if 'otp_verified' not in session:
        return render_template('otp_page.html', error="Please enter your email first!")
    return render_template('otp_page.html')

@app.route('/dashboard')
def dashboard():
    if not session.get('otp_verified'):
        return redirect('/login')
    return render_template('index.html')

@app.route('/logout')
def logout():
    session.clear(); return redirect('/login')

@app.route('/send-otp', methods=['POST'])
def send_otp():
    email = request.form.get("email", "").strip()
    if not email or '@' not in email:
        return jsonify({'success': False, 'message': 'Invalid email'}), 400
    otp = generate_otp()
    session.update({'otp': otp, 'email': email, 'otp_time': time.time(), 'otp_attempts': 0})
    session['userid'] = email.split('@')[0]
    if send_otp_email(email, otp):
        return jsonify({'success': True, 'message': f'OTP sent to {email}', 'redirect': '/otp'})
    session.clear()
    return jsonify({'success': False, 'message': 'Email failed'}), 500

@app.route('/verify-otp', methods=['POST'])
def verify_otp():
    user_otp = request.form.get("otp", "").strip()
    stored = session.get('otp')
    if not stored:
        return jsonify({'success': False, 'message': 'Session expired'}), 400
    if time.time() - session.get('otp_time', 0) > 300:
        session.clear()
        return jsonify({'success': False, 'message': 'OTP expired', 'expired': True}), 400
    attempts = session.get('otp_attempts', 0)
    if attempts >= 3:
        session.clear()
        return jsonify({'success': False, 'message': 'Too many attempts'}), 400
    if user_otp == stored:
        session['otp_verified'] = True
        session.pop('otp', None)
        return jsonify({'success': True, 'redirect': '/dashboard'})
    session['otp_attempts'] = attempts + 1
    return jsonify({'success': False, 'message': f'Wrong OTP. {2 - attempts} left.'})

@app.route('/resend-otp', methods=['POST'])
def resend_otp():
    email = request.form.get('email') or session.get('email')
    if not email:
        return jsonify({'success': False, 'message': 'No email found'}), 400
    otp = generate_otp()
    session.update({'otp': otp, 'email': email, 'otp_time': time.time(), 'otp_attempts': 0})
    if send_otp_email(email, otp):
        return jsonify({'success': True, 'message': f'New OTP sent to {email}'})
    return jsonify({'success': False, 'message': 'Failed to send OTP'}), 500

# ── Camera state endpoint (called by your local camera_server.py) ────────────

@app.route('/update-status', methods=['POST'])
def update_status():
    """Local camera server POSTs detected state here."""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data'}), 400
    camera_state.update({k: data[k] for k in camera_state if k in data})
    userid = session.get('userid', '')
    if userid and 'attendance' in data:
        set_attendance(userid, data['attendance'])
    return jsonify({'ok': True})

@app.route('/status')
def status():
    userid = session.get('userid', '')
    present, absent = get_attendance_counts(userid) if userid else (0, 0)
    return jsonify({**camera_state, 'user': userid,
                    'total_present': present, 'total_absent': absent,
                    'verified': session.get('otp_verified', False)})

@app.route('/attendance-all')
def attendance_all():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT id, userid, date, status, last_updated, is_locked FROM attendance ORDER BY date DESC LIMIT 100')
    rows = c.fetchall()
    conn.close()
    html = '<table border=1><tr><th>ID</th><th>User</th><th>Date</th><th>Status</th><th>Time</th><th>Locked</th></tr>'
    for r in rows:
        html += f'<tr><td>{r[0]}</td><td>{r[1]}</td><td>{r[2]}</td><td>{r[3]}</td><td>{r[4]}</td><td>{"🔒" if r[5] else ""}</td></tr>'
    return html + '</table>'

# ── Groq AI chat ─────────────────────────────────────────────────────────────

@socketio.on('message')
def handle_message(data):
    ctx = f"Attendance Assistant. State: {camera_state}"
    try:
        res = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "system", "content": ctx},
                      {"role": "user", "content": data['message']}]
        )
        emit('response', {'message': res.choices[0].message.content})
    except Exception as e:
        emit('response', {'message': f"AI error: {e}"})

# ── Init ─────────────────────────────────────────────────────────────────────

init_db()

if __name__ == '__main__':
    socketio.run(app, debug=True, port=5000)
