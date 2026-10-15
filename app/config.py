from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from pathlib import Path


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 310000).hex()
    return f'pbkdf2_sha256${salt}${digest}'


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, salt, _ = encoded.split('$')
        return algorithm == 'pbkdf2_sha256' and hmac.compare_digest(hash_password(password, salt), encoded)
    except (ValueError, TypeError):
        return False


@dataclass
class Settings:
    data_dir: Path
    ads_token: str
    admin_username: str
    password_hash: str
    session_secret: str
    public_url: str
    secure_cookie: bool = True
    run_worker: bool = True
    sync_interval: int = 300
    max_batch_size: int = 50
    max_batch_budget: float = 200

    @classmethod
    def from_env(cls):
        return cls(
            Path(os.getenv('DATA_DIR', '/data')),
            os.getenv('TelegramAds_API_Token', ''),
            os.getenv('ADMIN_USERNAME', 'admin'),
            os.getenv('ADMIN_PASSWORD_HASH', ''),
            os.getenv('SESSION_SECRET', ''),
            os.getenv('PUBLIC_URL', 'http://localhost:8000').rstrip('/'),
            os.getenv('COOKIE_SECURE', 'true').lower() == 'true',
            os.getenv('RUN_WORKER', 'true').lower() == 'true',
            max(60, int(os.getenv('SYNC_INTERVAL_SECONDS', '300'))),
            min(100, int(os.getenv('MAX_BATCH_SIZE', '50'))),
            float(os.getenv('MAX_BATCH_BUDGET', '200')),
        )

    def validate(self):
        if len(self.session_secret) < 32:
            raise RuntimeError('SESSION_SECRET must contain at least 32 characters')
        if not self.password_hash.startswith('pbkdf2_sha256$'):
            raise RuntimeError('ADMIN_PASSWORD_HASH must be configured')
        if not self.ads_token:
            raise RuntimeError('TelegramAds_API_Token must be configured')
