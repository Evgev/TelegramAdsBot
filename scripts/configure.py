"""Create private deployment config without displaying credentials."""
import argparse
import json
import secrets
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import hash_password

parser = argparse.ArgumentParser()
parser.add_argument('--token-env', type=Path, required=True)
parser.add_argument('--output', type=Path, default=Path('.'))
parser.add_argument('--public-url', default='https://ads.papin-free.site')
args = parser.parse_args()
values = {}
for line in args.token_env.read_text().splitlines():
    if line.strip().startswith('TelegramAds_API_Token='):
        parsed = shlex.split(line.split('=', 1)[1], comments=True)
        values['TelegramAds_API_Token'] = parsed[0] if parsed else ''
if not values.get('TelegramAds_API_Token'):
    raise SystemExit('TelegramAds_API_Token is missing')
args.output.mkdir(parents=True, exist_ok=True)
env = args.output / '.env'
access = args.output / 'access.txt'
if env.exists() or access.exists():
    raise SystemExit('Config already exists; refusing to overwrite credentials')
password = secrets.token_urlsafe(24)
values.update(DOMAIN=args.public_url.split('://', 1)[1], PUBLIC_URL=args.public_url,
              ADMIN_USERNAME='admin', ADMIN_PASSWORD_HASH=hash_password(password),
              SESSION_SECRET=secrets.token_urlsafe(48), DATA_DIR='/data',
              SYNC_INTERVAL_SECONDS='300', MAX_BATCH_SIZE='50', MAX_BATCH_BUDGET='200',
              COOKIE_SECURE='true', RUN_WORKER='true')
def quote(value):
    if "'" in value or '\n' in value:
        raise ValueError('Unsupported dotenv value')
    return "'" + value + "'"
env.write_text(''.join(k+'='+quote(v)+'\n' for k, v in values.items()))
env.chmod(0o600)
access.write_text(json.dumps({'url':args.public_url, 'username':'admin', 'password':password}, ensure_ascii=False, indent=2)+'\n')
access.chmod(0o600)
print('Private files created: .env and access.txt')
