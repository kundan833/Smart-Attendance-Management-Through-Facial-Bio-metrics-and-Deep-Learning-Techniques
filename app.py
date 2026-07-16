from flask import Flask, render_template, request, redirect, flash, Response, jsonify, session, url_for
import cv2
import os
import sqlite3
import time
import numpy as np
from werkzeug.utils import secure_filename
import face_recognition
import gc
import base64
import json
import re
from datetime import datetime, date, time as dt_time, timedelta
from functools import wraps

app = Flask(__name__)

@app.context_processor
def inject_now():
    """Make current datetime available in all templates"""
    return {'now': datetime.now()}

app.secret_key = 'your_secret_key_here'
app.config['SESSION_TYPE'] = 'filesystem'
FACE_DIR = "static/captured_faces"

# Admin credentials
ADMIN_USERNAME = 'admin'
ADMIN_PASSWORD = 'admin123'
FACE_MATCH_THRESHOLD = 0.5  # Lower is stricter (original was 0.6)
REQUIRED_MATCHES = 2  # Number of angles that must match
MIN_FACE_CONFIDENCE = 0.4  # Minimum confidence for a single match

if not os.path.exists(FACE_DIR):
    os.makedirs(FACE_DIR)

# Global variables
camera = None
known_face_encodings = []
known_face_metadata = []
camera_lock = False

# Database schema version
DB_VERSION = 3

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated_function

def ensure_database_tables():
    """Ensure all database tables exist with proper schema with timezone support"""
    conn = None
    try:
        conn = sqlite3.connect('attendance.db')
        cursor = conn.cursor()
        
        # Enable foreign key constraints
        cursor.execute("PRAGMA foreign_keys = ON")
        
        # Set the database to use local time
        cursor.execute("PRAGMA timezone = 'Asia/Kolkata'")  # Change to your timezone
        
        cursor.execute("PRAGMA user_version")
        version = cursor.fetchone()[0] or 0
        
        if version < DB_VERSION:
            # Create or upgrade tables with proper constraints
            cursor.executescript(f'''
                CREATE TABLE IF NOT EXISTS streams (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    code TEXT NOT NULL UNIQUE,
                    is_active BOOLEAN DEFAULT 1
                );
                
                CREATE TABLE IF NOT EXISTS students (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    stream_id INTEGER NOT NULL,
                    image_path TEXT NOT NULL,
                    angle TEXT NOT NULL,
                    date_added TIMESTAMP DEFAULT (datetime('now', 'localtime')),
                    UNIQUE(student_id, angle),
                    FOREIGN KEY (stream_id) REFERENCES streams(id)
                );
                
                CREATE TABLE IF NOT EXISTS schedules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stream_id INTEGER NOT NULL,
                    subject TEXT NOT NULL,
                    day TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    UNIQUE(stream_id, subject, day),
                    FOREIGN KEY (stream_id) REFERENCES streams(id)
                );
                
                CREATE TABLE IF NOT EXISTS attendance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    stream_id INTEGER NOT NULL,
                    subject TEXT NOT NULL,
                    status TEXT DEFAULT 'Present',
                    timestamp DATETIME NOT NULL DEFAULT (datetime('now', 'localtime')),
                    FOREIGN KEY (student_id) REFERENCES students(student_id),
                    FOREIGN KEY (stream_id) REFERENCES streams(id),
                    UNIQUE(student_id, subject, date(timestamp))
                );
                
                CREATE TABLE IF NOT EXISTS admin_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password TEXT NOT NULL
                );
                
                -- Create index for faster attendance lookups
                CREATE INDEX IF NOT EXISTS idx_attendance_student_date 
                ON attendance(student_id, date(timestamp));
                
                PRAGMA user_version = {DB_VERSION};
            ''')
            
                       
            # Insert default streams
            default_streams = [
                ('BTech Computer Science', 'BTECH_CSE', 1),
                ('BCA', 'BCA', 1),
                ('MCA', 'MCA', 1), 
                ('BTech Electrical', 'BTECH_ELEC', 1)
            ]
            
            cursor.execute("SELECT COUNT(*) FROM streams")
            if cursor.fetchone()[0] == 0:
                cursor.executemany(
                    "INSERT INTO streams (name, code, is_active) VALUES (?, ?, ?)",
                    default_streams
                )
            
            # Insert default admin
            cursor.execute("SELECT COUNT(*) FROM admin_users WHERE username = ?", (ADMIN_USERNAME,))
            if cursor.fetchone()[0] == 0:
                cursor.execute(
                    "INSERT INTO admin_users (username, password) VALUES (?, ?)",
                    (ADMIN_USERNAME, ADMIN_PASSWORD)
                )
            
            conn.commit()
            print("Database tables verified/updated successfully")
        else:
            print("Database is up to date")
            
    except sqlite3.Error as e:
        print(f"Database error: {str(e)}")
        raise
    finally:
        if conn:
            conn.close()


