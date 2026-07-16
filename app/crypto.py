from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class PayloadCipher:
    """Encrypt customer inputs and delivered credentials at rest."""

    def __init__(self, key: str) -> None:
        if not key:
            raise ValueError("DATA_ENCRYPTION_KEY is missing")
        self._fernet = Fernet(key.encode())

    @classmethod
    def generate_key(cls) -> str:
        return Fernet.generate_key().decode()

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("Encrypted data could not be decrypted") from exc
