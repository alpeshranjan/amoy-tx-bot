import os
import uuid

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _master(key_hex: str) -> bytes:
    raw = bytes.fromhex(key_hex.removeprefix("0x"))
    if len(raw) != 32:
        raise ValueError("KEY_ENCRYPTION_KEY must be 32 bytes of hex.")
    return raw


def encrypt_key(key_hex: str, user_id: uuid.UUID, private_key: bytes) -> tuple[bytes, bytes]:
    nonce = os.urandom(12)
    ciphertext = AESGCM(_master(key_hex)).encrypt(nonce, private_key, user_id.bytes)
    return ciphertext, nonce


def decrypt_key(key_hex: str, user_id: uuid.UUID, ciphertext: bytes, nonce: bytes) -> bytes:
    return AESGCM(_master(key_hex)).decrypt(nonce, ciphertext, user_id.bytes)
