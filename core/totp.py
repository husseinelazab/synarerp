"""رموز TOTP (RFC 6238) المتوافقة مع Google Authenticator وMicrosoft Authenticator وغيرهما."""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

STEP = 30
DIGITS = 6


def generate_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret):
    return base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)


def code_at(secret, step):
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** DIGITS).zfill(DIGITS)


def current_step(now=None):
    return int((now if now is not None else time.time()) // STEP)


def verify(secret, code, last_step=0, now=None, window=1):
    """يعيد رقم الخطوة الزمنية المطابقة (لمنع إعادة استخدامها)، أو None. يقبل انحراف ساعة ±30 ثانية."""
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(code) != DIGITS:
        return None
    step = current_step(now)
    for candidate in range(step - window, step + window + 1):
        if candidate > last_step and hmac.compare_digest(code_at(secret, candidate), code):
            return candidate
    return None


def provisioning_uri(secret, account, issuer):
    label = quote(f"{issuer}:{account}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits={DIGITS}&period={STEP}"


def new_recovery_codes(count=10):
    codes = ["-".join(secrets.token_hex(3)[:5] for _ in range(2)).upper() for _ in range(count)]
    return codes, [hash_code(c) for c in codes]


def hash_code(code):
    return hashlib.sha256(code.strip().upper().encode()).hexdigest()


def _fernet():
    from cryptography.fernet import Fernet
    from django.conf import settings

    key = hashlib.sha256((settings.SECRET_KEY + ":synar-2fa").encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_secret(secret):
    return _fernet().encrypt(secret.encode()).decode()


def decrypt_secret(token):
    return _fernet().decrypt(token.encode()).decode()
