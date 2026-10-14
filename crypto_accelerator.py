"""
crypto_accelerator.py - Native OpenSSL Hardware AES-IGE Acceleration for Telethon on Windows.

Provides bit-exact hardware-accelerated AES-IGE encryption and decryption using
Python's built-in libcrypto-3.dll (OpenSSL with AES-NI instructions).

Falls back gracefully to Telethon's pure-Python pyaes if OpenSSL is unavailable.
"""

import os
import sys
import ctypes
import logging
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_IS_ACCELERATED: bool = False
_ACCELERATOR_ENGINE: str = "pyaes (pure-Python fallback)"
_LIBCRYPTO_PATH: Optional[str] = None

# Known AES-128-IGE Test Vector from OpenSSL test/igetest.c (Vector 0)
_VEC_KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
_VEC_IV  = bytes.fromhex("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f")
_VEC_IN  = bytes.fromhex("0000000000000000000000000000000000000000000000000000000000000000")
_VEC_OUT = bytes.fromhex("1a8519a6557be652e9da8e43da4ef4453cf456b4ca488aa383c79c98b34797cb")

def _resolve_libcrypto_path() -> Optional[str]:
    """
    Safely resolves libcrypto-3.dll strictly within known Python directories
    to prevent DLL search-order hijacking.
    """
    search_dirs = [
        Path(sys.base_prefix) / "DLLs",
        Path(sys.prefix) / "DLLs",
        Path(os.path.dirname(sys.executable)) / "DLLs",
    ]
    for d in search_dirs:
        candidate = d / "libcrypto-3.dll"
        if candidate.exists():
            return str(candidate.resolve())
    return None

def init_crypto_acceleration() -> Tuple[bool, str]:
    """
    Initializes and validates native OpenSSL hardware AES acceleration.
    Hooks into telethon.crypto.libssl if verified.
    
    Returns:
        (is_accelerated: bool, status_message: str)
    """
    global _IS_ACCELERATED, _ACCELERATOR_ENGINE, _LIBCRYPTO_PATH

    if _IS_ACCELERATED:
        return True, _ACCELERATOR_ENGINE

    dll_path = _resolve_libcrypto_path()
    if not dll_path:
        _IS_ACCELERATED = False
        _ACCELERATOR_ENGINE = "pyaes (pure-Python fallback: libcrypto-3.dll not found)"
        logger.warning(f"[Crypto] {_ACCELERATOR_ENGINE}")
        return False, _ACCELERATOR_ENGINE

    try:
        # Load libcrypto DLL safely via absolute path
        lib = ctypes.CDLL(dll_path)

        # Verify required symbols
        for sym in ("AES_set_encrypt_key", "AES_set_decrypt_key", "AES_ige_encrypt"):
            if not hasattr(lib, sym):
                raise AttributeError(f"libcrypto-3.dll is missing symbol: {sym}")

        AES_ENCRYPT = ctypes.c_int(1)
        AES_DECRYPT = ctypes.c_int(0)
        AES_MAXNR = 14

        class AES_KEY(ctypes.Structure):
            _fields_ = [
                ('rd_key', ctypes.c_uint32 * (4 * (AES_MAXNR + 1))),
                ('rounds', ctypes.c_uint),
            ]

        def decrypt_ige(cipher_text: bytes, key: bytes, iv: bytes) -> bytes:
            blocks_count = len(cipher_text) // 16
            if blocks_count == 0:
                return b""
            aligned_len = blocks_count * 16
            if len(cipher_text) != aligned_len:
                cipher_text = cipher_text[:aligned_len]

            aes_key = AES_KEY()
            key_len = ctypes.c_int(8 * len(key))
            key_c = (ctypes.c_ubyte * len(key))(*key)
            iv_c = (ctypes.c_ubyte * len(iv))(*iv)
            in_len = ctypes.c_size_t(aligned_len)
            in_ptr = (ctypes.c_ubyte * aligned_len)(*cipher_text)
            out_ptr = (ctypes.c_ubyte * aligned_len)()

            lib.AES_set_decrypt_key(key_c, key_len, ctypes.byref(aes_key))
            lib.AES_ige_encrypt(
                ctypes.byref(in_ptr),
                ctypes.byref(out_ptr),
                in_len,
                ctypes.byref(aes_key),
                ctypes.byref(iv_c),
                AES_DECRYPT
            )
            return bytes(out_ptr)

        def encrypt_ige(plain_text: bytes, key: bytes, iv: bytes) -> bytes:
            padding = len(plain_text) % 16
            if padding:
                plain_text = plain_text + os.urandom(16 - padding)

            aes_key = AES_KEY()
            key_len = ctypes.c_int(8 * len(key))
            key_c = (ctypes.c_ubyte * len(key))(*key)
            iv_c = (ctypes.c_ubyte * len(iv))(*iv)
            in_len = ctypes.c_size_t(len(plain_text))
            in_ptr = (ctypes.c_ubyte * len(plain_text))(*plain_text)
            out_ptr = (ctypes.c_ubyte * len(plain_text))()

            lib.AES_set_encrypt_key(key_c, key_len, ctypes.byref(aes_key))
            lib.AES_ige_encrypt(
                ctypes.byref(in_ptr),
                ctypes.byref(out_ptr),
                in_len,
                ctypes.byref(aes_key),
                ctypes.byref(iv_c),
                AES_ENCRYPT
            )
            return bytes(out_ptr)

        # Self-test against official OpenSSL AES-128-IGE known test vector
        test_enc = encrypt_ige(_VEC_IN, _VEC_KEY, _VEC_IV)
        if test_enc != _VEC_OUT:
            raise RuntimeError(f"OpenSSL AES-IGE self-test encryption mismatch: {test_enc.hex()}")
        test_dec = decrypt_ige(_VEC_OUT, _VEC_KEY, _VEC_IV)
        if test_dec != _VEC_IN:
            raise RuntimeError(f"OpenSSL AES-IGE self-test decryption mismatch: {test_dec.hex()}")

        # Wire into Telethon's libssl and aes modules
        from telethon.crypto import libssl, aes
        libssl._libssl = lib
        libssl.decrypt_ige = decrypt_ige
        libssl.encrypt_ige = encrypt_ige

        # Also patch aes.AES directly to ensure Telethon routes all calls to native OpenSSL
        aes.AES.decrypt_ige = staticmethod(decrypt_ige)
        aes.AES.encrypt_ige = staticmethod(encrypt_ige)

        _IS_ACCELERATED = True
        _LIBCRYPTO_PATH = dll_path
        _ACCELERATOR_ENGINE = f"OpenSSL Hardware AES-NI ({Path(dll_path).name})"
        logger.info(f"[Crypto] {_ACCELERATOR_ENGINE} verified and active.")
        return True, _ACCELERATOR_ENGINE

    except Exception as e:
        _IS_ACCELERATED = False
        _ACCELERATOR_ENGINE = f"pyaes (pure-Python fallback: {e})"
        logger.warning(f"[Crypto] Initialization failed: {e}. Falling back to pyaes.")
        return False, _ACCELERATOR_ENGINE

def is_accelerated() -> bool:
    """Returns True if native OpenSSL AES acceleration is active."""
    return _IS_ACCELERATED

def get_engine_status() -> str:
    """Returns a descriptive string of the current crypto engine."""
    return _ACCELERATOR_ENGINE