def cleanup_orphaned_files():
    """Remove image files not referenced in database"""
    try:
        existing_files = set()
        for root, dirs, files in os.walk(FACE_DIR):
            for file in files:
                existing_files.add(os.path.join(root, file))
        
        referenced_files = set()
        conn = sqlite3.connect('attendance.db')
        cursor = conn.cursor()
        cursor.execute("SELECT image_path FROM students WHERE image_path IS NOT NULL")
        for (path,) in cursor.fetchall():
            referenced_files.add(path)
        conn.close()
        
        orphaned = existing_files - referenced_files
        for file in orphaned:
            try:
                os.remove(file)
                print(f"Removed orphaned file: {file}")
            except Exception as e:
                print(f"Error removing file {file}: {str(e)}")
    except Exception as e:
        print(f"Error during orphaned file cleanup: {str(e)}")

def init_db():
    """Initialize database and load known faces"""
    global known_face_encodings, known_face_metadata
    conn = None
    try:
        ensure_database_tables()
        conn = sqlite3.connect('attendance.db')
        cursor = conn.cursor()
        
        # Clean up database entries for files that don't exist
        cursor.execute("SELECT id, image_path FROM students")
        for row in cursor.fetchall():
            if row[1] and not os.path.exists(row[1]):
                cursor.execute("DELETE FROM students WHERE id = ?", (row[0],))
                conn.commit()
                print(f"Removed database entry for missing file: {row[1]}")
        
        # Load valid faces with metadata
        cursor.execute('''
            SELECT s.student_id, s.name, s.stream_id, s.image_path, st.name as stream_name 
            FROM students s
            JOIN streams st ON s.stream_id = st.id
            WHERE s.image_path IS NOT NULL
        ''')
        known_face_encodings = []
        known_face_metadata = []
        
        for student_id, name, stream_id, image_path, stream_name in cursor.fetchall():
            try:
                if not os.path.exists(image_path):
                    continue
                    
                image = face_recognition.load_image_file(image_path)
                encodings = face_recognition.face_encodings(image)
                if encodings:
                    known_face_encodings.append(encodings[0])
                    known_face_metadata.append({
                        'student_id': student_id,
                        'name': name,
                        'stream_id': stream_id,
                        'stream_name': stream_name,
                        'image_path': image_path
                    })
                else:
                    print(f"No faces found in image: {image_path}")
            except Exception as e:
                print(f"Error loading {image_path}: {str(e)}")
                
    except sqlite3.Error as e:
        print(f"Database error: {str(e)}")
    finally:
        if conn:
            conn.close()

def get_active_streams():
    """Get list of active streams"""
    conn = None
    try:
        conn = sqlite3.connect('attendance.db')
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, code FROM streams WHERE is_active = 1 ORDER BY name")
        return cursor.fetchall()
    except sqlite3.Error as e:
        print(f"Error getting streams: {str(e)}")
        return []
    finally:
        if conn:
            conn.close()

def get_current_class():
    """Get the currently scheduled class based on system time"""
    conn = None
    try:
        conn = sqlite3.connect('attendance.db')
        cursor = conn.cursor()
        
        now = datetime.now()
        current_time = now.time()
        current_weekday = now.strftime("%A")
        
        cursor.execute('''
            SELECT s.id, s.name, sch.subject 
            FROM schedules sch
            JOIN streams s ON sch.stream_id = s.id
            WHERE sch.day = ? AND sch.start_time <= ? AND sch.end_time >= ?
        ''', (current_weekday, current_time.strftime("%H:%M"), current_time.strftime("%H:%M")))
        
        result = cursor.fetchone()
        if result:
            return {
                'stream_id': result[0],
                'stream_name': result[1],
                'subject': result[2],
                'day': current_weekday,
                'time': now.strftime("%Y-%m-%d %H:%M:%S")
            }
        return None
    except sqlite3.Error as e:
        print(f"Error getting current class: {str(e)}")
        return None
    finally:
        if conn:
            conn.close()

