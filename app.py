import json, os, re, sqlite3, secrets, hashlib, base64, urllib.parse, urllib.request, threading, time
from dotenv import load_dotenv

load_dotenv()
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB = BASE / 'codestorm.db'
PORT = int(os.environ.get('PORT', '8000'))
GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID', '').strip()
GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET', '').strip()
GOOGLE_REDIRECT_URI = os.environ.get('GOOGLE_REDIRECT_URI', f'http://localhost:{PORT}/api/google/callback').strip()
GOOGLE_STATE = {}
TOKENS = {}
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY', '').strip()
OPENAI_MODEL = os.environ.get('OPENAI_MODEL', 'gpt-6-luna').strip() or 'gpt-6-luna'


def db():
    conn = sqlite3.connect(DB, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA busy_timeout=30000')
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


def column_exists(c, table, column):
    return any(r['name'] == column for r in c.execute(f'PRAGMA table_info({table})').fetchall())


def init_db():
    c = db()
    c.executescript('''
    CREATE TABLE IF NOT EXISTS users (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      username TEXT UNIQUE NOT NULL,
      password TEXT NOT NULL,
      role TEXT NOT NULL DEFAULT 'doctor',
      full_name TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS patients (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      patient_code TEXT UNIQUE NOT NULL,
      name TEXT NOT NULL,
      dob TEXT, gender TEXT, phone TEXT, email TEXT,
      address TEXT, blood_group TEXT, emergency_contact TEXT,
      doctor_id INTEGER,
      created_at TEXT NOT NULL,
      FOREIGN KEY(doctor_id) REFERENCES users(id) ON DELETE SET NULL
    );
    CREATE TABLE IF NOT EXISTS cases (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      patient_id INTEGER NOT NULL,
      chief_complaint TEXT, symptoms TEXT, medical_history TEXT,
      allergies TEXT, medications TEXT, vitals TEXT,
      clinical_notes TEXT, summary TEXT,
      status TEXT NOT NULL DEFAULT 'Draft',
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      FOREIGN KEY(patient_id) REFERENCES patients(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS appointments (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      patient_id INTEGER NOT NULL,
      doctor_id INTEGER,
      appointment_date TEXT NOT NULL,
      reason TEXT,
      status TEXT NOT NULL DEFAULT 'Requested',
      notes TEXT,
      doctor_suggestion TEXT,
      created_at TEXT NOT NULL,
      FOREIGN KEY(patient_id) REFERENCES patients(id) ON DELETE CASCADE,
      FOREIGN KEY(doctor_id) REFERENCES users(id) ON DELETE SET NULL
    );
      CREATE TABLE IF NOT EXISTS referrals (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      patient_id INTEGER NOT NULL,
      case_id INTEGER NOT NULL,
      from_doctor_id INTEGER NOT NULL,
      to_doctor_id INTEGER NOT NULL,
      reason TEXT,
      created_at TEXT NOT NULL,
      FOREIGN KEY(patient_id) REFERENCES patients(id) ON DELETE CASCADE,
      FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE CASCADE,
      FOREIGN KEY(from_doctor_id) REFERENCES users(id) ON DELETE CASCADE,
      FOREIGN KEY(to_doctor_id) REFERENCES users(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS messages (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      patient_id INTEGER NOT NULL,
      doctor_id INTEGER NOT NULL,
      sender_role TEXT NOT NULL CHECK(sender_role IN ('patient','doctor')),
      sender_id INTEGER NOT NULL,
      message TEXT NOT NULL,
      created_at TEXT NOT NULL,
      read_at TEXT,
      FOREIGN KEY(patient_id) REFERENCES patients(id) ON DELETE CASCADE,
      FOREIGN KEY(doctor_id) REFERENCES users(id) ON DELETE CASCADE,
      FOREIGN KEY(sender_id) REFERENCES users(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS notifications (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL,
      type TEXT NOT NULL,
      title TEXT NOT NULL,
      message TEXT NOT NULL,
      entity_type TEXT,
      entity_id INTEGER,
      created_at TEXT NOT NULL,
      read_at TEXT,
      FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(patient_id, doctor_id, id);
    CREATE INDEX IF NOT EXISTS idx_messages_unread ON messages(doctor_id, read_at, sender_role);
    CREATE INDEX IF NOT EXISTS idx_appointments_doctor_date ON appointments(doctor_id, appointment_date);
    CREATE INDEX IF NOT EXISTS idx_appointments_patient_date ON appointments(patient_id, appointment_date);
    CREATE INDEX IF NOT EXISTS idx_notifications_user_read ON notifications(user_id, read_at, id);
    ''')
    # Upgrade older MVP database without requiring the user to delete their data.
    if not column_exists(c,'cases','doctor_id'):
        c.execute('ALTER TABLE cases ADD COLUMN doctor_id INTEGER')
    if not column_exists(c,'patients','doctor_id'):
        c.execute('ALTER TABLE patients ADD COLUMN doctor_id INTEGER')
    for col, ddl in [
        ('referred_by_doctor', 'ALTER TABLE patients ADD COLUMN referred_by_doctor TEXT'),
        ('referring_doctor_name', 'ALTER TABLE patients ADD COLUMN referring_doctor_name TEXT'),
        ('referral_complete', 'ALTER TABLE patients ADD COLUMN referral_complete INTEGER NOT NULL DEFAULT 1'),
    ]:
        if not column_exists(c,'patients',col):
            c.execute(ddl)
    for table, col, ddl in [
        ('appointments','doctor_suggestion','ALTER TABLE appointments ADD COLUMN doctor_suggestion TEXT'),
        ('appointments','referred_by_doctor','ALTER TABLE appointments ADD COLUMN referred_by_doctor TEXT'),
        ('appointments','referring_doctor_name','ALTER TABLE appointments ADD COLUMN referring_doctor_name TEXT'),
        ('users','email','ALTER TABLE users ADD COLUMN email TEXT'),
        ('users','google_sub','ALTER TABLE users ADD COLUMN google_sub TEXT'),
        ('users','patient_id','ALTER TABLE users ADD COLUMN patient_id INTEGER'),
    ]:
        if not column_exists(c, table, col):
            c.execute(ddl)
    doctors = [
        ('doctor1','doctor123','Dr. Arjun Mehta','arjun.mehta@codestorm.local'),
        ('doctor2','doctor123','Dr. Priya Sharma','priya.sharma@codestorm.local'),
        ('doctor3','doctor123','Dr. Rahul Verma','rahul.verma@codestorm.local'),
        ('doctor4','doctor123','Dr. Neha Kapoor','neha.kapoor@codestorm.local'),
    ]
    # Convert the original single demo doctor into doctor1 on older databases.
    legacy=c.execute("SELECT id FROM users WHERE username='doctor' AND role='doctor'").fetchone()
    if legacy and not c.execute("SELECT 1 FROM users WHERE username='doctor1'").fetchone():
        c.execute("UPDATE users SET username='doctor1', password=?, full_name=?, email=? WHERE id=?", ('doctor123','Dr. Arjun Mehta','arjun.mehta@codestorm.local',legacy['id']))
    for username,password,name,email in doctors:
        if not c.execute('SELECT 1 FROM users WHERE username=?',(username,)).fetchone():
            c.execute("INSERT INTO users(username,password,role,full_name,email) VALUES (?,?,?,?,?)", (username,password,'doctor',name,email))
    if not c.execute("SELECT 1 FROM users WHERE username='admin'").fetchone():
        c.execute("INSERT INTO users(username,password,role,full_name,email) VALUES (?,?,?,?,?)", ('admin','admin123','admin','System Administrator','admin@codestorm.local'))
    if c.execute('SELECT COUNT(*) FROM patients').fetchone()[0] == 0:
        now = datetime.now().isoformat(timespec='seconds')
        demo_doc=c.execute("SELECT id FROM users WHERE username='doctor1' AND role='doctor'").fetchone()
        demo_doctor_id=demo_doc['id'] if demo_doc else None
        cur = c.execute("INSERT INTO patients(patient_code,name,dob,gender,phone,email,address,blood_group,emergency_contact,doctor_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  ('PAT-0001','Aarav Sharma','1998-04-12','Male','9876543210','aarav@example.com','Jaipur, Rajasthan','B+','9876500000',demo_doctor_id,now))
        pid = cur.lastrowid
        c.execute("INSERT INTO cases(patient_id,chief_complaint,symptoms,medical_history,allergies,medications,vitals,clinical_notes,summary,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (pid,'Fever and cough','Fever for 3 days; dry cough; fatigue','No significant past medical history','No known allergies','Paracetamol 500 mg as needed','Temp: 101.2 F; BP: 118/76; Pulse: 92 bpm','Patient reports symptoms began after recent travel.','Patient presents with a 3-day history of fever, dry cough, and fatigue. No significant past history or known allergies reported. Current medication: paracetamol as needed. Vitals recorded: temperature 101.2 F, BP 118/76, pulse 92 bpm.','Completed',now,now))
    # Assign the built-in demo patient to doctor1 after ownership migration.
    demo_doc=c.execute("SELECT id FROM users WHERE username='doctor1' AND role='doctor'").fetchone()
    demo_patient=c.execute("SELECT id FROM patients WHERE patient_code='PAT-0001' AND (doctor_id IS NULL OR doctor_id=0)").fetchone()
    if demo_doc and demo_patient:
        c.execute('UPDATE patients SET doctor_id=? WHERE id=?',(demo_doc['id'],demo_patient['id']))
    c.execute("UPDATE patients SET referred_by_doctor='No' WHERE referred_by_doctor IS NULL OR referred_by_doctor=''")
    c.execute("UPDATE patients SET referring_doctor_name='' WHERE referring_doctor_name IS NULL")
    c.execute("UPDATE patients SET referral_complete=1 WHERE referral_complete IS NULL")
    # Recover ownership for older patients when their existing cases already have a doctor.
    c.execute('''UPDATE patients SET doctor_id=(SELECT c.doctor_id FROM cases c WHERE c.patient_id=patients.id AND c.doctor_id IS NOT NULL ORDER BY c.id DESC LIMIT 1) WHERE doctor_id IS NULL AND EXISTS (SELECT 1 FROM cases c2 WHERE c2.patient_id=patients.id AND c2.doctor_id IS NOT NULL)''')
    # Keep the built-in demo case useful after the ownership migration.
    demo_doc=c.execute("SELECT id FROM users WHERE username='doctor1' AND role='doctor'").fetchone()
    demo_case=c.execute("SELECT c.id FROM cases c JOIN patients p ON p.id=c.patient_id WHERE p.patient_code='PAT-0001' AND c.doctor_id IS NULL ORDER BY c.id LIMIT 1").fetchone()
    if demo_doc and demo_case:
        c.execute('UPDATE cases SET doctor_id=? WHERE id=?',(demo_doc['id'],demo_case['id']))
    c.commit(); c.close()


def add_notification(c, user_id, ntype, title, message, entity_type=None, entity_id=None):
    if not user_id:
        return
    c.execute('INSERT INTO notifications(user_id,type,title,message,entity_type,entity_id,created_at) VALUES (?,?,?,?,?,?,?)',
              (int(user_id), ntype, title, message, entity_type, entity_id, datetime.now().isoformat(timespec='seconds')))


def sync_appointment_statuses(c):
    now = datetime.now()
    rows = c.execute("""SELECT a.id,a.patient_id,a.doctor_id,a.appointment_date,p.name patient_name,u.full_name doctor_name
                       FROM appointments a JOIN patients p ON p.id=a.patient_id
                       LEFT JOIN users u ON u.id=a.doctor_id WHERE a.status='Confirmed'""").fetchall()
    changed=False
    for a in rows:
        try: due=datetime.fromisoformat(a['appointment_date'])
        except Exception: continue
        if due <= now:
            cur=c.execute("UPDATE appointments SET status='Completed' WHERE id=? AND status='Confirmed'", (a['id'],))
            if cur.rowcount:
                changed=True
                linked=c.execute("SELECT id FROM users WHERE patient_id=? AND role='patient' LIMIT 1",(a['patient_id'],)).fetchone()
                if linked: add_notification(c,linked['id'],'appointment_completed','Appointment completed',f"Your appointment with {a['doctor_name'] or 'your doctor'} has been marked completed.",'appointment',a['id'])
                if a['doctor_id']: add_notification(c,a['doctor_id'],'appointment_completed','Appointment completed',f"The appointment with {a['patient_name']} has been marked completed.",'appointment',a['id'])
    if changed: c.commit()

def notification_worker():
    while True:
        try:
            c=db(); sync_appointment_statuses(c); c.close()
        except Exception: pass
        time.sleep(15)

def hash_password(password):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 120000)
    return 'pbkdf2$120000$' + base64.urlsafe_b64encode(salt).decode() + '$' + base64.urlsafe_b64encode(dk).decode()


def verify_password(password, stored):
    if not stored.startswith('pbkdf2$'):
        return secrets.compare_digest(password, stored)
    try:
        _, rounds, salt_b64, hash_b64 = stored.split('$', 3)
        salt = base64.urlsafe_b64decode(salt_b64.encode())
        expected = base64.urlsafe_b64decode(hash_b64.encode())
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, int(rounds))
        return secrets.compare_digest(actual, expected)
    except Exception:
        return False


def summary_text(p, case):
    name = p['name']; parts=[]
    for label, key in [('presents with','chief_complaint'),('Symptoms','symptoms'),('Medical history','medical_history'),('Allergies','allergies'),('Medications','medications'),('Vitals','vitals'),('Clinical notes','clinical_notes')]:
        val=(case[key] or '').strip()
        if val:
            parts.append(f"{name} presents with {val.lower()}." if key=='chief_complaint' else f"{label}: {val}.")
    return ' '.join(parts) or 'Insufficient information for a case summary.'



def local_ai_answer(message, context=''):
    text=(message or '').strip().lower()
    if any(k in text for k in ('summarize','summary','summarise')) and context:
        return 'Here is a documentation-focused summary based only on the supplied record:\n\n'+context[:5000]+'\n\nReview this summary against the original record before clinical use.'
    if any(k in text for k in ('chest pain','difficulty breathing','shortness of breath','severe bleeding','unconscious','seizure','face drooping','slurred speech','one-sided weakness')):
        return 'The information contains a potential warning sign. This assistant cannot diagnose or determine severity. Prompt clinical assessment is appropriate, and emergency services should be considered if symptoms are severe, sudden, or worsening.'
    if any(k in text for k in ('diagnose','diagnosis','what disease','what do i have')):
        return 'I cannot provide a diagnosis. I can help organize the recorded symptoms, history, medicines, and warning signs for discussion with a qualified clinician.'
    return 'I am the CODESTORM clinical assistant. I can help summarize case information, identify information that may need clinical review, explain documentation in plain language, and organize questions for a doctor. I do not diagnose, prescribe, or replace professional medical judgment.'


def openai_ai_answer(message, context='', history=None):
    if not OPENAI_API_KEY:
        return None
    history=history or []
    system=(
        'You are the CODESTORM Clinical Assistant inside a patient case-taking application. '
        'You are a documentation and information-support assistant, not a doctor. Never claim to diagnose, '
        'prescribe, or replace a clinician. For urgent symptoms, advise prompt professional assessment. '
        'Use only the supplied patient context and conversation. If information is missing, say so. '
        'Keep answers concise, clear, and clinically cautious. Do not invent patient facts. '
        'When summarizing, preserve uncertainty and distinguish recorded facts from suggestions.\n\n'
        'PATIENT/CASE CONTEXT:\n'+(context[:12000] if context else 'No patient record was supplied.')
    )
    items=[{'role':'developer','content':system}]
    for h in history[-8:]:
        role=h.get('role') if isinstance(h,dict) else None
        content=h.get('content') if isinstance(h,dict) else None
        if role in ('user','assistant') and content:
            items.append({'role':role,'content':str(content)[:4000]})
    items.append({'role':'user','content':str(message)[:5000]})
    payload=json.dumps({'model':OPENAI_MODEL,'input':items,'max_output_tokens':700}).encode()
    req=urllib.request.Request('https://api.openai.com/v1/responses',data=payload,headers={'Authorization':'Bearer '+OPENAI_API_KEY,'Content-Type':'application/json'},method='POST')
    try:
        with urllib.request.urlopen(req,timeout=45) as r:
            data=json.loads(r.read().decode())
        text=data.get('output_text')
        if text:return text.strip()
        out=[]
        for item in data.get('output',[]) or []:
            for part in item.get('content',[]) or []:
                if part.get('type')=='output_text' and part.get('text'):out.append(part['text'])
        return '\n'.join(out).strip() if out else None
    except Exception:
        return None


def send_json(h, obj, status=200, extra_headers=None):
    data=json.dumps(obj, ensure_ascii=False).encode()
    h.send_response(status); h.send_header('Content-Type','application/json; charset=utf-8'); h.send_header('Cache-Control','no-store, no-cache, must-revalidate, max-age=0'); h.send_header('Pragma','no-cache'); h.send_header('Content-Length',str(len(data)))
    for k,v in (extra_headers or {}).items(): h.send_header(k,v)
    h.end_headers(); h.wfile.write(data)


def redirect(h, url):
    h.send_response(302); h.send_header('Location', url); h.end_headers()


def read_body(h):
    n=int(h.headers.get('Content-Length','0')); raw=h.rfile.read(n) if n else b''
    try:return json.loads(raw.decode() or '{}')
    except:return {}


def auth_user(h, required_roles=None):
    auth=h.headers.get('Authorization','')
    token=auth[7:] if auth.startswith('Bearer ') else ''
    user=TOKENS.get(token)
    if not user or (required_roles and user['role'] not in required_roles): return None
    return user


def create_token(user):
    token=secrets.token_urlsafe(24); TOKENS[token]=dict(user); return token


def doctor_has_appointment(c, doctor_id, patient_id):
    return bool(c.execute("SELECT 1 FROM appointments WHERE doctor_id=? AND patient_id=? AND status!='Cancelled' LIMIT 1", (doctor_id, patient_id)).fetchone())


def doctor_has_referral(c, doctor_id, patient_id):
    return bool(c.execute("SELECT 1 FROM referrals WHERE patient_id=? AND from_doctor_id=? LIMIT 1", (patient_id, doctor_id)).fetchone())


def doctor_has_referral_for_case(c, doctor_id, case_id):
    return bool(c.execute("SELECT 1 FROM referrals WHERE case_id=? AND from_doctor_id=? LIMIT 1", (case_id, doctor_id)).fetchone())


def doctor_can_access_patient(c, doctor_id, patient_id):
    # Used for creating new clinical records. A referring doctor is view-only.
    p=c.execute('SELECT doctor_id FROM patients WHERE id=?',(patient_id,)).fetchone()
    if not p: return False
    return (p['doctor_id']==doctor_id or doctor_has_appointment(c, doctor_id, patient_id))


def google_enabled():
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def google_start_url(role='patient', doctor_id=None, registering=False):
    state=secrets.token_urlsafe(24); GOOGLE_STATE[state]={'created':datetime.now().timestamp(),'role':role,'doctor_id':doctor_id,'registering':bool(registering)}
    q=urllib.parse.urlencode({'client_id':GOOGLE_CLIENT_ID,'redirect_uri':GOOGLE_REDIRECT_URI,'response_type':'code','scope':'openid email profile','state':state,'access_type':'online','prompt':'select_account'})
    return 'https://accounts.google.com/o/oauth2/v2/auth?'+q


def google_exchange(code):
    body=urllib.parse.urlencode({'code':code,'client_id':GOOGLE_CLIENT_ID,'client_secret':GOOGLE_CLIENT_SECRET,'redirect_uri':GOOGLE_REDIRECT_URI,'grant_type':'authorization_code'}).encode()
    req=urllib.request.Request('https://oauth2.googleapis.com/token', data=body, headers={'Content-Type':'application/x-www-form-urlencoded'})
    with urllib.request.urlopen(req, timeout=12) as r: token=json.loads(r.read().decode())
    req=urllib.request.Request('https://openidconnect.googleapis.com/v1/userinfo', headers={'Authorization':'Bearer '+token['access_token']})
    with urllib.request.urlopen(req, timeout=12) as r: return json.loads(r.read().decode())


class Handler(BaseHTTPRequestHandler):
    server_version='CODESTORM/2.0'
    def log_message(self, fmt,*args): pass
    def do_GET(self): self.route('GET')
    def do_POST(self): self.route('POST')
    def do_PUT(self): self.route('PUT')
    def do_DELETE(self): self.route('DELETE')

    def route(self, method):
        u=urlparse(self.path); path=u.path
        try:
            if path.startswith('/api/'):
                self.api(method,path,u)
            elif path=='/' or path=='/index.html':
                data=(BASE/'templates'/'index.html').read_bytes(); self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
            elif path.startswith('/static/'):
                f=BASE/path.lstrip('/')
                if f.exists():
                    typ='text/javascript' if f.suffix=='.js' else 'text/css'; data=f.read_bytes(); self.send_response(200); self.send_header('Content-Type',typ); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
                else:self.send_error(404)
            else:self.send_error(404)
        except Exception as e:
            send_json(self,{'error':str(e)},500)


    def api(self, method, path, u):
        c=None
        try:
            c=db()
            if method=='GET' and path=='/api/google/config':
                return send_json(self, {'enabled':google_enabled(), 'client_id':GOOGLE_CLIENT_ID, 'redirect_uri':GOOGLE_REDIRECT_URI})
            if method=='GET' and path=='/api/google/start':
                if not google_enabled(): return send_json(self, {'error':'Google login is not configured. Add GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and GOOGLE_REDIRECT_URI to the environment.'}, 503)
                params=parse_qs(u.query)
                role=params.get('role',['patient'])[0].lower()
                if role not in ('patient','doctor'): role='patient'
                registering=params.get('registering',['0'])[0]=='1'
                doctor_id=params.get('doctor_id',[''])[0]
                if role=='doctor':
                    if not doctor_id or not doctor_id.isdigit():
                        return send_json(self, {'error':'Select one of the four doctors before using Google sign-in.'}, 400)
                    if not c.execute("SELECT id FROM users WHERE id=? AND role='doctor'",(int(doctor_id),)).fetchone():
                        return send_json(self, {'error':'Invalid doctor selection.'}, 400)
                else:
                    # Patient accounts are created without a doctor assignment.
                    # The doctor is selected only when the patient books the first appointment.
                    doctor_id=''
                return redirect(self, google_start_url(role, int(doctor_id) if doctor_id else None, registering))
            if method=='GET' and path=='/api/google/callback':
                if not google_enabled(): return redirect(self, '/?oauth_error='+urllib.parse.quote('Google login is not configured'))
                params=parse_qs(u.query); code=params.get('code',[''])[0]; state=params.get('state',[''])[0]
                state_info=GOOGLE_STATE.pop(state,None)
                if not code or not state_info or datetime.now().timestamp()-state_info.get('created',0)>600: return redirect(self, '/?oauth_error='+urllib.parse.quote('Invalid or expired Google sign-in request'))
                requested_role=state_info.get('role','patient')
                try:
                    c.close(); c=None
                    info=google_exchange(code); email=info.get('email','').lower().strip(); sub=info.get('sub',''); name=info.get('name') or email.split('@')[0]
                    c=db()
                    if not email or not sub: raise ValueError('Google did not provide a verified email')
                    row=c.execute('SELECT * FROM users WHERE google_sub=? OR lower(email)=?',(sub,email)).fetchone()
                    if row:
                        if row['role'] != requested_role and not (requested_role=='doctor' and row['role']=='admin'):
                            raise ValueError(f'This Google account is registered as {row["role"]}. Please use the {row["role"].title()} login.')
                        if requested_role=='doctor' and row['role']=='doctor':
                            selected_id=state_info.get('doctor_id')
                            if selected_id and int(selected_id)!=row['id']:
                                raise ValueError('This Google account is already linked to a different doctor profile.')
                        # Patient Google login never asks for or changes doctor assignment.
                        # Doctor assignment happens when the patient books an appointment.
                        if row['google_sub'] != sub: c.execute('UPDATE users SET google_sub=? WHERE id=?',(sub,row['id']))
                        user=dict(c.execute('SELECT * FROM users WHERE id=?',(row['id'],)).fetchone())
                    else:
                        if requested_role=='doctor':
                            selected_id=state_info.get('doctor_id')
                            doctor=c.execute("SELECT * FROM users WHERE id=? AND role='doctor'",(selected_id,)).fetchone() if selected_id else None
                            if not doctor:
                                raise ValueError('Selected doctor profile was not found.')
                            if doctor['google_sub']:
                                raise ValueError('This doctor profile is already linked to another Google account.')
                            # First Google sign-in links the selected demo doctor profile to the Google account.
                            c.execute('UPDATE users SET google_sub=?, email=? WHERE id=?',(sub,email,doctor['id']))
                            user=dict(c.execute('SELECT * FROM users WHERE id=?',(doctor['id'],)).fetchone())
                        else:
                            if not state_info.get('registering'):
                                raise ValueError('No patient account was found for this Google account. Please use Patient Register to create an account.')
                            now=datetime.now().isoformat(timespec='seconds'); code_patient='PAT-'+secrets.token_hex(3).upper()
                            cur=c.execute("INSERT INTO patients(patient_code,name,email,referred_by_doctor,referring_doctor_name,referral_complete,created_at) VALUES (?,?,?,'','',0,?)",(code_patient,name,email,now)); pid=cur.lastrowid
                            username=email
                            cur=c.execute('INSERT INTO users(username,password,role,full_name,email,google_sub,patient_id) VALUES (?,?,?,?,?,?,?)',(username,hash_password(secrets.token_urlsafe(20)),'patient',name,email,sub,pid))
                            user=dict(c.execute('SELECT * FROM users WHERE id=?',(cur.lastrowid,)).fetchone())
                    c.commit(); token=create_token({'id':user['id'],'username':user['username'],'role':user['role'],'full_name':user['full_name'],'patient_id':user['patient_id']})
                    return redirect(self, '/?oauth_token='+urllib.parse.quote(token))
                except Exception as e:
                    return redirect(self, '/?oauth_error='+urllib.parse.quote('Google sign-in failed: '+str(e)))
            if method=='POST' and path=='/api/login':
                b=read_body(self); identifier=(b.get('username') or b.get('email') or '').strip().lower(); password=b.get('password',''); requested_role=(b.get('role') or 'patient').lower()
                row=c.execute('SELECT * FROM users WHERE lower(username)=? OR lower(email)=?',(identifier,identifier)).fetchone()
                if not row or not verify_password(password,row['password']): return send_json(self,{'error':'Invalid credentials'},401)
                if requested_role=='patient' and row['role']!='patient': return send_json(self,{'error':'This account is not a patient account.'},403)
                if requested_role=='doctor' and row['role'] not in ('doctor','admin'): return send_json(self,{'error':'This account is not a doctor account.'},403)
                user={'id':row['id'],'username':row['username'],'role':row['role'],'full_name':row['full_name'],'patient_id':row['patient_id']}
                return send_json(self,{'user':user,'token':create_token(user)})
            if method=='POST' and path=='/api/patient-register':
                b=read_body(self); name=b.get('name','').strip(); email=b.get('email','').strip().lower(); password=b.get('password',''); phone=b.get('phone','').strip()
                referred=str(b.get('referred_by_doctor','')).strip(); referring_name=str(b.get('referring_doctor_name','')).strip()
                if len(name)<2 or '@' not in email or len(password)<6: return send_json(self,{'error':'Enter a valid name, email and password of at least 6 characters.'},400)
                if referred not in ('Yes','No'): return send_json(self,{'error':'Please answer whether you were referred by another doctor: Yes or No.'},400)
                if referred=='Yes' and not referring_name: return send_json(self,{'error':'Please enter the referring doctor name.'},400)
                if referred=='No': referring_name=''
                if c.execute('SELECT 1 FROM users WHERE lower(username)=? OR lower(email)=?',(email,email)).fetchone(): return send_json(self,{'error':'An account with this email already exists.'},409)
                now=datetime.now().isoformat(timespec='seconds'); code='PAT-'+secrets.token_hex(3).upper()
                cur=c.execute('INSERT INTO patients(patient_code,name,phone,email,referred_by_doctor,referring_doctor_name,referral_complete,created_at) VALUES (?,?,?,?,?,?,1,?)',(code,name,phone,email,referred,referring_name,now)); pid=cur.lastrowid
                cur=c.execute('INSERT INTO users(username,password,role,full_name,email,patient_id) VALUES (?,?,?,?,?,?)',(email,hash_password(password),'patient',name,email,pid)); uid=cur.lastrowid
                c.commit(); user={'id':uid,'username':email,'role':'patient','full_name':name,'patient_id':pid}
                return send_json(self,{'user':user,'token':create_token(user),'patient_code':code},201)
            if method=='GET' and path=='/api/me':
                user=auth_user(self)
                if not user:return send_json(self,{'error':'Unauthorized'},401)
                return send_json(self,user)
            if method=='POST' and path=='/api/logout':
                auth=self.headers.get('Authorization',''); token=auth[7:] if auth.startswith('Bearer ') else ''; TOKENS.pop(token,None); return send_json(self,{'ok':True})
            user=auth_user(self)
            # Patient portal: patients can view and edit only their own profile.
            if method=='GET' and path=='/api/my-record':
                if not user or user['role']!='patient' or not user.get('patient_id'): return send_json(self,{'error':'Unauthorized'},401)
                p=c.execute('SELECT p.*,u.full_name doctor_name FROM patients p LEFT JOIN users u ON u.id=p.doctor_id WHERE p.id=?',(user['patient_id'],)).fetchone(); cases=c.execute('SELECT c.*,u.full_name doctor_name FROM cases c LEFT JOIN users u ON u.id=c.doctor_id WHERE c.patient_id=? ORDER BY c.id DESC',(user['patient_id'],)).fetchall(); refs=c.execute('SELECT r.*,fd.full_name from_doctor_name,td.full_name to_doctor_name FROM referrals r JOIN users fd ON fd.id=r.from_doctor_id JOIN users td ON td.id=r.to_doctor_id WHERE r.patient_id=? ORDER BY r.id DESC',(user['patient_id'],)).fetchall()
                if not p:return send_json(self,{'error':'Patient profile not found'},404)
                return send_json(self,{'patient':dict(p),'cases':[dict(x) for x in cases],'referrals':[dict(x) for x in refs]})

            # Shared patient-profile update endpoint. Doctors/admins can edit any patient;
            # patients can edit only their own linked record. Patient code remains immutable.
            m=re.match(r'^/api/patients/(\d+)$',path)
            if m and method=='PUT':
                pid=int(m.group(1))
                if not user or (user['role']=='patient' and user.get('patient_id')!=pid) or user['role'] not in ('patient','doctor','admin'):
                    return send_json(self,{'error':'Unauthorized'},401)
                existing=c.execute('SELECT * FROM patients WHERE id=?',(pid,)).fetchone()
                if not existing:return send_json(self,{'error':'Patient not found'},404)
                b=read_body(self); name=(b.get('name') if b.get('name') is not None else existing['name']).strip()
                if not name:return send_json(self,{'error':'Patient name is required'},400)
                if user['role']=='doctor' and existing['doctor_id']!=user['id']:
                    return send_json(self,{'error':'The referring doctor has view-only access to this patient profile.'},403)
                new_doctor_id=existing['doctor_id']
                if user['role']=='admin' and 'doctor_id' in b:
                    try: new_doctor_id=int(b['doctor_id']) if b['doctor_id'] not in (None,'') else None
                    except Exception: return send_json(self,{'error':'Invalid doctor_id'},400)
                    if new_doctor_id is not None and not c.execute("SELECT id FROM users WHERE id=? AND role='doctor'",(new_doctor_id,)).fetchone():
                        return send_json(self,{'error':'Invalid doctor'},400)
                fields=['name','dob','gender','phone','email','address','blood_group','emergency_contact']
                vals=[b.get(f, existing[f] or '') for f in fields]
                referred=str(b.get('referred_by_doctor', existing['referred_by_doctor'] or '')).strip()
                referring_name=str(b.get('referring_doctor_name', existing['referring_doctor_name'] or '')).strip()
                if referred not in ('Yes','No'): return send_json(self,{'error':'Please answer whether you were referred by another doctor: Yes or No.'},400)
                if referred=='Yes' and not referring_name: return send_json(self,{'error':'Please enter the referring doctor name.'},400)
                if referred=='No': referring_name=''
                email=str(vals[4]).strip().lower()
                # Keep the linked patient login account synchronized with profile changes.
                linked=c.execute('SELECT * FROM users WHERE patient_id=?',(pid,)).fetchone()
                if linked and not email:
                    email=(linked['email'] or existing['email'] or '').strip().lower()
                if linked:
                    conflict=c.execute('SELECT id FROM users WHERE lower(email)=? AND id<>?',(email,linked['id'])).fetchone() if email else None
                    if conflict:return send_json(self,{'error':'That email is already used by another account.'},409)
                    c.execute('UPDATE users SET full_name=?, email=?, username=? WHERE id=?',(name,email,email,linked['id']))
                previous_doctor_id=existing['doctor_id']
                c.execute('UPDATE patients SET name=?,dob=?,gender=?,phone=?,email=?,address=?,blood_group=?,emergency_contact=?,doctor_id=?,referred_by_doctor=?,referring_doctor_name=?,referral_complete=1 WHERE id=?',(*vals[:4],email,*vals[5:],new_doctor_id,referred,referring_name,pid))
                if user['role']=='admin' and new_doctor_id != previous_doctor_id:
                    linked_patient=c.execute("SELECT id FROM users WHERE patient_id=? AND role='patient' LIMIT 1",(pid,)).fetchone()
                    if new_doctor_id:
                        doctor_row=c.execute("SELECT full_name FROM users WHERE id=?",(new_doctor_id,)).fetchone()
                        add_notification(c,int(new_doctor_id),'patient_registered','Patient registered',f'{name} is now registered with you.', 'patient', pid)
                        if linked_patient:
                            add_notification(c,linked_patient['id'],'patient_registered','Registered doctor assigned',f'You are now registered with {doctor_row['full_name'] if doctor_row else 'your doctor'}.', 'patient', pid)
                    elif linked_patient and previous_doctor_id:
                        add_notification(c,linked_patient['id'],'patient_registered','Registered doctor removed','You are no longer assigned to a registered doctor.', 'patient', pid)
                c.commit()
                updated=c.execute('SELECT * FROM patients WHERE id=?',(pid,)).fetchone()
                # Refresh this user's in-memory token identity after an edit.
                for token,tuser in list(TOKENS.items()):
                    if tuser.get('id')==user.get('id'):
                        tuser['full_name']=name; tuser['username']=email or tuser.get('username'); tuser['patient_id']=pid
                        TOKENS[token]=tuser
                return send_json(self,{'ok':True,'patient':dict(updated)})

            # Permanent patient/account deletion. Before removing the patient row,
            # explicitly remove every notification that points to the patient or to
            # any of its child records. This prevents old patient references from
            # remaining visible in another doctor's notification/message workspace.
            # Child clinical data is then removed by the ON DELETE CASCADE foreign keys.
            if m and method=='DELETE':
                pid=int(m.group(1))
                existing=c.execute('SELECT id,name FROM patients WHERE id=?',(pid,)).fetchone()
                if not existing:return send_json(self,{'error':'Patient not found'},404)
                if user['role']=='admin' or (user['role']=='patient' and user.get('patient_id')==pid):
                    linked=c.execute("SELECT id FROM users WHERE patient_id=? AND role='patient'",(pid,)).fetchone()
                    # Capture every doctor who considered this patient registered, referred, or booked.
                    affected_doctors={r['id'] for r in c.execute("SELECT DISTINCT doctor_id id FROM patients WHERE id=? AND doctor_id IS NOT NULL",(pid,)).fetchall()}
                    affected_doctors.update(r['id'] for r in c.execute("SELECT DISTINCT doctor_id id FROM appointments WHERE patient_id=? AND doctor_id IS NOT NULL",(pid,)).fetchall())
                    affected_doctors.update(r['id'] for r in c.execute("SELECT from_doctor_id id FROM referrals WHERE patient_id=? UNION SELECT to_doctor_id id FROM referrals WHERE patient_id=?",(pid,pid)).fetchall())

                    # Capture child IDs before the patient is deleted so all related
                    # notification rows can be removed as well.
                    case_ids=[r['id'] for r in c.execute('SELECT id FROM cases WHERE patient_id=?',(pid,)).fetchall()]
                    appointment_ids=[r['id'] for r in c.execute('SELECT id FROM appointments WHERE patient_id=?',(pid,)).fetchall()]
                    referral_ids=[r['id'] for r in c.execute('SELECT id FROM referrals WHERE patient_id=?',(pid,)).fetchall()]
                    message_ids=[r['id'] for r in c.execute('SELECT id FROM messages WHERE patient_id=?',(pid,)).fetchall()]

                    # Patient-scoped notifications. Entity IDs for appointments,
                    # referrals, messages and cases are also removed so doctors do
                    # not retain stale links to a deleted patient.
                    c.execute("DELETE FROM notifications WHERE entity_type='patient' AND entity_id=?",(pid,))
                    for etype, ids in (('case',case_ids),('appointment',appointment_ids),('referral',referral_ids),('message',message_ids)):
                        if ids:
                            placeholders=','.join('?' for _ in ids)
                            c.execute(f"DELETE FROM notifications WHERE entity_type=? AND entity_id IN ({placeholders})", [etype,*ids])

                    # Deleting the patient cascades cases, appointments, referrals
                    # and messages because foreign_keys=ON is enabled in db().
                    c.execute('DELETE FROM patients WHERE id=?',(pid,))
                    if linked:
                        c.execute("DELETE FROM users WHERE id=? AND role='patient'",(linked['id'],))
                    # Tell affected doctors immediately so their dashboard can refresh the registered count.
                    for did in affected_doctors:
                        add_notification(c, int(did), 'patient_deleted', 'Patient record deleted', f'Patient {existing["name"]} was permanently deleted. Your Registered Patients count has been updated.', 'patient_deleted', pid)
                    c.commit()

                    if linked:
                        for token,tuser in list(TOKENS.items()):
                            if tuser.get('id')==linked['id']: TOKENS.pop(token,None)
                    if user['role']=='patient':
                        auth=self.headers.get('Authorization',''); token=auth[7:] if auth.startswith('Bearer ') else ''
                        if token:TOKENS.pop(token,None)
                    return send_json(self,{'ok':True,'deleted_patient_id':pid,'account_deleted':bool(linked)})
                return send_json(self,{'error':'Only an administrator can delete another patient. Patients can delete only their own record.'},403)

            # Notifications
            if path=='/api/notifications':
                if not user: return send_json(self,{'error':'Unauthorized'},401)
                sync_appointment_statuses(c)
                if method=='GET':
                    rows=c.execute('SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 60',(user['id'],)).fetchall()
                    unread=c.execute('SELECT COUNT(*) n FROM notifications WHERE user_id=? AND read_at IS NULL',(user['id'],)).fetchone()['n']
                    return send_json(self,{'notifications':[dict(r) for r in rows],'unread':unread})
                if method=='PUT':
                    b=read_body(self); stamp=datetime.now().isoformat(timespec='seconds')
                    if b.get('all'): c.execute('UPDATE notifications SET read_at=? WHERE user_id=? AND read_at IS NULL',(stamp,user['id']))
                    elif b.get('id'): c.execute('UPDATE notifications SET read_at=? WHERE id=? AND user_id=?',(stamp,int(b['id']),user['id']))
                    c.commit(); return send_json(self,{'ok':True})
                return send_json(self,{'error':'Method not allowed'},405)

            # Appointment APIs
            if method=='GET' and path=='/api/doctors':
                if not user:return send_json(self,{'error':'Unauthorized'},401)
                rows=c.execute("SELECT id,full_name,email FROM users WHERE role='doctor' ORDER BY id").fetchall()
                return send_json(self,[dict(r) for r in rows])
            if method=='GET' and path=='/api/appointments':
                if not user:return send_json(self,{'error':'Unauthorized'},401)
                sync_appointment_statuses(c)
                if user['role']=='patient':
                    rows=c.execute("SELECT a.*,p.patient_code,p.name patient_name,u.full_name doctor_name FROM appointments a JOIN patients p ON p.id=a.patient_id LEFT JOIN users u ON u.id=a.doctor_id WHERE a.patient_id=? ORDER BY a.appointment_date DESC",(user['patient_id'],)).fetchall()
                elif user['role']=='doctor':
                    # Appointment assignment is independent of case ownership/referral.
                    # Every doctor sees every appointment that the patient explicitly
                    # booked with that doctor, regardless of who currently owns the case.
                    rows=c.execute("SELECT a.*,p.patient_code,p.name patient_name,u.full_name doctor_name FROM appointments a JOIN patients p ON p.id=a.patient_id JOIN users u ON u.id=a.doctor_id AND u.role='doctor' WHERE a.doctor_id=? ORDER BY datetime(a.appointment_date) ASC, a.id ASC",(user['id'],)).fetchall()
                else:
                    rows=c.execute("SELECT a.*,p.patient_code,p.name patient_name,u.full_name doctor_name FROM appointments a JOIN patients p ON p.id=a.patient_id LEFT JOIN users u ON u.id=a.doctor_id ORDER BY a.appointment_date DESC").fetchall()
                result=[]
                now_ts=datetime.now().timestamp()
                for r in rows:
                    item=dict(r)
                    try: age=now_ts-datetime.fromisoformat(item.get('created_at','')).timestamp()
                    except Exception: age=999999
                    item['reason_available']=age < 300
                    item['edit_available']=age < 300 and item.get('status')!='Cancelled'
                    item['reason_expires_at']=(datetime.fromisoformat(item['created_at']).timestamp()+300) if item.get('created_at') else None
                    item['edit_expires_at']=item['reason_expires_at']
                    if not item['reason_available']: item['reason']=''
                    result.append(item)
                return send_json(self,result)
            if method=='POST' and path=='/api/appointments':
                if not user:return send_json(self,{'error':'Unauthorized'},401)
                b=read_body(self); date=(b.get('appointment_date') or '').strip(); reason=(b.get('reason') or '').strip(); doctor_id=b.get('doctor_id'); referred_by_doctor=(b.get('referred_by_doctor') or '').strip(); referring_doctor_name=(b.get('referring_doctor_name') or '').strip()
                if not date:return send_json(self,{'error':'Choose an appointment date and time.'},400)
                try:
                    datetime.fromisoformat(date)
                except Exception:
                    return send_json(self,{'error':'Invalid appointment date/time.'},400)
                if referred_by_doctor not in ('Yes','No'):
                    return send_json(self,{'error':'Please select whether you were referred by another doctor.'},400)
                if referred_by_doctor=='Yes' and not referring_doctor_name:
                    return send_json(self,{'error':'Please enter the referring doctor name.'},400)
                if referred_by_doctor=='No': referring_doctor_name=''
                if user['role']=='patient':
                    pid=user.get('patient_id')
                    patient=c.execute('SELECT doctor_id FROM patients WHERE id=?',(pid,)).fetchone() if pid else None
                    if not patient:return send_json(self,{'error':'Patient profile not found.'},404)
                    # A doctor is selected for every appointment. The patient's current
                    # registered doctor remains the case/message owner and is only set
                    # automatically when the patient has no doctor yet.
                    if not doctor_id:
                        return send_json(self,{'error':'Please select a doctor for this appointment.'},400)
                    try: doctor_id=int(doctor_id)
                    except Exception: return send_json(self,{'error':'Invalid doctor selection.'},400)
                    if not c.execute("SELECT id FROM users WHERE id=? AND role='doctor'",(doctor_id,)).fetchone(): return send_json(self,{'error':'Doctor not found.'},404)
                elif user['role'] in ('doctor','admin'):
                    pid=int(b.get('patient_id') or 0)
                    if user['role']=='doctor':
                        doctor_id=user['id']
                else:
                    return send_json(self,{'error':'Unauthorized'},401)
                patient_row=c.execute('SELECT * FROM patients WHERE id=?',(pid,)).fetchone()
                if not patient_row: return send_json(self,{'error':'Patient not found.'},404)
                if user['role']=='patient' and not int(patient_row['referral_complete'] or 0): return send_json(self,{'error':'Please complete the required referral question before booking an appointment.'},403)
                if doctor_id and not c.execute("SELECT id FROM users WHERE id=? AND role='doctor'",(int(doctor_id),)).fetchone(): return send_json(self,{'error':'Doctor not found.'},404)
                day=date[:10]
                existing=c.execute("SELECT id FROM appointments WHERE patient_id=? AND substr(appointment_date,1,10)=? LIMIT 1",(pid,day)).fetchone()
                if existing:
                    return send_json(self,{'error':'You already have an appointment for this date. Only one appointment is allowed per patient per day.'},409)
                # Booking an appointment does NOT register the patient with that doctor.
                # Registration/ownership is a separate workflow; appointment access is temporary
                # and remains represented only by appointments.doctor_id.
                now=datetime.now().isoformat(timespec='seconds')
                assigned_doctor=c.execute("SELECT id,full_name FROM users WHERE id=? AND role='doctor'",(int(doctor_id),)).fetchone()
                if not assigned_doctor:
                    return send_json(self,{'error':'The selected doctor is no longer available.'},409)
                cur=c.execute('INSERT INTO appointments(patient_id,doctor_id,appointment_date,reason,status,created_at,referred_by_doctor,referring_doctor_name) VALUES (?,?,?,?,?,?,?,?)',(pid,int(doctor_id),date,reason,'Requested',now,referred_by_doctor,referring_doctor_name))
                aid=cur.lastrowid
                add_notification(c,assigned_doctor['id'],'appointment_requested','New appointment request',f"{patient_row['name']} requested an appointment for {date.replace('T',' ')}.",'appointment',aid)
                c.commit()
                return send_json(self,{'id':aid,'doctor_id':assigned_doctor['id'],'doctor_name':assigned_doctor['full_name'],'status':'Requested'},201)
            m=re.match(r'^/api/appointments/(\d+)$',path)
            if m:
                aid=int(m.group(1)); ap=c.execute('SELECT * FROM appointments WHERE id=?',(aid,)).fetchone()
                if not ap:return send_json(self,{'error':'Appointment not found'},404)
                if user['role']=='patient' and ap['patient_id']!=user.get('patient_id'):return send_json(self,{'error':'Unauthorized'},403)
                if user['role']=='doctor' and ap['doctor_id']!=user['id']:return send_json(self,{'error':'Unauthorized'},403)
                if method=='PUT' and user['role']=='patient':
                    age=datetime.now().timestamp()-datetime.fromisoformat(ap['created_at']).timestamp()
                    if age >= 300:
                        return send_json(self,{'error':'The 5-minute appointment edit window has expired.'},403)
                    b=read_body(self); new_date=(b.get('appointment_date') or ap['appointment_date']).strip(); new_reason=(b.get('reason') if b.get('reason') is not None else ap['reason']); new_referred=(b.get('referred_by_doctor') if b.get('referred_by_doctor') is not None else (ap['referred_by_doctor'] or 'No')); new_ref_name=(b.get('referring_doctor_name') if b.get('referring_doctor_name') is not None else (ap['referring_doctor_name'] or '')); new_doctor_id=b.get('doctor_id', ap['doctor_id'])
                    try: new_doctor_id=int(new_doctor_id)
                    except Exception: return send_json(self,{'error':'Please select a valid doctor.'},400)
                    if not c.execute("SELECT id FROM users WHERE id=? AND role='doctor'",(new_doctor_id,)).fetchone(): return send_json(self,{'error':'Doctor not found.'},404)
                    if new_referred not in ('Yes','No'): return send_json(self,{'error':'Please select whether you were referred by another doctor.'},400)
                    if new_referred=='Yes' and not str(new_ref_name).strip(): return send_json(self,{'error':'Please enter the referring doctor name.'},400)
                    if new_referred=='No': new_ref_name=''
                    try: datetime.fromisoformat(new_date)
                    except Exception: return send_json(self,{'error':'Invalid appointment date/time.'},400)
                    day=new_date[:10]
                    conflict=c.execute("SELECT id FROM appointments WHERE patient_id=? AND substr(appointment_date,1,10)=? AND id<>? LIMIT 1",(ap['patient_id'],day,aid)).fetchone()
                    if conflict:return send_json(self,{'error':'You already have another appointment for that date.'},409)
                    patient_row=c.execute('SELECT name FROM patients WHERE id=?',(ap['patient_id'],)).fetchone()
                    old_doctor=ap['doctor_id']; changed_doctor=int(new_doctor_id)!=int(old_doctor); changed_when=new_date!=ap['appointment_date']; changed_reason=str(new_reason).strip()!=(ap['reason'] or '')
                    c.execute('UPDATE appointments SET appointment_date=?,doctor_id=?,reason=?,referred_by_doctor=?,referring_doctor_name=? WHERE id=?',(new_date,new_doctor_id,str(new_reason).strip(),new_referred,str(new_ref_name).strip(),aid))
                    if changed_doctor:
                        if old_doctor and old_doctor!=new_doctor_id: add_notification(c,old_doctor,'appointment_updated','Appointment reassigned',f"{patient_row['name'] if patient_row else 'A patient'} changed appointment #{aid} to another doctor.",'appointment',aid)
                        add_notification(c,new_doctor_id,'appointment_updated','Appointment assigned to you',f"An appointment with {patient_row['name'] if patient_row else 'a patient'} was assigned to you for {new_date.replace('T',' ')}.",'appointment',aid)
                    elif changed_when or changed_reason:
                        add_notification(c,ap['doctor_id'],'appointment_updated','Appointment updated',f"{patient_row['name'] if patient_row else 'A patient'} updated appointment #{aid}.",'appointment',aid)
                    c.commit()
                    return send_json(self,{'ok':True})
                if method=='PUT' and user['role'] in ('doctor','admin'):
                    b=read_body(self); status=b.get('status',ap['status']); notes=b.get('notes',ap['notes'] or ''); suggestion=b.get('doctor_suggestion',ap['doctor_suggestion'] or '')
                    if status not in ('Requested','Confirmed','Completed','Cancelled'):return send_json(self,{'error':'Invalid appointment status'},400)
                    previous=ap['status']; c.execute('UPDATE appointments SET status=?,notes=?,doctor_suggestion=? WHERE id=?',(status,notes,suggestion,aid))
                    patient_user=c.execute("SELECT id FROM users WHERE patient_id=? AND role='patient' LIMIT 1",(ap['patient_id'],)).fetchone()
                    if status!=previous and patient_user:
                        label={'Confirmed':'accepted','Cancelled':'rejected/cancelled','Completed':'completed','Requested':'set back to requested'}.get(status,status.lower())
                        add_notification(c,patient_user['id'],'appointment_status',f'Appointment {label}',f"Your appointment with the doctor has been {label}.",'appointment',aid)
                    c.commit();return send_json(self,{'ok':True})
                if method=='DELETE' and user['role'] in ('admin','doctor'):
                    # Admin can permanently delete any appointment. A doctor can permanently
                    # delete only an appointment assigned to that doctor.
                    if user['role']=='doctor' and ap['doctor_id'] != user['id']:
                        return send_json(self,{'error':'You can only permanently delete appointments assigned to you.'},403)
                    patient_user=c.execute("SELECT id FROM users WHERE patient_id=? AND role='patient' LIMIT 1",(ap['patient_id'],)).fetchone()
                    patient_row=c.execute('SELECT name FROM patients WHERE id=?',(ap['patient_id'],)).fetchone()
                    patient_name=patient_row['name'] if patient_row else 'A patient'
                    doctor_user=c.execute("SELECT id,full_name FROM users WHERE id=? AND role='doctor'",(ap['doctor_id'],)).fetchone() if ap['doctor_id'] else None
                    deleter_label='the administrator' if user['role']=='admin' else (doctor_user['full_name'] if doctor_user else 'the doctor')
                    if patient_user:
                        add_notification(c,patient_user['id'],'appointment_deleted','Appointment deleted',f"Your appointment #{aid} with {doctor_user['full_name'] if doctor_user else 'the doctor'} was permanently deleted by {deleter_label}.",'appointment',aid)
                    if user['role']=='admin' and doctor_user:
                        add_notification(c,doctor_user['id'],'appointment_deleted','Appointment deleted',f"Appointment #{aid} with {patient_name} was permanently deleted by the administrator.",'appointment',aid)
                    c.execute('DELETE FROM appointments WHERE id=?',(aid,))
                    c.commit();return send_json(self,{'ok':True,'deleted_appointment_id':aid})
                if method=='DELETE' and user['role']=='patient':
                    if ap['status']=='Cancelled': return send_json(self,{'ok':True})
                    c.execute("UPDATE appointments SET status='Cancelled' WHERE id=?",(aid,))
                    if ap['doctor_id']:
                        pname=c.execute('SELECT name FROM patients WHERE id=?',(ap['patient_id'],)).fetchone()
                        add_notification(c,ap['doctor_id'],'appointment_cancelled','Appointment cancelled',f"{pname['name'] if pname else 'A patient'} cancelled appointment #{aid}.",'appointment',aid)
                    c.commit();return send_json(self,{'ok':True})
                return send_json(self,{'error':'Unauthorized'},403)
            if method=='POST' and path=='/api/ai-assistant':
                if not user or user['role'] not in ('patient','doctor','admin'):
                    return send_json(self,{'error':'Unauthorized'},401)
                b=read_body(self); message=str(b.get('message','') or '').strip()
                if not message:return send_json(self,{'error':'Message is required'},400)
                context=''; patient_id=b.get('patient_id')
                if user['role']=='patient':
                    patient_id=user.get('patient_id')
                if patient_id and str(patient_id).isdigit():
                    pid=int(patient_id)
                    p=c.execute('SELECT p.*,u.full_name doctor_name FROM patients p LEFT JOIN users u ON u.id=p.doctor_id WHERE p.id=?',(pid,)).fetchone()
                    if not p:return send_json(self,{'error':'Patient record not found'},404)
                    if user['role']=='patient' and pid!=user.get('patient_id'):return send_json(self,{'error':'You can only use your own patient record.'},403)
                    if user['role']=='doctor':
                        allowed=(p['doctor_id']==user['id'] or doctor_has_referral(c,user['id'],pid) or doctor_has_appointment(c,user['id'],pid))
                        if not allowed:return send_json(self,{'error':'This patient is not currently accessible to you.'},403)
                    cases=c.execute("SELECT c.*,u.full_name doctor_name FROM cases c LEFT JOIN users u ON u.id=c.doctor_id WHERE c.patient_id=? ORDER BY c.id DESC LIMIT 8",(pid,)).fetchall()
                    context=json.dumps({'patient':dict(p),'cases':[dict(x) for x in cases]},ensure_ascii=False)
                history=b.get('history') if isinstance(b.get('history'),list) else []
                answer=openai_ai_answer(message,context,history)
                mode='openai'
                if not answer:
                    answer=local_ai_answer(message,context); mode='local'
                return send_json(self,{'answer':answer,'mode':mode,'model':OPENAI_MODEL if mode=='openai' else None})

            if method=='POST' and path=='/api/ai-detect':
                if not user:return send_json(self,{'error':'Unauthorized'},401)
                b=read_body(self); text=' '.join(str(b.get(k,'') or '') for k in ('chief_complaint','symptoms','medical_history','clinical_notes')).lower()
                red_flags={'chest pain':'urgent cardiovascular symptom flag','difficulty breathing':'urgent breathing symptom flag','shortness of breath':'urgent breathing symptom flag','severe bleeding':'urgent bleeding symptom flag','unconscious':'urgent consciousness symptom flag','fainted':'urgent consciousness symptom flag','seizure':'urgent neurological symptom flag','face drooping':'possible stroke warning sign','slurred speech':'possible stroke warning sign','one-sided weakness':'possible stroke warning sign','suicidal':'urgent mental-health safety flag'}
                matched=[v for k,v in red_flags.items() if k in text]
                infection=any(k in text for k in ('fever','cough','sore throat','vomiting','diarrhea'))
                if matched: level='URGENT FLAG'; message='Potential red-flag symptoms detected: '+', '.join(sorted(set(matched)))+'. Seek prompt clinical assessment.'
                elif infection: level='REVIEW'; message='The entered text contains symptoms commonly seen in infectious/acute illness patterns. This is not a diagnosis; review history, examination and vitals.'
                else: level='NO OBVIOUS FLAG'; message='No predefined red-flag pattern detected by this small rule-based assistant. This does not rule out illness.'
                return send_json(self,{'level':level,'message':message,'matched':matched})

            if path=='/api/messages':
                if user['role'] not in ('patient','doctor','admin'):
                    return send_json(self,{'error':'Unauthorized'},401)
                if method=='GET':
                    params=parse_qs(u.query)
                    if user['role']=='patient':
                        pid=user.get('patient_id')
                        if not pid:return send_json(self,{'error':'Patient account is not linked to a patient record.'},400)
                        p=c.execute('SELECT p.*,u.full_name doctor_name FROM patients p LEFT JOIN users u ON u.id=p.doctor_id WHERE p.id=?',(pid,)).fetchone()
                        if not p:return send_json(self,{'error':'Patient record not found.'},404)
                        doctors=c.execute("SELECT DISTINCT u.id,u.full_name,CASE WHEN u.id=? THEN 0 ELSE 1 END priority FROM users u WHERE u.role='doctor' AND (u.id=? OR EXISTS (SELECT 1 FROM appointments a WHERE a.patient_id=? AND a.doctor_id=u.id AND a.status!='Cancelled')) ORDER BY priority,u.full_name",(p['doctor_id'] or -1,p['doctor_id'] or -1,pid)).fetchall()
                        doctor_id_raw=params.get('doctor_id',[''])[0]
                        doctor_id=int(doctor_id_raw) if doctor_id_raw and str(doctor_id_raw).isdigit() else (int(doctors[0]['id']) if doctors else None)
                        if not doctor_id:return send_json(self,{'patient_id':pid,'doctor_id':None,'doctor_name':None,'doctors':[],'messages':[]})
                        allowed=bool(c.execute("SELECT 1 FROM users u WHERE u.id=? AND u.role='doctor' AND (u.id=? OR EXISTS (SELECT 1 FROM appointments a WHERE a.patient_id=? AND a.doctor_id=u.id AND a.status!='Cancelled'))",(doctor_id,p['doctor_id'] or -1,pid)).fetchone())
                        if not allowed:return send_json(self,{'error':'You can message only your registered doctor or a doctor with an active appointment for you.'},403)
                        rows=c.execute('SELECT m.*,u.full_name sender_name FROM messages m JOIN users u ON u.id=m.sender_id WHERE m.patient_id=? AND m.doctor_id=? ORDER BY m.id ASC',(pid,doctor_id)).fetchall()
                        c.execute("UPDATE messages SET read_at=? WHERE patient_id=? AND doctor_id=? AND sender_role='doctor' AND read_at IS NULL",(datetime.now().isoformat(timespec='seconds'),pid,doctor_id)); c.commit()
                        d=c.execute('SELECT full_name FROM users WHERE id=?',(doctor_id,)).fetchone()
                        return send_json(self,{'patient_id':pid,'doctor_id':doctor_id,'doctor_name':d['full_name'] if d else None,'doctors':[dict(x) for x in doctors],'messages':[dict(x) for x in rows]})
                    if user['role']=='doctor':
                        pid_raw=params.get('patient_id',[''])[0]
                        if not pid_raw or not str(pid_raw).isdigit():
                            # Build the doctor's message inbox from the actual communication
                            # relationships, not only from the current patient registration.
                            # This is important after referrals: the original doctor must still
                            # see the receiving doctor's conversation in read-only mode, and a
                            # doctor must retain access to an existing conversation even if an
                            # appointment later ends or the patient registration changes.
                            rows=c.execute(r"""
                                SELECT * FROM (
                                    -- Patients currently registered with this doctor.
                                    SELECT p.id AS patient_id,p.patient_code,p.name,
                                           p.doctor_id AS registered_doctor_id,
                                           ru.full_name AS registered_doctor_name,
                                           p.doctor_id AS conversation_doctor_id,
                                           0 AS referred_view,0 AS appointment_access,
                                           0 AS message_history_access,
                                           (SELECT message FROM messages m WHERE m.patient_id=p.id AND m.doctor_id=p.doctor_id ORDER BY m.id DESC LIMIT 1) AS last_message,
                                           (SELECT created_at FROM messages m WHERE m.patient_id=p.id AND m.doctor_id=p.doctor_id ORDER BY m.id DESC LIMIT 1) AS last_message_at,
                                           (SELECT COUNT(*) FROM messages m WHERE m.patient_id=p.id AND m.doctor_id=p.doctor_id AND m.sender_role='patient' AND m.read_at IS NULL) AS unread
                                    FROM patients p
                                    LEFT JOIN users ru ON ru.id=p.doctor_id
                                    WHERE p.doctor_id=?

                                    UNION

                                    -- Patients this doctor previously referred. The conversation
                                    -- shown is deliberately the CURRENT receiving doctor's thread.
                                    SELECT p.id,p.patient_code,p.name,p.doctor_id,
                                           ru.full_name,p.doctor_id,1,0,0,
                                           (SELECT message FROM messages m WHERE m.patient_id=p.id AND m.doctor_id=p.doctor_id ORDER BY m.id DESC LIMIT 1),
                                           (SELECT created_at FROM messages m WHERE m.patient_id=p.id AND m.doctor_id=p.doctor_id ORDER BY m.id DESC LIMIT 1),
                                           0
                                    FROM patients p
                                    LEFT JOIN users ru ON ru.id=p.doctor_id
                                    WHERE EXISTS (SELECT 1 FROM referrals r WHERE r.patient_id=p.id AND r.from_doctor_id=?)

                                    UNION

                                    -- Patients with a non-cancelled appointment for this doctor.
                                    SELECT p.id,p.patient_code,p.name,p.doctor_id,
                                           ru.full_name,?,0,1,0,
                                           (SELECT message FROM messages m WHERE m.patient_id=p.id AND m.doctor_id=? ORDER BY m.id DESC LIMIT 1),
                                           (SELECT created_at FROM messages m WHERE m.patient_id=p.id AND m.doctor_id=? ORDER BY m.id DESC LIMIT 1),
                                           (SELECT COUNT(*) FROM messages m WHERE m.patient_id=p.id AND m.doctor_id=? AND m.sender_role='patient' AND m.read_at IS NULL)
                                    FROM patients p
                                    LEFT JOIN users ru ON ru.id=p.doctor_id
                                    WHERE EXISTS (SELECT 1 FROM appointments a WHERE a.patient_id=p.id AND a.doctor_id=? AND a.status!='Cancelled')

                                    UNION

                                    -- Existing conversations with this doctor are retained in the
                                    -- inbox even when the appointment is completed/cancelled or
                                    -- registration ownership has subsequently changed.
                                    SELECT p.id,p.patient_code,p.name,p.doctor_id,
                                           ru.full_name,?,0,0,1,
                                           (SELECT message FROM messages m WHERE m.patient_id=p.id AND m.doctor_id=? ORDER BY m.id DESC LIMIT 1),
                                           (SELECT created_at FROM messages m WHERE m.patient_id=p.id AND m.doctor_id=? ORDER BY m.id DESC LIMIT 1),
                                           (SELECT COUNT(*) FROM messages m WHERE m.patient_id=p.id AND m.doctor_id=? AND m.sender_role='patient' AND m.read_at IS NULL)
                                    FROM patients p
                                    LEFT JOIN users ru ON ru.id=p.doctor_id
                                    WHERE EXISTS (SELECT 1 FROM messages m WHERE m.patient_id=p.id AND m.doctor_id=? )
                                ) AS message_contacts
                                GROUP BY patient_id,conversation_doctor_id
                                ORDER BY CASE WHEN unread>0 THEN 0 ELSE 1 END,
                                         CASE WHEN last_message_at IS NULL THEN 1 ELSE 0 END,
                                         last_message_at DESC,name ASC
                            """,(
                                user['id'],          # registered
                                user['id'],          # referral
                                user['id'],          # appointment conversation doctor
                                user['id'],          # appointment last message
                                user['id'],          # appointment last message time
                                user['id'],          # appointment unread
                                user['id'],          # appointment exists
                                user['id'],          # history conversation doctor
                                user['id'],          # history last message
                                user['id'],          # history last message time
                                user['id'],          # history unread
                                user['id']           # history exists
                            )).fetchall()
                            return send_json(self,[dict(x) for x in rows])
                        pid=int(pid_raw)
                        access_mode=(params.get('access',[''])[0] or '').strip().lower()
                        p=c.execute('SELECT p.*,u.full_name doctor_name FROM patients p LEFT JOIN users u ON u.id=p.doctor_id WHERE p.id=?',(pid,)).fetchone()
                        if not p:return send_json(self,{'error':'Patient not found.'},404)
                        current_owner=p['doctor_id']==user['id']; referral_access=doctor_has_referral(c,user['id'],pid); appointment_access=doctor_has_appointment(c,user['id'],pid); history_access=bool(c.execute('SELECT 1 FROM messages WHERE patient_id=? AND doctor_id=? LIMIT 1',(pid,user['id'])).fetchone())
                        if not (current_owner or referral_access or appointment_access or history_access):return send_json(self,{'error':'You can view messages for patients registered with you, previously referred by you, booked with you, or with whom you already have a message history.'},403)
                        # A doctor can have more than one relationship with the same patient.
                        # The Messages list therefore sends an explicit access mode so that a
                        # referring doctor opening the "Referred case — view only" entry always
                        # sees the receiving doctor's conversation, even if the referring doctor
                        # also has a separate appointment with the same patient.
                        if access_mode=='referral' and referral_access and p['doctor_id']:
                            conversation_doctor_id=int(p['doctor_id']); read_only=True
                        elif access_mode=='appointment' and appointment_access:
                            conversation_doctor_id=user['id']; read_only=False
                        elif current_owner:
                            conversation_doctor_id=user['id']; read_only=False
                        elif referral_access:
                            conversation_doctor_id=int(p['doctor_id']) if p['doctor_id'] else user['id']; read_only=True
                        elif appointment_access:
                            conversation_doctor_id=user['id']; read_only=False
                        else:
                            conversation_doctor_id=int(p['doctor_id']) if p['doctor_id'] else user['id']; read_only=True
                        rows=c.execute('SELECT m.*,u.full_name sender_name FROM messages m JOIN users u ON u.id=m.sender_id WHERE m.patient_id=? AND m.doctor_id=? ORDER BY m.id ASC',(pid,conversation_doctor_id)).fetchall()
                        if not read_only:
                            c.execute("UPDATE messages SET read_at=? WHERE patient_id=? AND doctor_id=? AND sender_role='patient' AND read_at IS NULL",(datetime.now().isoformat(timespec='seconds'),pid,conversation_doctor_id)); c.commit()
                        d=c.execute('SELECT full_name FROM users WHERE id=?',(conversation_doctor_id,)).fetchone()
                        return send_json(self,{'patient_id':pid,'patient_name':p['name'],'patient_code':p['patient_code'],'doctor_id':conversation_doctor_id,'doctor_name':d['full_name'] if d else None,'read_only':read_only,'appointment_access':appointment_access,'messages':[dict(x) for x in rows]})
                    rows=c.execute('SELECT m.*,p.name patient_name,p.patient_code,du.full_name doctor_name,su.full_name sender_name FROM messages m JOIN patients p ON p.id=m.patient_id JOIN users du ON du.id=m.doctor_id JOIN users su ON su.id=m.sender_id ORDER BY m.id DESC').fetchall()
                    return send_json(self,[dict(x) for x in rows])
                if method=='POST':
                    b=read_body(self); text=str(b.get('message','')).strip()
                    if not text:return send_json(self,{'error':'Message cannot be empty.'},400)
                    try: pid=int(user.get('patient_id') or 0) if user['role']=='patient' else int(b.get('patient_id') or 0)
                    except Exception:return send_json(self,{'error':'Invalid patient.'},400)
                    p=c.execute('SELECT id,name,doctor_id FROM patients WHERE id=?',(pid,)).fetchone()
                    if not p:return send_json(self,{'error':'Patient not found.'},404)
                    if user['role']=='patient':
                        if user.get('patient_id')!=pid:return send_json(self,{'error':'You can only message from your own patient account.'},403)
                        requested=b.get('doctor_id'); doctor_id=int(requested) if requested not in (None,'') else p['doctor_id']
                        if not doctor_id:return send_json(self,{'error':'Please select a doctor by booking an appointment first.'},400)
                        allowed=bool(c.execute("SELECT 1 FROM users u WHERE u.id=? AND u.role='doctor' AND (u.id=? OR EXISTS (SELECT 1 FROM appointments a WHERE a.patient_id=? AND a.doctor_id=u.id AND a.status!='Cancelled'))",(doctor_id,p['doctor_id'] or -1,pid)).fetchone())
                        if not allowed:return send_json(self,{'error':'You can message only your registered doctor or a doctor with an active appointment for you.'},403)
                        sender_role='patient'; sender_id=user['id']
                    elif user['role']=='doctor':
                        doctor_id=user['id']
                        if not (p['doctor_id']==doctor_id or doctor_has_appointment(c,doctor_id,pid)):
                            return send_json(self,{'error':'You can message patients currently registered with you or booked with you. Referring doctors have view-only access.'},403)
                        sender_role='doctor'; sender_id=user['id']
                    else:return send_json(self,{'error':'Administrators do not participate in patient-doctor messaging.'},403)
                    now=datetime.now().isoformat(timespec='seconds'); cur=c.execute('INSERT INTO messages(patient_id,doctor_id,sender_role,sender_id,message,created_at) VALUES (?,?,?,?,?,?)',(pid,doctor_id,sender_role,sender_id,text,now))
                    recipient=doctor_id if sender_role=='patient' else c.execute("SELECT id FROM users WHERE patient_id=? AND role='patient' LIMIT 1",(pid,)).fetchone()
                    recipient_id=recipient['id'] if hasattr(recipient,'keys') else recipient
                    sender_name=c.execute('SELECT full_name FROM users WHERE id=?',(sender_id,)).fetchone()
                    if recipient_id: add_notification(c,recipient_id,'message','New message',f"New message from {sender_name['full_name'] if sender_name else 'a user'}.",'message',cur.lastrowid)
                    c.commit(); row=c.execute('SELECT m.*,u.full_name sender_name FROM messages m JOIN users u ON u.id=m.sender_id WHERE m.id=?',(cur.lastrowid,)).fetchone(); return send_json(self,dict(row),201)
            if method=='GET' and path=='/api/messages/unread-count':
                if user['role']=='patient':
                    pid=user.get('patient_id') or 0
                    row=c.execute("SELECT COUNT(*) n FROM messages WHERE patient_id=? AND sender_role='doctor' AND read_at IS NULL",(pid,)).fetchone()
                elif user['role']=='doctor':
                    row=c.execute("SELECT COUNT(*) n FROM messages WHERE doctor_id=? AND sender_role='patient' AND read_at IS NULL",(user['id'],)).fetchone()
                else:
                    return send_json(self,{'count':0})
                return send_json(self,{'count':int(row['n'])})
            if not user or user['role'] not in ('doctor','admin'):
                return send_json(self,{'error':'Doctor/Admin authentication required'},401)
            if method=='GET' and path=='/api/stats':
                if user['role']=='doctor':
                    # Registered Patients means patients actually registered/assigned to this doctor.
                    # Referrals and appointments do not change this count.
                    patient_count=c.execute("SELECT COUNT(*) n FROM patients WHERE doctor_id=?",(user['id'],)).fetchone()['n']
                    case_filter="WHERE c.doctor_id=? OR EXISTS (SELECT 1 FROM referrals r WHERE r.case_id=c.id AND r.from_doctor_id=?)"
                    case_params=(user['id'],user['id'])
                    stats={
                        'patients':patient_count,
                        'cases':c.execute('SELECT COUNT(*) n FROM cases c '+case_filter,case_params).fetchone()['n'],
                        'completed':c.execute("SELECT COUNT(*) n FROM cases c WHERE c.status='Completed' AND (c.doctor_id=? OR EXISTS (SELECT 1 FROM referrals r WHERE r.patient_id=c.patient_id AND r.from_doctor_id=?))",case_params).fetchone()['n'],
                        'drafts':c.execute("SELECT COUNT(*) n FROM cases c WHERE c.status='Draft' AND (c.doctor_id=? OR EXISTS (SELECT 1 FROM referrals r WHERE r.patient_id=c.patient_id AND r.from_doctor_id=?))",case_params).fetchone()['n']
                    }
                else:
                    stats={'patients':c.execute('SELECT COUNT(*) n FROM patients').fetchone()['n'],'cases':c.execute('SELECT COUNT(*) n FROM cases').fetchone()['n'],'completed':c.execute("SELECT COUNT(*) n FROM cases WHERE status='Completed'").fetchone()['n'],'drafts':c.execute("SELECT COUNT(*) n FROM cases WHERE status='Draft'").fetchone()['n']}
                return send_json(self,stats)
            if method=='GET' and path=='/api/patients':
                q=parse_qs(u.query).get('q',[''])[0].strip()
                base="SELECT p.*,u.full_name doctor_name FROM patients p LEFT JOIN users u ON u.id=p.doctor_id"
                if user['role']=='doctor':
                    sql=base+" WHERE (p.doctor_id=? OR EXISTS (SELECT 1 FROM referrals r WHERE r.patient_id=p.id AND r.from_doctor_id=?) OR EXISTS (SELECT 1 FROM appointments a WHERE a.patient_id=p.id AND a.doctor_id=? AND a.status!='Cancelled'))"; params=[user['id'],user['id'],user['id']]
                    if q:
                        sql+=" AND (p.name LIKE ? OR p.patient_code LIKE ? OR p.phone LIKE ?)"; params += [f'%{q}%',f'%{q}%',f'%{q}%']
                    sql+=" ORDER BY p.id DESC"
                else:
                    sql=base; params=[]
                    if q:
                        sql+=" WHERE p.name LIKE ? OR p.patient_code LIKE ? OR p.phone LIKE ?"; params=[f'%{q}%',f'%{q}%',f'%{q}%']
                    sql+=" ORDER BY p.id DESC"
                rows=c.execute(sql,params).fetchall(); result=[]
                for r in rows:
                    item=dict(r); item['referred_view']=bool(user['role']=='doctor' and r['doctor_id']!=user['id'] and not doctor_has_appointment(c,user['id'],r['id'])); item['appointment_access']=bool(user['role']=='doctor' and doctor_has_appointment(c,user['id'],r['id'])); result.append(item)
                return send_json(self,result)
            if method=='POST' and path=='/api/patients':
                b=read_body(self); name=b.get('name','').strip()
                if not name:return send_json(self,{'error':'Patient name is required'},400)
                if user['role']=='doctor':
                    doctor_id=user['id']
                elif user['role']=='admin':
                    doctor_id=b.get('doctor_id')
                else:
                    return send_json(self,{'error':'Doctor/Admin authentication required'},401)
                referred=str(b.get('referred_by_doctor','')).strip(); referring_name=str(b.get('referring_doctor_name','')).strip()
                if referred not in ('Yes','No'): return send_json(self,{'error':'Please answer whether the patient was referred by another doctor: Yes or No.'},400)
                if referred=='Yes' and not referring_name: return send_json(self,{'error':'Please enter the referring doctor name.'},400)
                if referred=='No': referring_name=''
                if doctor_id not in (None,''):
                    try: doctor_id=int(doctor_id)
                    except Exception: return send_json(self,{'error':'Invalid doctor_id'},400)
                    if not c.execute("SELECT id FROM users WHERE id=? AND role='doctor'",(doctor_id,)).fetchone(): return send_json(self,{'error':'Invalid doctor'},400)
                now=datetime.now().isoformat(timespec='seconds'); code='PAT-'+secrets.token_hex(3).upper(); cur=c.execute('INSERT INTO patients(patient_code,name,dob,gender,phone,email,address,blood_group,emergency_contact,doctor_id,referred_by_doctor,referring_doctor_name,referral_complete,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?)',(code,name,b.get('dob',''),b.get('gender',''),b.get('phone',''),b.get('email',''),b.get('address',''),b.get('blood_group',''),b.get('emergency_contact',''),doctor_id,referred,referring_name,now)); pid_new=cur.lastrowid
                if doctor_id:
                    add_notification(c,int(doctor_id),'patient_registered','Patient registered',f'{name} is now registered with you.', 'patient', pid_new)
                linked_patient=c.execute("SELECT id FROM users WHERE patient_id=? AND role='patient' LIMIT 1",(pid_new,)).fetchone()
                if linked_patient and doctor_id:
                    doctor_row=c.execute("SELECT full_name FROM users WHERE id=?",(int(doctor_id),)).fetchone()
                    add_notification(c,linked_patient['id'],'patient_registered','Registered doctor assigned',f'You are now registered with {doctor_row['full_name'] if doctor_row else 'your doctor'}.', 'patient', pid_new)
                c.commit(); return send_json(self,{'id':pid_new,'patient_code':code,'doctor_id':doctor_id})
            m=re.match(r'^/api/patients/(\d+)$',path)
            if m:
                pid=int(m.group(1)); p=c.execute('SELECT p.*,u.full_name doctor_name FROM patients p LEFT JOIN users u ON u.id=p.doctor_id WHERE p.id=?',(pid,)).fetchone()
                if not p:return send_json(self,{'error':'Not found'},404)
                referred_patient_access=bool(user['role']=='doctor' and doctor_has_referral(c,user['id'],pid))
                current_owner_access=bool(user['role']=='doctor' and p['doctor_id']==user['id'])
                appointment_access=bool(user['role']=='doctor' and doctor_has_appointment(c,user['id'],pid))
                if user['role']=='doctor' and not (current_owner_access or referred_patient_access or appointment_access):return send_json(self,{'error':'This patient is not currently assigned to you, previously referred by you, or booked with you.'},403)
                if method=='GET':
                    if user['role']=='doctor':
                        # A referring doctor gets view-only access to the case(s) they
                        # actually referred, not every later case created for the patient.
                        if current_owner_access:
                            cases=c.execute('''SELECT c.*,u.full_name doctor_name FROM cases c
                                              LEFT JOIN users u ON u.id=c.doctor_id
                                              WHERE c.patient_id=? ORDER BY c.id DESC''',(pid,)).fetchall()
                        elif referred_patient_access:
                            cases=c.execute('''SELECT c.*,u.full_name doctor_name FROM cases c
                                              LEFT JOIN users u ON u.id=c.doctor_id
                                              WHERE c.patient_id=? AND (c.doctor_id=? OR EXISTS (
                                                  SELECT 1 FROM referrals r WHERE r.case_id=c.id AND r.from_doctor_id=?
                                              )) ORDER BY c.id DESC''',(pid,user['id'],user['id'])).fetchall()
                        else:
                            cases=c.execute('''SELECT c.*,u.full_name doctor_name FROM cases c
                                              LEFT JOIN users u ON u.id=c.doctor_id
                                              WHERE c.patient_id=? AND c.doctor_id=? ORDER BY c.id DESC''',(pid,user['id'])).fetchall()
                    else:
                        cases=c.execute('SELECT c.*,u.full_name doctor_name FROM cases c LEFT JOIN users u ON u.id=c.doctor_id WHERE c.patient_id=? ORDER BY c.id DESC',(pid,)).fetchall()
                    refs=c.execute('SELECT r.*,fd.full_name from_doctor_name,td.full_name to_doctor_name FROM referrals r JOIN users fd ON fd.id=r.from_doctor_id JOIN users td ON td.id=r.to_doctor_id WHERE r.patient_id=? ORDER BY r.id DESC',(pid,)).fetchall(); access='current_owner' if current_owner_access else ('appointment_doctor' if appointment_access else ('referring_doctor' if referred_patient_access else 'admin')); return send_json(self,{'patient':dict(p),'cases':[dict(x) for x in cases],'referrals':[dict(x) for x in refs],'access_type':access})
            if method=='GET' and path=='/api/cases':
                if user['role']=='doctor':
                    # Appointment access does not expose another doctor's historical cases.
                    # A doctor sees their own cases, all cases for patients they previously
                    # referred, and (for appointment-only access) only cases they created.
                    rows=c.execute('''SELECT c.*,p.patient_code,p.name,u.full_name doctor_name FROM cases c
                                      JOIN patients p ON p.id=c.patient_id
                                      LEFT JOIN users u ON u.id=c.doctor_id
                                      WHERE c.doctor_id=? OR EXISTS (
                                          SELECT 1 FROM referrals r WHERE r.patient_id=c.patient_id AND r.from_doctor_id=?
                                      ) ORDER BY c.id DESC''',(user['id'],user['id'])).fetchall()
                else:
                    rows=c.execute('SELECT c.*,p.patient_code,p.name,u.full_name doctor_name FROM cases c JOIN patients p ON p.id=c.patient_id LEFT JOIN users u ON u.id=c.doctor_id ORDER BY c.id DESC').fetchall()
                return send_json(self,[dict(x) for x in rows])
            m=re.match(r'^/api/cases/(\d+)$',path)
            if m:
                cid=int(m.group(1)); case=c.execute('SELECT c.*,p.patient_code,p.name,p.dob,p.gender,p.phone,p.email,p.address,p.blood_group,p.emergency_contact,u.full_name doctor_name FROM cases c JOIN patients p ON p.id=c.patient_id LEFT JOIN users u ON u.id=c.doctor_id WHERE c.id=?',(cid,)).fetchone()
                if not case:return send_json(self,{'error':'Not found'},404)
                referred_patient = bool(user['role']=='doctor' and doctor_has_referral_for_case(c,user['id'],case['id']))
                is_current_owner = bool(user['role']=='doctor' and case['doctor_id']==user['id'])
                appointment_access = bool(user['role']=='doctor' and doctor_has_appointment(c,user['id'],case['patient_id']))
                # An appointment gives a doctor access to the patient/encounter, not to
                # unrelated historical cases owned by another doctor. Referral access is
                # the explicit mechanism that grants full historical case visibility.
                appointment_case_access = appointment_access and case['doctor_id']==user['id']
                if user['role']=='doctor' and not (is_current_owner or referred_patient or appointment_case_access):
                    return send_json(self,{'error':'You can access cases assigned to you or cases for patients you previously referred. An appointment alone does not grant access to another doctor\'s historical case.'},403)
                if method=='GET':
                    access='current_owner' if is_current_owner else ('appointment_doctor' if appointment_case_access else ('referring_doctor' if referred_patient else 'admin'))
                    d=dict(case); d['access_type']=access
                    ref=c.execute('SELECT r.*,fd.full_name from_doctor_name,td.full_name to_doctor_name FROM referrals r JOIN users fd ON fd.id=r.from_doctor_id JOIN users td ON td.id=r.to_doctor_id WHERE r.case_id=? ORDER BY r.id DESC LIMIT 1',(cid,)).fetchone(); d['latest_referral']=dict(ref) if ref else None; return send_json(self,d)
                if method=='PUT':
                    if user['role']=='doctor' and not is_current_owner:
                        return send_json(self,{'error':'The original referring doctor can view this referred case, but only the receiving doctor can edit it.'},403)
                    b=read_body(self); now=datetime.now().isoformat(timespec='seconds'); fields=['chief_complaint','symptoms','medical_history','allergies','medications','vitals','clinical_notes','status']; vals=[b.get(x,'') for x in fields]
                    doctor_id=case['doctor_id']
                    if user['role']=='admin' and 'doctor_id' in b:
                        try: doctor_id=int(b['doctor_id']) if b['doctor_id'] not in (None,'') else None
                        except Exception: return send_json(self,{'error':'Invalid doctor_id'},400)
                        if doctor_id is not None and not c.execute("SELECT id FROM users WHERE id=? AND role='doctor'",(doctor_id,)).fetchone():
                            return send_json(self,{'error':'Invalid doctor'},400)
                    if b.get('generate_summary'): vals[7]='Completed'
                    c.execute('UPDATE cases SET chief_complaint=?,symptoms=?,medical_history=?,allergies=?,medications=?,vitals=?,clinical_notes=?,status=?,doctor_id=?,updated_at=? WHERE id=?',(*vals,doctor_id,now,cid))
                    if b.get('generate_summary'):
                        fresh=c.execute('SELECT * FROM cases WHERE id=?',(cid,)).fetchone(); p=c.execute('SELECT * FROM patients WHERE id=?',(fresh['patient_id'],)).fetchone(); c.execute('UPDATE cases SET summary=? WHERE id=?',(summary_text(p,fresh),cid))
                    c.commit();return send_json(self,{'ok':True})
                if method=='DELETE':
                    if user['role']=='doctor' and not is_current_owner: return send_json(self,{'error':'The original referring doctor cannot delete a referred case.'},403)
                    c.execute('DELETE FROM cases WHERE id=?',(cid,));c.commit();return send_json(self,{'ok':True})
            if method=='POST' and path=='/api/referrals':
                if user['role']!='doctor': return send_json(self,{'error':'Only doctors can refer patients.'},403)
                b=read_body(self)
                try: cid=int(b.get('case_id') or 0); to_doctor_id=int(b.get('to_doctor_id') or 0)
                except Exception: return send_json(self,{'error':'Invalid case or doctor.'},400)
                case=c.execute('SELECT * FROM cases WHERE id=?',(cid,)).fetchone()
                if not case:return send_json(self,{'error':'Case not found.'},404)
                if case['doctor_id']!=user['id']:return send_json(self,{'error':'You can only refer a case currently assigned to you.'},403)
                target=c.execute("SELECT id,full_name FROM users WHERE id=? AND role='doctor'",(to_doctor_id,)).fetchone()
                if not target:return send_json(self,{'error':'Target doctor not found.'},404)
                if to_doctor_id==user['id']:return send_json(self,{'error':'Choose another doctor for referral.'},400)
                # Do not allow the receiving doctor to immediately refer the case back
                # to the same doctor who referred it to them.
                latest_referral=c.execute('SELECT from_doctor_id FROM referrals WHERE case_id=? ORDER BY id DESC LIMIT 1',(cid,)).fetchone()
                if latest_referral and int(latest_referral['from_doctor_id'])==to_doctor_id:
                    return send_json(self,{'error':'You cannot refer this patient back to the doctor who referred this case to you.'},400)
                reason=str(b.get('reason','')).strip(); now=datetime.now().isoformat(timespec='seconds')
                cur=c.execute('INSERT INTO referrals(patient_id,case_id,from_doctor_id,to_doctor_id,reason,created_at) VALUES (?,?,?,?,?,?)',(case['patient_id'],cid,user['id'],to_doctor_id,reason,now))
                c.execute("UPDATE cases SET doctor_id=?,status='Referred',updated_at=? WHERE id=?",(to_doctor_id,now,cid))
                c.execute('UPDATE patients SET doctor_id=? WHERE id=?',(to_doctor_id,case['patient_id']))

                # Referral notifications: notify the receiving doctor, the patient, and
                # the referring doctor. The frontend notification poller will play the
                # audible notification sound for each recipient while their portal is open.
                patient_row=c.execute('SELECT id,name FROM patients WHERE id=?',(case['patient_id'],)).fetchone()
                patient_user=c.execute("SELECT id FROM users WHERE patient_id=? AND role='patient' LIMIT 1",(case['patient_id'],)).fetchone()
                from_doctor=c.execute('SELECT full_name FROM users WHERE id=?',(user['id'],)).fetchone()
                patient_name=patient_row['name'] if patient_row else 'The patient'
                from_name=from_doctor['full_name'] if from_doctor else 'A doctor'
                add_notification(c,to_doctor_id,'referral_received','New patient referral',f'{from_name} referred {patient_name} to you.', 'referral',cur.lastrowid)
                if patient_user:
                    add_notification(c,patient_user['id'],'referral_received','Patient referral update',f'{from_name} referred your case to {target["full_name"]}.', 'referral',cur.lastrowid)
                add_notification(c,user['id'],'referral_sent','Referral sent',f'{patient_name} was referred to {target["full_name"]}.', 'referral',cur.lastrowid)
                c.commit(); return send_json(self,{'ok':True,'referral_id':cur.lastrowid,'case_id':cid,'patient_id':case['patient_id'],'to_doctor_id':to_doctor_id,'to_doctor_name':target['full_name']},201)
            if method=='GET' and path=='/api/referrals':
                if user['role']=='doctor':
                    rows=c.execute('SELECT r.*,p.patient_code,p.name patient_name,fd.full_name from_doctor_name,td.full_name to_doctor_name FROM referrals r JOIN patients p ON p.id=r.patient_id JOIN users fd ON fd.id=r.from_doctor_id JOIN users td ON td.id=r.to_doctor_id WHERE r.from_doctor_id=? OR r.to_doctor_id=? ORDER BY r.id DESC',(user['id'],user['id'])).fetchall()
                elif user['role']=='patient':
                    rows=c.execute('SELECT r.*,p.patient_code,p.name patient_name,fd.full_name from_doctor_name,td.full_name to_doctor_name FROM referrals r JOIN patients p ON p.id=r.patient_id JOIN users fd ON fd.id=r.from_doctor_id JOIN users td ON td.id=r.to_doctor_id WHERE r.patient_id=? ORDER BY r.id DESC',(user['patient_id'],)).fetchall()
                else:
                    rows=c.execute('SELECT r.*,p.patient_code,p.name patient_name,fd.full_name from_doctor_name,td.full_name to_doctor_name FROM referrals r JOIN patients p ON p.id=r.patient_id JOIN users fd ON fd.id=r.from_doctor_id JOIN users td ON td.id=r.to_doctor_id ORDER BY r.id DESC').fetchall()
                return send_json(self,[dict(x) for x in rows])
            if method=='POST' and path=='/api/cases':
                b=read_body(self); now=datetime.now().isoformat(timespec='seconds'); pid=int(b['patient_id']); status='Completed' if b.get('generate_summary') else 'Draft'
                doctor_id=user['id'] if user['role']=='doctor' else b.get('doctor_id')
                if doctor_id not in (None,''):
                    try: doctor_id=int(doctor_id)
                    except Exception: return send_json(self,{'error':'Invalid doctor_id'},400)
                    if not c.execute("SELECT id FROM users WHERE id=? AND role='doctor'",(doctor_id,)).fetchone(): return send_json(self,{'error':'Invalid doctor'},400)
                else: doctor_id=None
                patient_row=c.execute('SELECT id FROM patients WHERE id=?',(pid,)).fetchone()
                if not patient_row:return send_json(self,{'error':'Patient not found'},404)
                if user['role']=='doctor' and not doctor_can_access_patient(c,user['id'],pid):
                    return send_json(self,{'error':'You can create a case only for a patient currently assigned to you, previously referred by you, or booked with you.'},403)
                cur=c.execute('INSERT INTO cases(patient_id,doctor_id,chief_complaint,symptoms,medical_history,allergies,medications,vitals,clinical_notes,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(pid,doctor_id,b.get('chief_complaint',''),b.get('symptoms',''),b.get('medical_history',''),b.get('allergies',''),b.get('medications',''),b.get('vitals',''),b.get('clinical_notes',''),status,now,now)); cid=cur.lastrowid
                if b.get('generate_summary'):
                    fresh=c.execute('SELECT * FROM cases WHERE id=?',(cid,)).fetchone(); p=c.execute('SELECT * FROM patients WHERE id=?',(pid,)).fetchone(); c.execute('UPDATE cases SET summary=? WHERE id=?',(summary_text(p,fresh),cid))
                c.commit();return send_json(self,{'id':cid})
            return send_json(self,{'error':'Not found'},404)
        finally:
            if c is not None:
                try: c.close()
                except Exception: pass


if __name__=='__main__':
    init_db(); print(f'CODESTORM running on http://localhost:{PORT}'); print('Google OAuth:', 'enabled' if google_enabled() else 'not configured (set GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET)')
    ThreadingHTTPServer(('0.0.0.0',PORT),Handler).serve_forever()
