"""Generate local-only environment values without printing passwords."""
import getpass
import secrets
from pathlib import Path
from werkzeug.security import generate_password_hash

def main():
    path = Path('.env')
    if path.exists():
        raise SystemExit('.env already exists; edit it manually instead of overwriting it.')
    password = getpass.getpass('Choose an administrator password (12+ characters): ')
    if len(password) < 12:
        raise SystemExit('Use at least 12 characters.')
    if password != getpass.getpass('Confirm password: '):
        raise SystemExit('Passwords did not match.')
    path.write_text('APP_ENV=local\nDATABASE_URL=sqlite\nSTORAGE_BACKEND=local\n'
        f'SECRET_KEY={secrets.token_hex(32)}\nADMIN_PASSWORD_HASH={generate_password_hash(password)}\n')
    path.chmod(0o600)
    print('Created local .env. Start the app with: flask --app app run')

if __name__ == '__main__':
    main()