def mark_attendance(student_id, name, stream_id):
    """Mark attendance for a recognized student with timezone awareness and proper locking"""
    current_class = get_current_class()
    if not current_class:
        return False, "No class scheduled at this time"
    
    if int(stream_id) != int(current_class['stream_id']):
        return False, f"Student not in {current_class['stream_name']} stream"
    
    conn = None
    try:
        conn = sqlite3.connect('attendance.db')
        cursor = conn.cursor()
        
        # Set timezone for this connection
        cursor.execute("PRAGMA timezone = 'Asia/Kolkata'")  # Change to your timezone
        
        # Begin immediate transaction to prevent race conditions
        cursor.execute("BEGIN IMMEDIATE TRANSACTION")
        
        # Get current date in database local time
        cursor.execute("SELECT date('now', 'localtime')")
        today = cursor.fetchone()[0]
        
        # Check for existing attendance using database time
        cursor.execute('''
            SELECT 1 FROM attendance 
            WHERE student_id = ? 
              AND subject = ? 
              AND date(timestamp) = ?
            LIMIT 1
        ''', (student_id, current_class['subject'], today))
        
        if cursor.fetchone():
            conn.rollback()
            return False, "Attendance already marked today"
        
        # Insert using database local time
        cursor.execute('''
            INSERT INTO attendance 
            (student_id, name, stream_id, subject, timestamp)
            VALUES (?, ?, ?, ?, datetime('now', 'localtime'))
        ''', (student_id, name, stream_id, current_class['subject']))
        
        conn.commit()
        return True, "Attendance marked successfully"
    
    except sqlite3.IntegrityError as e:
        if "UNIQUE constraint failed" in str(e):
            return False, "Attendance already marked today"
        return False, f"Database error: {str(e)}"
    
    except sqlite3.Error as e:
        if conn:
            conn.rollback()
        return False, f"Database error: {str(e)}"
    
    finally:
        if conn:
            conn.close()

def safe_camera_operation():
    """Decorator to handle camera operations safely"""
    def decorator(func):
        def wrapper(*args, **kwargs):
            global camera_lock
            if camera_lock:
                return None
            camera_lock = True
            try:
                return func(*args, **kwargs)
            except Exception as e:
                print(f"Camera operation error: {str(e)}")
                return None
            finally:
                camera_lock = False
                gc.collect()
        return wrapper
    return decorator

@safe_camera_operation()
def start_camera():
    """Start the USB camera"""
    global camera
    if camera is None or not camera.isOpened():
        camera = cv2.VideoCapture(0)
        camera.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        time.sleep(1)  # Allow camera to warm up

@safe_camera_operation()
def stop_camera():
    """Stop and release camera resources"""
    global camera
    if camera is not None:
        camera.release()
        camera = None

@safe_camera_operation()
def capture_frame():
    """Capture a single frame from camera"""
    global camera
    if camera is None or not camera.isOpened():
        return None
    try:
        ret, frame = camera.read()
        if ret:
            return frame
        return None
    except Exception as e:
        print(f"Frame capture error: {str(e)}")
        return None

def validate_base64(data):
    """Validate and clean base64 data"""
    if data.startswith('data:image'):
        data = data.split(',', 1)[-1]
    
    data = re.sub(r'\s+', '', data)
    
    if len(data) % 4 != 0:
        data += '=' * (4 - len(data) % 4)
    
    return data

def data_url_to_cv2(data_url):
    """Convert base64 data URL to OpenCV image with robust error handling"""
    try:
        if not data_url:
            raise ValueError("Empty image data")
            
        clean_data = validate_base64(data_url)
        
        binary_data = base64.b64decode(clean_data)
        np_arr = np.frombuffer(binary_data, np.uint8)
        img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        
        if img is None:
            raise ValueError("Failed to decode image data")
            
        return img
    except Exception as e:
        print(f"Image conversion error: {str(e)}")
        print(f"Problematic data (first 100 chars): {data_url[:100]}")
        return None

