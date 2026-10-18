"""Interactive operator-only login. Never accepts credentials through HTTP or argv."""
import asyncio
import os
import shlex
from getpass import getpass
from pathlib import Path


async def main():
    os.umask(0o077)
    env = Path('.env')
    if env.is_file():
        for line in env.read_text().splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                name, value = line.split('=', 1)
                if name.strip() in {'TELEGRAM_API_ID', 'TELEGRAM_API_HASH', 'TELEGRAM_SESSION_PATH', 'DATA_DIR'}:
                    values = shlex.split(value)
                    os.environ.setdefault(name.strip(), values[0] if values else '')
    api_id = int(os.environ.get('TELEGRAM_API_ID', '0') or '0')
    api_hash = os.environ.get('TELEGRAM_API_HASH', '')
    if not api_id or not api_hash:
        raise SystemExit('Добавьте TELEGRAM_API_ID и TELEGRAM_API_HASH в .env или окружение')
    session = Path(os.environ.get('TELEGRAM_SESSION_PATH', str(Path(os.environ.get('DATA_DIR', './data')) / 'telegram')))
    session.parent.mkdir(parents=True, exist_ok=True)
    from telethon import TelegramClient
    client = TelegramClient(str(session), api_id, api_hash)
    try:
        await client.start(phone=lambda: getpass('Номер отдельного Telegram-аккаунта (+...): '),
                           code_callback=lambda: getpass('Код входа из Telegram: '),
                           password=lambda: getpass('Пароль двухэтапной аутентификации: '))
        if await client.is_bot():
            raise SystemExit('Нужен пользовательский аккаунт, а не токен бота')
        print('Пользовательская сессия Telegram сохранена. Можно запускать сервис.')
    finally:
        await client.disconnect()
        path = session if str(session).endswith('.session') else Path(str(session) + '.session')
        if path.exists():
            path.chmod(0o600)


if __name__ == '__main__':
    asyncio.run(main())
