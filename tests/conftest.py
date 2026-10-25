"""Shared pytest fixtures for the XGIMI Home Assistant integration test suite."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

# Ensure repository root is on sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


@pytest.fixture(autouse=True)
def auto_enable_local_sockets(socket_enabled: None) -> None:
    """Allow local loopback sockets for in-process ADB protocol tests."""


@pytest.fixture(scope="session")
def shared_rsa_key_pem() -> bytes:
    """Generate a single RSA-2048 private key in PEM format for fast test execution."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


@pytest.fixture
def adb_key_file(tmp_path: Path, shared_rsa_key_pem: bytes) -> Path:
    """Provide a temporary pre-populated RSA-2048 key file."""
    key_file = tmp_path / "xgimi_adb_key"
    key_file.write_bytes(shared_rsa_key_pem)
    return key_file