@app.route('/')
def index():
    """Main registration page"""
    start_camera()
    streams = get_active_streams()
    return render_template('admin/register.html', streams=streams)

@app.route('/video_feed')
def video_feed():
    """Video streaming route with face recognition"""
    current_class = get_current_class()
    class_info = f"{current_class['stream_name']} - {current_class['subject']}" if current_class else "No active class"
    
    def generate_frames():
        while True:
            frame = capture_frame()
            if frame is not None:
                try:
                    if time.time() % 0.3 < 0.1:
                        small_frame = cv2.resize(frame, (0, 0), fx=0.3, fy=0.3)
                        face_locations = face_recognition.face_locations(small_frame)
                        face_encodings = face_recognition.face_encodings(small_frame, face_locations)
                        
                        for (top, right, bottom, left), face_encoding in zip(face_locations, face_encodings):
                            if len(known_face_encodings) > 0:
                                # Stricter matching logic
                                face_distances = face_recognition.face_distance(known_face_encodings, face_encoding)
                                best_match_index = np.argmin(face_distances)
                                best_distance = face_distances[best_match_index]
                                
                                # Get all matches that meet our strict threshold
                                matches = [d < FACE_MATCH_THRESHOLD for d in face_distances]
                                matching_indices = [i for i, match in enumerate(matches) if match]
                                
                                # Check if we have enough high-confidence matches
                                if len(matching_indices) >= REQUIRED_MATCHES:
                                    # Verify these matches are for the same person
                                    primary_student = known_face_metadata[best_match_index]['student_id']
                                    consistent_matches = 0
                                    
                                    for idx in matching_indices:
                                        if known_face_metadata[idx]['student_id'] == primary_student:
                                            consistent_matches += 1
                                    
                                    if consistent_matches >= REQUIRED_MATCHES and best_distance < MIN_FACE_CONFIDENCE:
                                        metadata = known_face_metadata[best_match_index]
                                        name = metadata['name']
                                        student_id = metadata['student_id']
                                        stream_id = metadata['stream_id']
                                        stream_name = metadata['stream_name']
                                        
                                        if current_class and int(stream_id) == int(current_class['stream_id']):
                                            success, message = mark_attendance(student_id, name, stream_id)
                                            color = (0, 255, 0) if success else (0, 0, 255)
                                            status = "Present" if success else message
                                        else:
                                            color = (255, 0, 0)
                                            status = "Wrong stream" if current_class else "No class"
                                    else:
                                        name = "Unknown (low confidence)"
                                        color = (0, 0, 255)
                                        status = "Unknown"
                                else:
                                    name = "Unknown (no match)"
                                    color = (0, 0, 255)
                                    status = "Unknown"
                            else:
                                name = "Unknown (no data)"
                                color = (0, 0, 255)
                                status = "Unknown"
                            
                            top *= 2; right *= 2; bottom *= 2; left *= 2
                            cv2.rectangle(frame, (left, top), (right, bottom), color, 2)
                            cv2.putText(frame, name, (left + 6, bottom - 6), 
                                       cv2.FONT_HERSHEY_DUPLEX, 0.5, (255, 255, 255), 1)
                            cv2.putText(frame, status, (left + 6, bottom + 20), 
                                       cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
                    
                    cv2.putText(frame, class_info, (10, 30), 
                               cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
                    
                    _, buffer = cv2.imencode('.jpg', frame)
                    yield (b'--frame\r\n'
                           b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
                except Exception as e:
                    print(f"Frame processing error: {str(e)}")
                    yield (b'--frame\r\n'
                           b'Content-Type: image/jpeg\r\n\r\n' + cv2.imencode('.jpg', np.zeros((480,640,3), dtype=np.uint8))[1].tobytes() + b'\r\n')
            else:
                time.sleep(0.1)
    
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/capture_frame')
def capture_frame_endpoint():
    """Endpoint for capturing single frame"""
    frame = capture_frame()
    if frame is None:
        return Response(status=500)
    _, buffer = cv2.imencode('.jpg', frame)
    return Response(buffer.tobytes(), mimetype='image/jpeg')

@app.route('/submit', methods=['POST'])
def submit():
    """Handle registration form submission"""
    try:
        if 'face_images' not in request.form:
            flash("No face images captured")
            return redirect('/')
            
        student_id = request.form.get('student_id', '').strip().upper()
        name = request.form.get('name', '').strip()
        stream_id = request.form.get('stream', '').strip()
        
        if not all([student_id, name, stream_id]):
            flash("Please provide all required fields")
            return redirect('/')
        
        try:
            face_images = json.loads(request.form['face_images'])
            if not isinstance(face_images, list) or len(face_images) < 3:
                flash("Invalid image data format")
                return redirect('/')
        except (json.JSONDecodeError, TypeError) as e:
            flash("Invalid image data format")
            return redirect('/')
        
        # Validate stream exists
        conn = sqlite3.connect('attendance.db')
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM streams WHERE id = ? AND is_active = 1", (stream_id,))
        if not cursor.fetchone():
            flash("Invalid stream selected")
            return redirect('/')
        
        # Check if student ID already exists (regardless of angles)
        cursor.execute("SELECT COUNT(*) FROM students WHERE student_id = ?", (student_id,))
        if cursor.fetchone()[0] > 0:
            flash("This student ID is already registered!")
            conn.close()
            return redirect('/')

        encodings = []
        saved_paths = []
        angles = ['front', 'left', 'right']
        success_count = 0
        
        # First validate all images and check for existing angles
        for i, img_data in enumerate(face_images[:3]):
            if not img_data or not isinstance(img_data, str):
                continue
                
            frame = data_url_to_cv2(img_data)
            if frame is None:
                continue
                
            try:
                small_frame = cv2.resize(frame, (0, 0), fx=0.3, fy=0.3)
                face_locations = face_recognition.face_locations(small_frame)
                
                if face_locations:
                    face_encoding = face_recognition.face_encodings(small_frame, face_locations)
                    if face_encoding:
                        # Check if this angle already exists for any student
                        cursor.execute(
                            "SELECT COUNT(*) FROM students WHERE student_id = ? AND angle = ?",
                            (student_id, angles[i])
                        )
                        if cursor.fetchone()[0] > 0:
                            flash(f"Angle {angles[i]} already exists for this student!")
                            conn.close()
                            return redirect('/')
                            
                        encodings.append(face_encoding[0])
                        filename = f"{secure_filename(student_id)}_{angles[i]}_{int(time.time())}.jpg"
                        filepath = os.path.join(FACE_DIR, filename)
                        cv2.imwrite(filepath, frame)
                        saved_paths.append((filepath, angles[i]))
                        success_count += 1
            except Exception as e:
                print(f"Error processing image {i}: {str(e)}")
                continue
        
        if success_count < 2:
            flash(f"Only captured {success_count} valid angles. Need at least 2 best shots.")
            conn.close()
            return redirect('/')
            
        # Stricter face validation against existing students
        if len(known_face_encodings) > 0:
            match_counts = {}
            for encoding in encodings:
                face_distances = face_recognition.face_distance(known_face_encodings, encoding)
                matching_indices = [i for i, d in enumerate(face_distances) if d < FACE_MATCH_THRESHOLD]
                
                for idx in matching_indices:
                    student = known_face_metadata[idx]['student_id']
                    match_counts[student] = match_counts.get(student, 0) + 1
            
            # Check if any existing student has enough matches
            for student, count in match_counts.items():
                if count >= REQUIRED_MATCHES:
                    flash(f"This face matches existing student {student}!")
                    conn.close()
                    # Clean up any saved images
                    for path, _ in saved_paths:
                        try:
                            os.remove(path)
                        except:
                            pass
                    return redirect('/')
        
        # Save to database
        try:
            for path, angle in saved_paths:
                cursor.execute(
                    "INSERT INTO students (student_id, name, stream_id, image_path, angle) VALUES (?, ?, ?, ?, ?)",
                    (student_id, name, stream_id, path, angle)
                )
            conn.commit()
            
            # Add to known faces
            for encoding in encodings:
                known_face_encodings.append(encoding)
                cursor.execute("SELECT name FROM streams WHERE id = ?", (stream_id,))
                stream_name = cursor.fetchone()[0]
                known_face_metadata.append({
                    'student_id': student_id,
                    'name': name,
                    'stream_id': stream_id,
                    'stream_name': stream_name,
                    'image_path': path
                })
            
            flash(f"Successfully registered {name} ({student_id})!")
        except sqlite3.Error as e:
            flash(f"Database error: {str(e)}")
        finally:
            conn.close()
                
        return redirect('/')
    except Exception as e:
        flash(f"Error: {str(e)}")
        return redirect('/')

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        conn = sqlite3.connect('attendance.db')
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM admin_users WHERE username = ? AND password = ?", (username, password))
        user = cursor.fetchone()
        conn.close()
        
        if user:
            session['logged_in'] = True
            session['username'] = username
            return redirect(url_for('admin_dashboard'))
        else:
            flash("Invalid credentials")
    
    return render_template('admin/login.html')

@app.route("/logout")
def logout():
    session.clear()  # Clear user session
    return render_template("admin/logout.html")  # Render the logout page


@app.route('/admin/dashboard')
@login_required
def admin_dashboard():
    conn = sqlite3.connect('attendance.db')
    cursor = conn.cursor()
    
    # Get stats
    cursor.execute("SELECT COUNT(DISTINCT student_id) FROM students")
    total_students = cursor.fetchone()[0]
    
    # Fix this line - use datetime.now() instead of date.today()
    today = datetime.now().strftime("%Y-%m-%d")
    cursor.execute("SELECT COUNT(DISTINCT student_id) FROM attendance WHERE date(timestamp) = ?", (today,))
    today_attendance = cursor.fetchone()[0]
    
    # Get current classes
    current_time = datetime.now().time()
    current_weekday = datetime.now().strftime("%A")
    
    cursor.execute('''
        SELECT s.name, sch.subject, sch.start_time, sch.end_time 
        FROM schedules sch
        JOIN streams s ON sch.stream_id = s.id
        WHERE sch.day = ? AND sch.start_time <= ? AND sch.end_time >= ?
    ''', (current_weekday, current_time.strftime("%H:%M"), current_time.strftime("%H:%M")))
    
    current_classes = cursor.fetchall()
    conn.close()
    
    return render_template('admin/dashboard.html', 
                         total_students=total_students,
                         today_attendance=today_attendance,
                         current_classes=current_classes,
                         current_date=date.today())

@app.route('/admin/students')
@login_required
def manage_students():
    conn = sqlite3.connect('attendance.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT s.student_id, s.name, st.name as stream_name, 
               COUNT(s.id) as angles, MAX(s.date_added) as last_updated
        FROM students s
        JOIN streams st ON s.stream_id = st.id
        GROUP BY s.student_id, s.name, st.name
        ORDER BY s.name
    ''')
    students = cursor.fetchall()
    conn.close()
    return render_template('admin/students.html', students=students)

@app.route('/admin/student/<path:student_id>')
@login_required
def view_student(student_id):
    conn = sqlite3.connect('attendance.db')
    cursor = conn.cursor()

    # Get basic student info
    cursor.execute('''
        SELECT DISTINCT s.student_id, s.name, st.name as stream_name, 
               (SELECT COUNT(*) FROM students WHERE student_id = s.student_id) as angle_count,
               MAX(s.date_added) as last_updated
        FROM students s
        JOIN streams st ON s.stream_id = st.id
        WHERE s.student_id = ?
        GROUP BY s.student_id, s.name, st.name
    ''', (student_id,))
    student = cursor.fetchone()

    if not student:
        flash("Student not found")
        conn.close()
        return redirect(url_for('manage_students'))

    # Get all face angles
    cursor.execute('''
        SELECT angle, image_path 
        FROM students 
        WHERE student_id = ?
        ORDER BY angle
    ''', (student_id,))
    faces = cursor.fetchall()

    # Get attendance history
    cursor.execute('''
        SELECT subject, status, timestamp 
        FROM attendance 
        WHERE student_id = ?
        ORDER BY timestamp DESC
        LIMIT 50
    ''', (student_id,))
    attendance = cursor.fetchall()

    conn.close()

    return render_template('admin/student_detail.html', 
                         student=student,
                         faces=faces,
                         attendance=attendance)



@app.route('/admin/streams')
@login_required
def manage_streams():
    conn = sqlite3.connect('attendance.db')
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, code, is_active FROM streams ORDER BY name")
    streams = cursor.fetchall()
    conn.close()
    return render_template('admin/streams.html', streams=streams)

@app.route('/admin/streams/add', methods=['POST'])
@login_required
def add_stream():
    name = request.form.get('name', '').strip()
    code = request.form.get('code', '').strip().upper()
    is_active = 1 if request.form.get('is_active') else 0
    
    if not name or not code:
        flash("Both name and code are required")
        return redirect(url_for('manage_streams'))
    
    conn = None
    try:
        conn = sqlite3.connect('attendance.db')
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO streams (name, code, is_active) VALUES (?, ?, ?)",
            (name, code, is_active)
        )
        conn.commit()
        flash("Stream added successfully")
    except sqlite3.IntegrityError:
        flash("Stream with this name or code already exists")
    except sqlite3.Error as e:
        flash(f"Database error: {str(e)}")
    finally:
        if conn:
            conn.close()
    
    return redirect(url_for('manage_streams'))

@app.route('/admin/streams/update/<int:stream_id>', methods=['POST'])
@login_required
def update_stream(stream_id):
    name = request.form.get('name', '').strip()
    code = request.form.get('code', '').strip().upper()
    is_active = 1 if request.form.get('is_active') else 0
    
    if not name or not code:
        flash("Both name and code are required")
        return redirect(url_for('manage_streams'))
    
    conn = None
    try:
        conn = sqlite3.connect('attendance.db')
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE streams SET name = ?, code = ?, is_active = ? WHERE id = ?",
            (name, code, is_active, stream_id)
        )
        conn.commit()
        flash("Stream updated successfully")
    except sqlite3.IntegrityError:
        flash("Another stream with this name or code already exists")
    except sqlite3.Error as e:
        flash(f"Database error: {str(e)}")
    finally:
        if conn:
            conn.close()
    
    return redirect(url_for('manage_streams'))

@app.route('/admin/streams/delete/<int:stream_id>')
@login_required
def delete_stream(stream_id):
    conn = None
    try:
        conn = sqlite3.connect('attendance.db')
        cursor = conn.cursor()
        
        # Check if stream has students
        cursor.execute("SELECT COUNT(*) FROM students WHERE stream_id = ?", (stream_id,))
        if cursor.fetchone()[0] > 0:
            flash("Cannot delete stream with registered students")
            return redirect(url_for('manage_streams'))
        
        cursor.execute("DELETE FROM streams WHERE id = ?", (stream_id,))
        conn.commit()
        flash("Stream deleted successfully")
    except sqlite3.Error as e:
        flash(f"Database error: {str(e)}")
    finally:
        if conn:
            conn.close()
    
    return redirect(url_for('manage_streams'))

@app.route('/admin/attendance')
@login_required
def view_attendance():
    stream = request.args.get('stream')
    date_param = request.args.get('date', date.today().isoformat())
    subject = request.args.get('subject')
    
    try:
        # Validate date format
        selected_date = datetime.strptime(date_param, "%Y-%m-%d").date()
    except ValueError:
        flash("Invalid date format. Using today's date instead.", 'warning')
        selected_date = date.today()
        date_param = selected_date.isoformat()

    conn = sqlite3.connect('attendance.db')
    conn.row_factory = sqlite3.Row  # Return rows as dictionaries
    cursor = conn.cursor()

    # Main query with filters
    query = '''
        SELECT a.student_id, a.name, s.name as stream_name, a.subject, a.status, a.timestamp 
        FROM attendance a
        JOIN streams s ON a.stream_id = s.id
        WHERE date(a.timestamp) = ?
    '''
    params = [date_param]
    
    if stream:
        query += " AND a.stream_id = ?"
        params.append(stream)
    if subject:
        query += " AND a.subject = ?"
        params.append(subject)
    
    query += " ORDER BY a.timestamp DESC"
    
    cursor.execute(query, params)
    records = cursor.fetchall()

    # Get filter options
    cursor.execute("SELECT id, name FROM streams ORDER BY name")
    streams = cursor.fetchall()
    
    cursor.execute("SELECT DISTINCT subject FROM schedules ORDER BY subject")
    subjects = [row[0] for row in cursor.fetchall()]
    
    conn.close()
    
    return render_template('admin/attendance.html',
                         records=records,
                         streams=streams,
                         subjects=subjects,
                         current_stream=stream,
                         current_date=date_param,
                         current_subject=subject,
                         now=datetime.now())

@app.route('/admin/export_attendance')
@login_required
def export_attendance():
    import csv
    from io import StringIO
    from flask import Response

    stream = request.args.get('stream')
    date_param = request.args.get('date', date.today().isoformat())
    subject = request.args.get('subject')

    conn = sqlite3.connect('attendance.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    query = '''
        SELECT a.student_id, a.name, s.name as stream_name, a.subject, a.status, a.timestamp 
        FROM attendance a
        JOIN streams s ON a.stream_id = s.id
        WHERE date(a.timestamp) = ?
    '''
    params = [date_param]
    
    if stream:
        query += " AND a.stream_id = ?"
        params.append(stream)
    if subject:
        query += " AND a.subject = ?"
        params.append(subject)

    query += " ORDER BY a.timestamp DESC"
    
    cursor.execute(query, params)
    records = cursor.fetchall()
    conn.close()

    # Generate CSV
    si = StringIO()
    writer = csv.writer(si)
    writer.writerow(['Student ID', 'Name', 'Stream', 'Subject', 'Status', 'Timestamp'])
    
    for record in records:
        timestamp = record['timestamp']
        if not isinstance(timestamp, str):
            timestamp = timestamp.strftime('%Y-%m-%d %H:%M:%S')
        
        writer.writerow([
            record['student_id'],
            record['name'],
            record['stream_name'],
            record['subject'],
            record['status'],
            timestamp
        ])

    output = si.getvalue()
    return Response(
        output,
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment;filename=attendance_{date_param}.csv'}
    )


@app.route('/admin/schedules')
@login_required
def manage_schedules():
    """Schedule management view"""
    conn = sqlite3.connect('attendance.db')
    
    # Set row_factory to return dictionaries
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Get all schedules with stream names
    cursor.execute('''
        SELECT s.id, st.name as stream_name, s.subject, s.day, s.start_time, s.end_time
        FROM schedules s
        JOIN streams st ON s.stream_id = st.id
        ORDER BY st.name, s.day, s.start_time
    ''')
    schedules = cursor.fetchall()  # Now returns sqlite3.Row objects that behave like dictionaries
    
    # Get all streams for the form (keep as tuples for the select options)
    cursor.execute("SELECT id, name FROM streams ORDER BY name")
    streams = cursor.fetchall()
    
    # Get all unique subjects
    cursor.execute("SELECT DISTINCT subject FROM schedules ORDER BY subject")
    subjects = [row[0] for row in cursor.fetchall()]
    
    conn.close()
    
    now = datetime.now()
    
    return render_template('admin/schedules.html',
                         schedules=schedules,
                         streams=streams,
                         subjects=subjects,
                         now=now,
                         timedelta=timedelta)

@app.route('/admin/schedules/add', methods=['POST'])
@login_required
def add_schedule():
    """Add a new schedule"""
    stream_id = request.form.get('stream_id')
    subject = request.form.get('subject')
    day = request.form.get('day')
    start_time = request.form.get('start_time')
    end_time = request.form.get('end_time')

    if not all([stream_id, subject, day, start_time, end_time]):
        flash("All fields are required")
        return redirect(url_for('manage_schedules'))

    conn = sqlite3.connect('attendance.db')
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO schedules (stream_id, subject, day, start_time, end_time) VALUES (?, ?, ?, ?, ?)",
            (stream_id, subject, day, start_time, end_time)
        )
        conn.commit()
        flash("Schedule added successfully")
    except sqlite3.IntegrityError:
        flash("A schedule for this stream, subject and day already exists")
    except sqlite3.Error as e:
        flash(f"Database error: {str(e)}")
    finally:
        conn.close()
    
    return redirect(url_for('manage_schedules'))

@app.route('/admin/schedules/delete/<int:schedule_id>')
@login_required
def delete_schedule(schedule_id):
    """Delete a schedule"""
    conn = sqlite3.connect('attendance.db')
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
        conn.commit()
        flash("Schedule deleted successfully")
    except sqlite3.Error as e:
        flash(f"Database error: {str(e)}")
    finally:
        conn.close()
    return redirect(url_for('manage_schedules'))


@app.route('/stop')
def stop():
    """Stop camera and return to home"""
    stop_camera()
    return redirect('/')

if __name__ == '__main__':
    ensure_database_tables()
    cleanup_orphaned_files()
    init_db()
    try:
        app.run(host='0.0.0.0', port=5000, debug=True, threaded=True)
    finally:
        stop_camera()