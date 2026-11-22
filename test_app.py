import io
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
from PIL import Image
from werkzeug.security import generate_password_hash
from app import create_app

@pytest.fixture
def app(tmp_path):
    return create_app({'TESTING': True, 'SECRET_KEY': 'test-only', 'DATA_DIR': str(tmp_path),
        'DATABASE_URL': 'sqlite', 'STORAGE_BACKEND': 'local',
        'ADMIN_PASSWORD_HASH': generate_password_hash('test-password')})

@pytest.fixture
def client(app):
    return app.test_client()

def token(client):
    client.get('/')
    with client.session_transaction() as s:
        return s['csrf']

def login(client):
    return client.post('/login', data={'csrf': token(client), 'password': 'test-password'})

def event(client, **extra):
    fields = {'csrf': token(client), 'title': 'Community workshop', 'description': 'Learn together',
        'location': 'Solihull', 'starts_at': '2027-05-01T12:30'}
    fields.update(extra)
    return client.post('/events/new', data=fields)

def image():
    b = io.BytesIO()
    Image.new('RGB', (20, 20), 'green').save(b, 'PNG')
    b.seek(0)
    return b

def ids(app):
    import sqlite3
    with sqlite3.connect(Path(app.config['DATA_DIR']) / 'events.db') as db:
        return db.execute('SELECT id,image_key FROM events').fetchall()

def test_public_list_and_health(client):
    assert client.get('/').status_code == 200
    assert client.get('/health').json == {'status': 'ok'}

def test_admin_required(client):
    assert client.get('/events/new').status_code == 302

def test_csrf_required(client):
    assert client.post('/login', data={'password': 'test-password'}).status_code == 400

def test_login_and_logout(client):
    assert login(client).status_code == 302
    assert client.get('/events/new').status_code == 200
    assert client.post('/logout', data={'csrf': token(client)}).status_code == 302
    assert client.get('/events/new').status_code == 302

def test_bad_password(client):
    client.post('/login', data={'csrf': token(client), 'password': 'wrong'})
    assert client.get('/events/new').status_code == 302

def test_create_and_delete(client, app):
    login(client)
    assert event(client).status_code == 302
    identifier, _ = ids(app)[0]
    assert b'Community workshop' in client.get('/').data
    assert client.post(f'/events/{identifier}/delete', data={'csrf': token(client)}).status_code == 302
    assert ids(app) == []

def test_invalid_date_and_length(client, app):
    login(client)
    assert event(client, starts_at='not-a-date').status_code == 400
    assert event(client, title='x' * 121).status_code == 400
    assert ids(app) == []

def test_html_escaped(client):
    login(client)
    event(client, title='<script>alert(1)</script>')
    html = client.get('/').data
    assert b'&lt;script&gt;' in html and b'<script>alert(1)</script>' not in html

def test_local_image_roundtrip(client, app):
    login(client)
    assert event(client, image=(image(), '../../image.png')).status_code == 302
    identifier, key = ids(app)[0]
    response = client.get(f'/events/{identifier}/image')
    assert response.status_code == 200 and response.mimetype == 'image/jpeg'
    target = Path(app.config['DATA_DIR']) / 'uploads' / Path(key).name
    assert target.is_file()
    client.post(f'/events/{identifier}/delete', data={'csrf': token(client)})
    assert not target.exists()

def test_invalid_upload(client, app):
    login(client)
    assert event(client, image=(io.BytesIO(b'not-an-image'), 'fake.png')).status_code == 400
    assert ids(app) == []

def test_missing_image_and_missing_event(client):
    assert client.get('/events/missing/image').status_code == 404
    login(client)
    assert client.post('/events/missing/delete', data={'csrf': token(client)}).status_code == 404

def test_s3_calls(client, app, monkeypatch):
    import boto3
    store = {}
    class FakeS3:
        def put_object(self, **kwargs):
            assert kwargs['Bucket'] == 'test-bucket'
            assert kwargs['ServerSideEncryption'] == 'AES256'
            store[kwargs['Key']] = kwargs['Body']
        def get_object(self, **kwargs):
            return {'Body': io.BytesIO(store[kwargs['Key']])}
        def delete_object(self, **kwargs):
            del store[kwargs['Key']]
    monkeypatch.setattr(boto3, 'client', lambda *a, **k: FakeS3())
    app.config.update(STORAGE_BACKEND='s3', S3_BUCKET='test-bucket')
    login(client)
    assert event(client, image=(image(), 'picture.png')).status_code == 302
    identifier, _ = ids(app)[0]
    assert client.get(f'/events/{identifier}/image').status_code == 200
    client.post(f'/events/{identifier}/delete', data={'csrf': token(client)})
    assert not store

def test_failed_database_write_cleans_image(client, app, monkeypatch):
    login(client)
    import sqlite3
    with sqlite3.connect(Path(app.config['DATA_DIR']) / 'events.db') as db:
        db.execute('DROP TABLE events')
    with pytest.raises(sqlite3.OperationalError):
        event(client, image=(image(), 'picture.png'))
    assert not list((Path(app.config['DATA_DIR']) / 'uploads').iterdir())
