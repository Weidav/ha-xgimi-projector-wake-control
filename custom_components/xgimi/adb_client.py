"""Async RSA-2048 ADB wire protocol client for XGIMI Projectors (port 5555).

Ported from LocalAdbClient.java in Weidav/xgimi-bridge using Home Assistant's
built-in cryptography package (zero external ADB pip dependency conflicts).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import struct
from pathlib import Path
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa, utils

from .const import ADB_KEY_STORAGE_FILE, DEFAULT_ADB_PORT

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# ADB Wire Protocol Constants (AOSP system/core/adb/protocol.txt)
A_CNXN = 0x4E584E43
A_AUTH = 0x48545541
A_OPEN = 0x4E45504F
A_OKAY = 0x59414B4F
A_CLSE = 0x45534C43
A_WRTE = 0x45545257

AUTH_TOKEN = 1
AUTH_SIGNATURE = 2
AUTH_RSAPUBLICKEY = 3

VERSION = 0x01000001
MAX_PAYLOAD = 256 * 1024
HEADER_LEN = 24

# RSA-2048 Android libcrypto_utils parameters
RSANUMBYTES = 256
RSANUMWORDS = RSANUMBYTES // 4  # 64 uint32 words


class AdbError(Exception):
    """Base exception for ADB protocol errors."""


class AdbAuthError(AdbError):
    """Raised when ADB authorization fails or has not been granted on the projector."""


class AdbConnectionError(AdbError):
    """Raised when the projector's ADB daemon is unreachable."""


def pack_adb_message(command: int, arg0: int, arg1: int, payload: bytes = b"") -> bytes:
    """Pack an ADB wire protocol message (24-byte little-endian header + payload)."""
    checksum = sum(payload) & 0xFFFFFFFF
    magic = (command ^ 0xFFFFFFFF) & 0xFFFFFFFF
    header = struct.pack("<6I", command, arg0, arg1, len(payload), checksum, magic)
    return header + payload


def encode_adb_public_key(public_key: rsa.RSAPublicKey, identity: str = "homeassistant@xgimi") -> bytes:
    """Encode an RSA-2048 public key into Android's RSAPublicKey binary struct + base64.

    Matches LocalAdbClient.encodeAdbPublicKey() and AOSP libcrypto_utils.
    Struct layout (524 bytes):
      - len: uint32 LE = 64 (RSANUMWORDS)
      - n0inv: uint32 LE = -1 / n[0] mod 2^32
      - n: 256 bytes little-endian modulus
      - rr: 256 bytes little-endian R^2 mod n (where R = 2^2048)
      - exponent: uint32 LE (e.g. 65537)
    """
    numbers = public_key.public_numbers()
    n = numbers.n
    e = numbers.e

    r32 = 1 << 32
    n0inv = (-pow(n % r32, -1, r32)) % r32
    rr = pow(2, 4096, n)

    n_le = n.to_bytes(RSANUMBYTES, byteorder="little")
    rr_le = rr.to_bytes(RSANUMBYTES, byteorder="little")

    struct_bytes = struct.pack("<2I", RSANUMWORDS, n0inv) + n_le + rr_le + struct.pack("<I", e)
    b64 = base64.b64encode(struct_bytes).decode("ascii")
    return f"{b64} {identity}\x00".encode("ascii")


def sign_adb_token(private_key: rsa.RSAPrivateKey, token: bytes) -> bytes:
    """Sign a 20-byte ADB AUTH_TOKEN using RSA-2048 PKCS#1 v1.5 SHA-1 DigestInfo."""
    if len(token) != 20:
        raise ValueError(f"ADB AUTH_TOKEN must be exactly 20 bytes, got {len(token)}")
    return private_key.sign(
        token,
        padding.PKCS1v15(),
        utils.Prehashed(hashes.SHA1()),
    )


# Maximum total shell output buffered across multiple A_WRTE chunks (1 MiB)
_MAX_SHELL_OUTPUT = 1024 * 1024


