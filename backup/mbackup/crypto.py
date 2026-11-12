"""Target passwords at rest: Fernet (AES-128-CBC + HMAC-SHA256)."""
from cryptography.fernet import Fernet, InvalidToken


class SecretBox:
    def __init__(self, key):
        self._fernet = Fernet(key.encode() if isinstance(key, str) else key)

    def seal(self, plaintext):
        return self._fernet.encrypt(plaintext.encode('utf-8')).decode('ascii')

    def open(self, token):
        """Plaintext, or None when the token is empty or the key changed."""
        if not token:
            return None
        try:
            return self._fernet.decrypt(token.encode('ascii')).decode('utf-8')
        except (InvalidToken, ValueError):
            return None
