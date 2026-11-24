"""Encryption at rest for sensitive values, using Windows DPAPI (current-user scope).

    blob = crypto.encrypt(b"...")            # only this Windows user on this PC can decrypt it
    crypto.secret_set("github", "ghp_...")   # stored under HOME/secrets/github.bin
    crypto.secret_get("github")              # -> str, or None if not stored

There is no key file: Windows derives the key from the user's logon credentials. On other platforms
encrypt/decrypt raise CryptoError instead of falling back to plaintext; keep credentials in the
environment (HOME/.env, read through config.secret) there.
"""
import ctypes
import re
import sys
import time

from . import config

_ENTROPY = b"sparx.secrets.v1"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_UI_FORBIDDEN = 0x1


class CryptoError(Exception):
    pass


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _Blob:
    return _Blob(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data) or 1), ctypes.POINTER(ctypes.c_char)))


def _dpapi(func: str, data: bytes) -> bytes:
    if sys.platform != "win32":
        raise CryptoError("Encryption at rest needs Windows DPAPI, which this platform does not have. "
                          "Nothing was stored. Put the credential in the environment or ~/.sparx/.env instead.")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    fn = getattr(crypt32, func)
    fn.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_uint32, ctypes.POINTER(_Blob)]
    fn.restype = ctypes.c_int
    src, entropy, out = _blob(data), _blob(_ENTROPY), _Blob()
    if not fn(ctypes.byref(src), None, ctypes.byref(entropy), None, None, _UI_FORBIDDEN, ctypes.byref(out)):
        raise CryptoError(f"{func} failed: {ctypes.FormatError(ctypes.get_last_error()).strip()}")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def encrypt(data: bytes) -> bytes:
    return _dpapi("CryptProtectData", bytes(data))


def decrypt(blob: bytes) -> bytes:
    """Raises CryptoError if the blob was made by another user or machine, or is corrupt."""
    return _dpapi("CryptUnprotectData", bytes(blob))


def _file(name: str):
    if not _NAME.fullmatch(name or ""):
        raise CryptoError("secret names are 1-64 characters: letters, digits, '_', '-' or '.'")
    return config.path("secrets", f"{name}.bin")


def secret_set(name: str, value: str):
    if not value:
        raise CryptoError("refusing to store an empty secret")
    _file(name).write_bytes(encrypt(value.encode("utf-8")))


def secret_get(name: str) -> str | None:
    f = _file(name)
    return decrypt(f.read_bytes()).decode("utf-8") if f.is_file() else None


def secret_names() -> list[str]:
    return sorted(f.stem for f in config.path("secrets", "x").parent.glob("*.bin"))


def secret_delete(name: str) -> bool:
    """Move the (still encrypted) secret to HOME/trash."""
    f = _file(name)
    if not f.is_file():
        return False
    f.replace(config.path("trash", f"secret-{name}-{time.strftime('%Y%m%d-%H%M%S')}.bin"))
    return True
