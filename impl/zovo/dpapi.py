"""Windows DPAPI helpers - encryption at rest without third-party packages.

Card PANs and customer sessions must not sit in SQLite as plaintext. DPAPI
binds the ciphertext to the current Windows user/machine, so a copied .db file
is useless on another box. On non-Windows hosts this module refuses to run
rather than silently storing plaintext.
"""

from __future__ import annotations

import base64
import ctypes
import os
import sys
from ctypes import wintypes

IS_WINDOWS = sys.platform == "win32"


class DpapiError(RuntimeError):
    pass


if IS_WINDOWS:
    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32

    def _blob(data) -> DATA_BLOB:
        if isinstance(data, str):
            data = data.encode("utf-8")
        buf = ctypes.create_string_buffer(data, len(data))
        return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    def _blob_bytes(blob: DATA_BLOB) -> bytes:
        return ctypes.string_at(blob.pbData, blob.cbData)

    def _free(blob: DATA_BLOB) -> None:
        _kernel32.LocalFree(blob.pbData)

    def protect(data: bytes, entropy: bytes = b"zovo") -> str:
        if isinstance(data, str):
            data = data.encode("utf-8")
        blob_in = _blob(data)
        blob_entropy = _blob(entropy)
        blob_out = DATA_BLOB()
        ok = _crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, ctypes.byref(blob_entropy),
            None, None, 0, ctypes.byref(blob_out),
        )
        if not ok:
            raise DpapiError("CryptProtectData failed: %d" % ctypes.GetLastError())
        try:
            return "dpapi:" + base64.b64encode(_blob_bytes(blob_out)).decode("ascii")
        finally:
            _free(blob_out)

    def unprotect(token: str, entropy: bytes = b"zovo") -> str:
        if not token.startswith("dpapi:"):
            raise DpapiError("not a dpapi token")
        raw = base64.b64decode(token[len("dpapi:"):])
        blob_in = _blob(raw)
        blob_entropy = _blob(entropy)
        blob_out = DATA_BLOB()
        ok = _crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, ctypes.byref(blob_entropy),
            None, None, 0, ctypes.byref(blob_out),
        )
        if not ok:
            raise DpapiError("CryptUnprotectData failed: %d" % ctypes.GetLastError())
        try:
            return _blob_bytes(blob_out).decode("utf-8")
        finally:
            _free(blob_out)

else:  # pragma: no cover - the service is Windows-first
    def protect(data: bytes, entropy: bytes = b"zovo") -> str:
        raise DpapiError("DPAPI is Windows-only; refusing to store secrets in plaintext")

    def unprotect(token: str, entropy: bytes = b"zovo") -> str:
        raise DpapiError("DPAPI is Windows-only")


def mask_pan(pan: str) -> str:
    digits = "".join(ch for ch in pan if ch.isdigit())
    return "*" * max(0, len(digits) - 4) + digits[-4:]


def bin_of(pan: str) -> str:
    digits = "".join(ch for ch in pan if ch.isdigit())
    return digits[:6]