class XgimiAdbClient:
    """Async ADB client speaking the native ADB TCP protocol on port 5555."""

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_ADB_PORT,
        hass: HomeAssistant | None = None,
        key_path: str | Path | None = None,
    ) -> None:
        """Initialize the ADB client."""
        self.host = host.strip().strip("[]")
        self.port = port
        self._hass = hass
        if key_path is not None:
            self._key_path = Path(key_path)
        elif hass is not None:
            self._key_path = Path(hass.config.path(".storage", ADB_KEY_STORAGE_FILE))
        else:
            self._key_path = Path(".storage") / ADB_KEY_STORAGE_FILE

        self._private_key: rsa.RSAPrivateKey | None = None
        self._public_key: rsa.RSAPublicKey | None = None
        self._key_lock = asyncio.Lock()
        self._cmd_lock = asyncio.Lock()

        # Persistent authenticated transport for low-latency shell/key commands
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._next_local_id: int = 1

    async def async_ensure_key(self) -> tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey]:
        """Load or generate the persistent RSA-2048 keypair without blocking the event loop."""
        if self._private_key is not None and self._public_key is not None:
            return self._private_key, self._public_key

        async with self._key_lock:
            if self._private_key is not None and self._public_key is not None:
                return self._private_key, self._public_key

            if self._hass is not None:
                priv, pub = await self._hass.async_add_executor_job(self._load_or_create_key_sync)
            else:
                priv, pub = await asyncio.to_thread(self._load_or_create_key_sync)

            self._private_key = priv
            self._public_key = pub
            return priv, pub

    def _load_or_create_key_sync(self) -> tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey]:
        """Synchronously load or generate the 2048-bit RSA keypair on disk."""
        key_file = self._key_path
        if key_file.is_file():
            try:
                pem_bytes = key_file.read_bytes()
                loaded = serialization.load_pem_private_key(pem_bytes, password=None)
                if isinstance(loaded, rsa.RSAPrivateKey) and loaded.key_size == 2048:
                    return loaded, loaded.public_key()
            except (ValueError, TypeError, OSError) as err:
                _LOGGER.warning("Stored ADB key at %s invalid (%s), regenerating", key_file, err)

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem_data = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        try:
            key_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_file = key_file.with_suffix(".tmp")
            tmp_file.write_bytes(pem_data)
            with contextlib.suppress(OSError):
                os.chmod(tmp_file, 0o600)
            os.replace(tmp_file, key_file)
        except OSError as err:
            _LOGGER.debug("Could not persist ADB key to %s: %s", key_file, err)

        return private_key, private_key.public_key()

    @staticmethod
    async def _read_message(reader: asyncio.StreamReader, timeout: float) -> tuple[int, int, int, bytes]:
        """Read a single ADB message header and payload within timeout."""
        try:
            header = await asyncio.wait_for(reader.readexactly(HEADER_LEN), timeout=timeout)
        except asyncio.IncompleteReadError as err:
            raise AdbConnectionError("ADB connection closed by peer while reading header") from err
        cmd, arg0, arg1, data_len, _data_check, magic = struct.unpack("<6I", header)
        expected_magic = (cmd ^ 0xFFFFFFFF) & 0xFFFFFFFF
        if magic != expected_magic:
            raise AdbError(f"Corrupt ADB header magic: 0x{magic:08x} != 0x{expected_magic:08x}")
        if data_len > MAX_PAYLOAD:
            raise AdbError(f"ADB payload length {data_len} exceeds MAX_PAYLOAD {MAX_PAYLOAD}")
        if data_len > 0:
            try:
                payload = await asyncio.wait_for(reader.readexactly(data_len), timeout=timeout)
            except asyncio.IncompleteReadError as err:
                raise AdbConnectionError("ADB connection closed by peer while reading payload") from err
        else:
            payload = b""
        return cmd, arg0, arg1, payload

    @staticmethod
    async def _write_message(
        writer: asyncio.StreamWriter,
        command: int,
        arg0: int,
        arg1: int,
        payload: bytes = b"",
        timeout: float = 5.0,
    ) -> None:
        """Write and drain an ADB packet."""
        writer.write(pack_adb_message(command, arg0, arg1, payload))
        await asyncio.wait_for(writer.drain(), timeout=timeout)

    async def _handshake(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        prompt_on_reject: bool,
        auth_timeout: float,
    ) -> bool:
        """Execute the ADB A_CNXN / A_AUTH handshake.

        When prompt_on_reject=False, only sends AUTH_SIGNATURE and returns False
        if the projector rejects the signature (never triggers an on-screen popup).
        When prompt_on_reject=True, sends AUTH_RSAPUBLICKEY if signature is rejected
        and waits up to auth_timeout seconds for the user to click Allow on the projector.
        """
        private_key, public_key = await self.async_ensure_key()

        await self._write_message(
            writer,
            A_CNXN,
            VERSION,
            MAX_PAYLOAD,
            b"host::homeassistant-xgimi\x00",
            timeout=min(auth_timeout, 5.0),
        )

        cmd, arg0, _arg1, payload = await self._read_message(reader, timeout=min(auth_timeout, 5.0))
        if cmd == A_CNXN:
            return True

        if cmd != A_AUTH or arg0 != AUTH_TOKEN:
            raise AdbError(f"Unexpected ADB handshake response: cmd=0x{cmd:08x} arg0={arg0}")

        # Step 1: Sign the token with our RSA-2048 private key
        signature = sign_adb_token(private_key, payload)
        await self._write_message(
            writer,
            A_AUTH,
            AUTH_SIGNATURE,
            0,
            signature,
            timeout=min(auth_timeout, 5.0),
        )

        cmd, arg0, _arg1, _payload = await self._read_message(reader, timeout=min(auth_timeout, 5.0))
        if cmd == A_CNXN:
            return True

        if cmd == A_AUTH and arg0 == AUTH_TOKEN:
            if not prompt_on_reject:
                return False

            # Step 2: Send our public key to trigger the on-screen Allow dialog on the projector
            pub_payload = encode_adb_public_key(public_key)
            await self._write_message(
                writer,
                A_AUTH,
                AUTH_RSAPUBLICKEY,
                0,
                pub_payload,
                timeout=min(auth_timeout, 5.0),
            )
            try:
                cmd, _arg0, _arg1, _payload = await self._read_message(reader, timeout=auth_timeout)
            except (TimeoutError, asyncio.IncompleteReadError, OSError, AdbConnectionError):
                return False
            return cmd == A_CNXN

        return False

    async def async_probe_status(self, timeout: float = 3.0) -> str:
        """Non-prompting probe returning 'authorized', 'unauthorized', or 'unreachable'."""
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=timeout,
            )
            authorized = await self._handshake(
                reader,
                writer,
                prompt_on_reject=False,
                auth_timeout=timeout,
            )
            return "authorized" if authorized else "unauthorized"
        except (TimeoutError, OSError, asyncio.IncompleteReadError, AdbError):
            return "unreachable"
        finally:
            if writer is not None:
                await self._close_writer(writer)

    async def async_authorize(self, timeout: float = 30.0) -> bool:
        """Trigger the on-screen ADB Allow prompt if needed and wait for user approval."""
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=min(timeout, 5.0),
            )
            return await self._handshake(
                reader,
                writer,
                prompt_on_reject=True,
                auth_timeout=timeout,
            )
        except (TimeoutError, OSError, asyncio.IncompleteReadError, AdbError) as err:
            _LOGGER.debug("ADB authorize to %s:%s failed: %s", self.host, self.port, err)
            return False
        finally:
            if writer is not None:
                await self._close_writer(writer)

    async def _ensure_connected_locked(self, timeout: float) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Ensure an authenticated persistent ADB connection is open (caller holds _cmd_lock)."""
        if (
            self._reader is not None
            and self._writer is not None
            and not self._writer.is_closing()
            and not self._reader.at_eof()
        ):
            return self._reader, self._writer

        await self._close_transport_locked()

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=timeout,
            )
        except (TimeoutError, OSError) as err:
            raise AdbConnectionError(f"Unable to connect to ADB at {self.host}:{self.port}: {err}") from err

        try:
            authorized = await self._handshake(
                reader,
                writer,
                prompt_on_reject=False,
                auth_timeout=timeout,
            )
        except (TimeoutError, OSError, asyncio.IncompleteReadError, AdbError) as err:
            await self._close_writer(writer)
            raise AdbConnectionError(f"ADB handshake failed with {self.host}:{self.port}: {err}") from err

        if not authorized:
            await self._close_writer(writer)
            raise AdbAuthError(f"ADB key not authorized on projector {self.host}:{self.port}")

        self._reader = reader
        self._writer = writer
        return reader, writer

    async def _run_shell_stream_locked(self, command: str, timeout: float) -> str:
        """Open a single shell:<command> stream on the authenticated connection and collect output."""
        reader, writer = await self._ensure_connected_locked(timeout=timeout)
        local_id = self._next_local_id
        self._next_local_id = (self._next_local_id % 0x7FFFFFFF) + 1

        open_payload = f"shell:{command}\x00".encode()
        await self._write_message(writer, A_OPEN, local_id, 0, open_payload, timeout=timeout)

        remote_id = 0
        output = bytearray()

        while True:
            cmd, arg0, arg1, payload = await self._read_message(reader, timeout=timeout)
            if cmd == A_OKAY:
                if arg1 == local_id:
                    remote_id = arg0
            elif cmd == A_WRTE:
                if arg1 == local_id:
                    remote_id = arg0
                    remaining = _MAX_SHELL_OUTPUT - len(output)
                    if remaining > 0 and payload:
                        output.extend(payload[:remaining])
                    await self._write_message(writer, A_OKAY, local_id, remote_id, b"", timeout=timeout)
            elif cmd == A_CLSE:
                if arg1 == local_id or arg1 == 0:
                    if remote_id == 0 and arg0 == 0:
                        raise AdbError(f"ADB stream open rejected by device for command: {command}")
                    with contextlib.suppress(OSError, TimeoutError):
                        await self._write_message(writer, A_CLSE, local_id, remote_id or arg0, b"", timeout=timeout)
                    break
            else:
                raise AdbError(f"Unexpected packet during shell stream: 0x{cmd:08x}")

        return output.decode("utf-8", errors="replace").strip()

    async def async_shell(self, command: str, timeout: float = 5.0) -> str:
        """Execute a bounded shell command over ADB, retrying once if a pooled socket went stale."""
        async with self._cmd_lock:
            had_existing_conn = self._writer is not None
            try:
                return await self._run_shell_stream_locked(command, timeout=timeout)
            except AdbAuthError:
                await self._close_transport_locked()
                raise
            except (TimeoutError, OSError, asyncio.IncompleteReadError, AdbError) as err:
                await self._close_transport_locked()
                if not had_existing_conn:
                    if isinstance(err, AdbError):
                        raise
                    raise AdbConnectionError(f"ADB shell command failed on {self.host}:{self.port}: {err}") from err
                try:
                    return await self._run_shell_stream_locked(command, timeout=timeout)
                except AdbAuthError:
                    await self._close_transport_locked()
                    raise
                except (TimeoutError, OSError, asyncio.IncompleteReadError, AdbError) as retry_err:
                    await self._close_transport_locked()
                    if isinstance(retry_err, AdbError):
                        raise
                    raise AdbConnectionError(
                        f"ADB shell command failed after reconnect to {self.host}:{self.port}: {retry_err}"
                    ) from retry_err
                except BaseException:
                    self._close_transport_nowait()
                    raise
            except BaseException:
                self._close_transport_nowait()
                raise

    async def async_send_key(self, keycode: int, timeout: float = 4.0) -> None:
        """Inject an Android keycode over the pooled ADB connection."""
        if type(keycode) is not int or not (0 <= keycode <= 1000):
            raise ValueError(f"Invalid Android keycode: {keycode}")
        await self.async_shell(f"input keyevent {keycode}", timeout=timeout)

    def close_transport_nowait(self) -> None:
        """Close the cached writer without awaiting."""
        writer = self._writer
        self._reader = None
        self._writer = None
        if writer is not None:
            with contextlib.suppress(OSError, RuntimeError):
                writer.close()

    _close_transport_nowait = close_transport_nowait

    async def _close_transport_locked(self) -> None:
        """Close the cached transport (caller holds _cmd_lock)."""
        writer = self._writer
        self._reader = None
        self._writer = None
        if writer is not None:
            await self._close_writer(writer)

    @staticmethod
    async def _close_writer(writer: asyncio.StreamWriter) -> None:
        """Safely close and await an asyncio StreamWriter."""
        with contextlib.suppress(OSError, TimeoutError, RuntimeError):
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), timeout=1.0)

    async def async_close(self) -> None:
        """Close any persistent ADB socket cleanly."""
        async with self._cmd_lock:
            await self._close_transport_locked()
