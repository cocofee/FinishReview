"""Windows user-scoped protection for persisted application secrets."""

from __future__ import annotations

import base64
import binascii
import ctypes
import os
from ctypes import wintypes


PROTECTED_SECRET_PREFIX = "dpapi:v1:"
_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class SecretProtectionError(RuntimeError):
    """Raised when a secret cannot be protected for the current Windows user."""


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _windows_libraries():
    if os.name != "nt":
        raise SecretProtectionError("DPAPI secret protection requires Windows")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return crypt32, kernel32


def _input_blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    return _DataBlob(len(data), buffer), buffer


def protect_secret(secret: str) -> str:
    """Protect a non-empty secret with the current Windows user credentials."""

    value = str(secret or "")
    if not value:
        return ""
    crypt32, kernel32 = _windows_libraries()
    input_blob, _input_buffer = _input_blob(value.encode("utf-8"))
    output_blob = _DataBlob()
    if not crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        "FinishReview secret",
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    ):
        raise SecretProtectionError(
            f"CryptProtectData failed with Windows error {ctypes.get_last_error()}"
        )
    try:
        encrypted = ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)
    return PROTECTED_SECRET_PREFIX + base64.b64encode(encrypted).decode("ascii")


def unprotect_secret(protected_value: str) -> str:
    """Decrypt a value produced by :func:`protect_secret` for this Windows user."""

    value = str(protected_value or "")
    if not value:
        return ""
    if not value.startswith(PROTECTED_SECRET_PREFIX):
        raise SecretProtectionError("unsupported protected secret format")
    try:
        encrypted = base64.b64decode(
            value.removeprefix(PROTECTED_SECRET_PREFIX),
            validate=True,
        )
    except (ValueError, binascii.Error) as error:
        raise SecretProtectionError("protected secret is not valid base64") from error
    if not encrypted:
        raise SecretProtectionError("protected secret payload is empty")
    crypt32, kernel32 = _windows_libraries()
    input_blob, _input_buffer = _input_blob(encrypted)
    output_blob = _DataBlob()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    ):
        raise SecretProtectionError(
            f"CryptUnprotectData failed with Windows error {ctypes.get_last_error()}"
        )
    try:
        decrypted = ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)
    try:
        return decrypted.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SecretProtectionError("protected secret is not valid UTF-8") from error


__all__ = [
    "PROTECTED_SECRET_PREFIX",
    "SecretProtectionError",
    "protect_secret",
    "unprotect_secret",
]
