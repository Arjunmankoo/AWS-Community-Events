"""Reconstructed AWS events portfolio project; local SQLite or RDS MySQL + S3."""
import io
import os
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from flask import Flask, abort, flash, redirect, render_template, request, send_file, session, url_for
from PIL import Image, UnidentifiedImageError
from werkzeug.security import check_password_hash
from dotenv import load_dotenv

SCHEMA = '''CREATE TABLE IF NOT EXISTS events (
 id VARCHAR(32) PRIMARY KEY, title VARCHAR(120) NOT NULL,
 description TEXT NOT NULL, location VARCHAR(160) NOT NULL,
 starts_at VARCHAR(16) NOT NULL, image_key VARCHAR(100),
 created_at VARCHAR(32) NOT NULL)'''


def create_app(overrides=None):
    load_dotenv()
    app = Flask(__name__)
    local = os.getenv('APP_ENV', 'local') != 'production'
    app.config.update(
        SECRET_KEY=os.getenv('SECRET_KEY') or (secrets.token_hex(32) if local else None),
        DATABASE_URL=os.getenv('DATABASE_URL', 'sqlite'),
        DATA_DIR=os.getenv('DATA_DIR', str(Path(__file__).parent / 'instance')),
        STORAGE_BACKEND=os.getenv('STORAGE_BACKEND', 'local'),
        S3_BUCKET=os.getenv('S3_BUCKET', ''), AWS_REGION=os.getenv('AWS_REGION', 'eu-west-2'),
        ADMIN_PASSWORD_HASH=os.getenv('ADMIN_PASSWORD_HASH', ''),
        RDS_HOST=os.getenv('RDS_HOST', ''), RDS_PORT=int(os.getenv('RDS_PORT', '3306')),
        RDS_DATABASE=os.getenv('RDS_DATABASE', 'events'), RDS_USER=os.getenv('RDS_USER', ''),
        RDS_PASSWORD=os.getenv('RDS_PASSWORD', ''), RDS_SSL_CA=os.getenv('RDS_SSL_CA', ''),
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=not local, MAX_CONTENT_LENGTH=6 * 1024 * 1024,
    )
    if overrides:
        app.config.update(overrides)
    if not app.config['SECRET_KEY']:
        raise RuntimeError('SECRET_KEY is required in production')
    if not local and (app.config['DATABASE_URL'] != 'mysql' or app.config['STORAGE_BACKEND'] != 's3'):
        raise RuntimeError('Production requires mysql and s3 storage')
    if app.config['STORAGE_BACKEND'] not in ('local', 's3'):
        raise RuntimeError('STORAGE_BACKEND must be local or s3')
    data = Path(app.config['DATA_DIR'])
    data.mkdir(parents=True, exist_ok=True)
    uploads = data / 'uploads'
    uploads.mkdir(exist_ok=True)

    def connect():
        if app.config['DATABASE_URL'] == 'sqlite':
            conn = sqlite3.connect(data / 'events.db')
            conn.row_factory = sqlite3.Row
            return conn
        if app.config['DATABASE_URL'] != 'mysql':
            raise RuntimeError('DATABASE_URL must be sqlite or mysql')
        import pymysql
        ca = app.config['RDS_SSL_CA']
        if not ca or not Path(ca).is_file():
            raise RuntimeError('RDS_SSL_CA must point to the RDS CA bundle')
        return pymysql.connect(host=app.config['RDS_HOST'], port=app.config['RDS_PORT'],
            user=app.config['RDS_USER'], password=app.config['RDS_PASSWORD'],
            database=app.config['RDS_DATABASE'], charset='utf8mb4',
            cursorclass=pymysql.cursors.DictCursor,
            ssl_ca=ca, ssl_verify_cert=True, ssl_verify_identity=True)

    def query(sql, args=(), write=False):
        conn = connect()
        try:
            cur = conn.cursor()
            cur.execute(sql.replace('?', '%s') if app.config['DATABASE_URL'] == 'mysql' else sql, args)
            if write:
                conn.commit()
                return None
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def s3():
        import boto3
        return boto3.client('s3', region_name=app.config['AWS_REGION'])

    def save_image(file):
        if not file or not file.filename:
            return None
        raw = file.read(5 * 1024 * 1024 + 1)
        if len(raw) > 5 * 1024 * 1024:
            raise ValueError('Image must be 5 MB or smaller.')
        try:
            with Image.open(io.BytesIO(raw)) as image:
                if image.format not in ('JPEG', 'PNG', 'WEBP') or image.width * image.height > 20_000_000:
                    raise ValueError('Use a JPEG, PNG or WebP image up to 20 megapixels.')
                image.load()
                clean = image.convert('RGB')
                clean.thumbnail((1600, 1600))
                output = io.BytesIO()
                clean.save(output, format='JPEG', quality=85)
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise ValueError('Upload a valid image.') from exc
        key = f'events/{uuid.uuid4().hex}.jpg'
        if app.config['STORAGE_BACKEND'] == 's3':
            s3().put_object(Bucket=app.config['S3_BUCKET'], Key=key,
                Body=output.getvalue(), ContentType='image/jpeg', ServerSideEncryption='AES256')
        else:
            (uploads / Path(key).name).write_bytes(output.getvalue())
        return key

    def delete_image(key):
        if not key:
            return
        if app.config['STORAGE_BACKEND'] == 's3':
            s3().delete_object(Bucket=app.config['S3_BUCKET'], Key=key)
        else:
            (uploads / Path(key).name).unlink(missing_ok=True)

    def admin_required(fn):
        @wraps(fn)
        def inner(*args, **kwargs):
            if not session.get('admin'):
                return redirect(url_for('login'))
            return fn(*args, **kwargs)
        return inner

    @app.before_request
    def csrf_guard():
        if 'csrf' not in session:
            session['csrf'] = secrets.token_hex(32)
        if request.method == 'POST':
            supplied = request.form.get('csrf', '')
            if not secrets.compare_digest(supplied, session['csrf']):
                abort(400, 'Invalid form token. Reload the page and try again.')

    @app.after_request
    def security_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self'; style-src 'self'; form-action 'self'; frame-ancestors 'none'"
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/')
    def index():
        events = query('SELECT * FROM events ORDER BY starts_at ASC')
        return render_template('index.html', events=events)

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            stored = app.config['ADMIN_PASSWORD_HASH']
            if stored and check_password_hash(stored, request.form.get('password', '')):
                session.clear()
                session['admin'] = True
                session['csrf'] = secrets.token_hex(32)
                return redirect(url_for('index'))
            flash('Incorrect password, or administrator access is not configured.')
        return render_template('login.html')

    @app.post('/logout')
    def logout():
        session.clear()
        return redirect(url_for('index'))

    @app.route('/events/new', methods=['GET', 'POST'])
    @admin_required
    def new_event():
        if request.method == 'POST':
            fields = {k: request.form.get(k, '').strip() for k in ('title', 'description', 'location', 'starts_at')}
            try:
                for field, limit in [('title', 120), ('description', 4000), ('location', 160)]:
                    if not 1 <= len(fields[field]) <= limit:
                        raise ValueError(f'{field.capitalize()} must contain 1–{limit} characters.')
                try:
                    datetime.strptime(fields['starts_at'], '%Y-%m-%dT%H:%M')
                except ValueError as exc:
                    raise ValueError('Choose a valid event date and time.') from exc
                key = save_image(request.files.get('image'))
                try:
                    query('INSERT INTO events (id,title,description,location,starts_at,image_key,created_at) VALUES (?,?,?,?,?,?,?)',
                        (uuid.uuid4().hex, fields['title'], fields['description'], fields['location'], fields['starts_at'], key, datetime.now(timezone.utc).isoformat()), write=True)
                except Exception:
                    # Best-effort compensation: do not leave an upload after a failed DB write.
                    try:
                        delete_image(key)
                    except Exception:
                        app.logger.exception('Could not clean up image after database failure')
                    raise
                flash('Event created.')
                return redirect(url_for('index'))
            except ValueError as exc:
                flash(str(exc))
                return render_template('new.html'), 400
        return render_template('new.html')

    @app.post('/events/<event_id>/delete')
    @admin_required
    def remove_event(event_id):
        rows = query('SELECT * FROM events WHERE id=?', (event_id,))
        if not rows:
            abort(404)
        # Keep DB row if storage deletion fails, allowing a retry.
        delete_image(rows[0]['image_key'])
        query('DELETE FROM events WHERE id=?', (event_id,), write=True)
        flash('Event deleted.')
        return redirect(url_for('index'))

    @app.get('/events/<event_id>/image')
    def event_image(event_id):
        rows = query('SELECT image_key FROM events WHERE id=?', (event_id,))
        if not rows or not rows[0]['image_key']:
            abort(404)
        key = rows[0]['image_key']
        if app.config['STORAGE_BACKEND'] == 's3':
            # Server proxies the private object; no public bucket access is needed.
            obj = s3().get_object(Bucket=app.config['S3_BUCKET'], Key=key)
            body = obj['Body']
            try:
                content = body.read()
            finally:
                body.close()
            return send_file(io.BytesIO(content), mimetype='image/jpeg')
        return send_file(uploads / Path(key).name, mimetype='image/jpeg')

    @app.get('/health')
    def health():
        return {'status': 'ok'}

    @app.errorhandler(413)
    def too_large(_):
        return render_template('error.html', message='Upload is too large. Images must be 5 MB or smaller.'), 413

    @app.errorhandler(500)
    def failure(_):
        return render_template('error.html', message='The request could not be completed. Please try again.'), 500

    @app.cli.command('init-db')
    def init_db():
        query(SCHEMA, write=True)
        print('Events table ready.')

    if app.config['DATABASE_URL'] == 'sqlite':
        query(SCHEMA, write=True)
    return app
