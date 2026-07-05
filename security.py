"""비밀번호 해시 + API키 암호화(Fernet) + 세션 토큰."""
import base64
import hashlib
import hmac
import pathlib
import secrets

from cryptography.fernet import Fernet

HERE = pathlib.Path(__file__).resolve().parent
SECRET_FILE = HERE / "data" / ".secret"

_ITER = 240_000


def _fernet() -> Fernet:
    SECRET_FILE.parent.mkdir(exist_ok=True)
    if not SECRET_FILE.exists():
        SECRET_FILE.write_bytes(Fernet.generate_key())
        SECRET_FILE.chmod(0o600)
    return Fernet(SECRET_FILE.read_bytes())


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITER)
    return base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_b64, dk_b64 = stored.split("$", 1)
        salt = base64.b64decode(salt_b64)
        expect = base64.b64decode(dk_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITER)
        return hmac.compare_digest(dk, expect)
    except Exception:
        return False


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def mask(value: str) -> str:
    if len(value) <= 8:
        return "****"
    return value[:4] + "…" + value[-4:]
