"""Tests for the async RSA-2048 ADB wire protocol client (adb_client.py)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import struct
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, utils

from custom_components.xgimi.adb_client import (
    A_AUTH,
    A_CLSE,
    A_CNXN,
    A_OKAY,
    A_OPEN,
    A_WRTE,
    AUTH_RSAPUBLICKEY,
    AUTH_SIGNATURE,
    AUTH_TOKEN,
    HEADER_LEN,
    MAX_PAYLOAD,
    RSANUMBYTES,
    RSANUMWORDS,
    VERSION,
    AdbAuthError,
    AdbConnectionError,
    AdbError,
    XgimiAdbClient,
    encode_adb_public_key,
    pack_adb_message,
    sign_adb_token,
)


class FakeAdbdServer:
    """In-process async mock ADB daemon speaking the AOSP ADB TCP wire protocol."""

    def __init__(
        self,
        *,
        accept_signature: bool = True,
        accept_pubkey: bool = True,
        immediate_cnxn: bool = False,
        drop_first_open: bool = False,
        hang_handshake: bool = False,
    ) -> None:
        self.accept_signature = accept_signature
        self.accept_pubkey = accept_pubkey
        self.immediate_cnxn = immediate_cnxn
        self.drop_first_open = drop_first_open
        self.hang_handshake = hang_handshake

        self.received_auth_types: list[int] = []
        self.executed_commands: list[str] = []
        self.shell_responses: dict[str, str] = {}
        self.connections_made: int = 0
        self._server: asyncio.AbstractServer | None = None
        self.port: int = 0
        self._dropped_once = False

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_client, "127.0.0.1", 0)
        sock = self._server.sockets[0]
        self.port = sock.getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    @staticmethod
    async def _read_msg(reader: asyncio.StreamReader) -> tuple[int, int, int, bytes]:
        hdr = await reader.readexactly(HEADER_LEN)
        cmd, arg0, arg1, data_len, _chk, _magic = struct.unpack("<6I", hdr)
        payload = await reader.readexactly(data_len) if data_len > 0 else b""
        return cmd, arg0, arg1, payload

    @staticmethod
    async def _write_msg(writer: asyncio.StreamWriter, cmd: int, arg0: int, arg1: int, payload: bytes = b"") -> None:
        writer.write(pack_adb_message(cmd, arg0, arg1, payload))
        await writer.drain()

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections_made += 1
        try:
            cmd, _arg0, _arg1, _payload = await self._read_msg(reader)
            if cmd != A_CNXN:
                return

            if self.hang_handshake:
                await asyncio.sleep(10.0)
                return

            if self.immediate_cnxn:
                await self._write_msg(writer, A_CNXN, VERSION, MAX_PAYLOAD, b"device::xgimi\x00")
            else:
                token = b"\x42" * 20
                await self._write_msg(writer, A_AUTH, AUTH_TOKEN, 0, token)

                cmd, arg0, _arg1, _payload = await self._read_msg(reader)
                if cmd != A_AUTH:
                    return
                self.received_auth_types.append(arg0)

                if arg0 == AUTH_SIGNATURE and self.accept_signature:
                    await self._write_msg(writer, A_CNXN, VERSION, MAX_PAYLOAD, b"device::xgimi\x00")
                else:
                    # Reject signature by sending a new AUTH_TOKEN challenge
                    await self._write_msg(writer, A_AUTH, AUTH_TOKEN, 0, b"\x99" * 20)
                    try:
                        cmd2, arg0_2, _arg1_2, _payload2 = await self._read_msg(reader)
                    except asyncio.IncompleteReadError:
                        return
                    if cmd2 == A_AUTH:
                        self.received_auth_types.append(arg0_2)
                        if arg0_2 == AUTH_RSAPUBLICKEY and self.accept_pubkey:
                            await self._write_msg(writer, A_CNXN, VERSION, MAX_PAYLOAD, b"device::xgimi\x00")
                        else:
                            return

            # Connected! Process A_OPEN streams
            while True:
                try:
                    cmd, arg0, _arg1, payload = await self._read_msg(reader)
                except asyncio.IncompleteReadError:
                    break

                if cmd == A_OPEN:
                    if self.drop_first_open and not self._dropped_once:
                        self._dropped_once = True
                        writer.close()
                        await writer.wait_closed()
                        return

                    local_id = arg0
                    remote_id = 100 + local_id
                    dest = payload.rstrip(b"\x00").decode("utf-8", errors="replace")
                    shell_cmd = dest.removeprefix("shell:")
                    self.executed_commands.append(shell_cmd)

                    if shell_cmd == "reject_open":
                        await self._write_msg(writer, A_CLSE, 0, local_id)
                        continue

                    if shell_cmd == "hang_stream":
                        await self._write_msg(writer, A_OKAY, remote_id, local_id)
                        with contextlib.suppress(asyncio.IncompleteReadError):
                            await self._read_msg(reader)
                        return

                    if shell_cmd == "drop_mid_stream":
                        await self._write_msg(writer, A_OKAY, remote_id, local_id)
                        writer.close()
                        await writer.wait_closed()
                        return

                    if shell_cmd == "slow_stream":
                        await self._write_msg(writer, A_OKAY, remote_id, local_id)
                        for _ in range(15):
                            await asyncio.sleep(0.02)
                            await self._write_msg(writer, A_WRTE, remote_id, local_id, b"chunk\n")
                            ok_cmd, _, _, _ = await self._read_msg(reader)
                            assert ok_cmd == A_OKAY
                        await self._write_msg(writer, A_CLSE, remote_id, local_id)
                        await self._read_msg(reader)
                        return

                    await self._write_msg(writer, A_OKAY, remote_id, local_id)
                    if shell_cmd == "multi_chunk":
                        for chunk in (b"chunk1-", b"", b"chunk2\n"):
                            await self._write_msg(writer, A_WRTE, remote_id, local_id, chunk)
                            ok_cmd, _, _, _ = await self._read_msg(reader)
                            assert ok_cmd == A_OKAY
                    else:
                        response_text = self.shell_responses.get(shell_cmd, f"OK:{shell_cmd}\n")
                        if response_text:
                            await self._write_msg(
                                writer,
                                A_WRTE,
                                remote_id,
                                local_id,
                                response_text.encode(),
                            )
                            # Expect client A_OKAY
                            ok_cmd, _, _, _ = await self._read_msg(reader)
                            assert ok_cmd == A_OKAY

                    await self._write_msg(writer, A_CLSE, remote_id, local_id)
                    # Read client A_CLSE ack
                    await self._read_msg(reader)
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()


def test_pack_adb_message_and_public_key_encoding(adb_key_file: Path) -> None:
    """Verify ADB packet header framing and libcrypto_utils RSA-2048 struct encoding."""
    client = XgimiAdbClient("127.0.0.1", key_path=adb_key_file)
    private_key, public_key = client._load_or_create_key_sync()

    # Verify packet framing
    pkt = pack_adb_message(A_CNXN, VERSION, MAX_PAYLOAD, b"abc")
    cmd, arg0, arg1, length, checksum, magic = struct.unpack("<6I", pkt[:HEADER_LEN])
    assert cmd == A_CNXN
    assert arg0 == VERSION
    assert arg1 == MAX_PAYLOAD
    assert length == 3
    assert checksum == sum(b"abc")
    assert magic == (A_CNXN ^ 0xFFFFFFFF) & 0xFFFFFFFF
    assert pkt[HEADER_LEN:] == b"abc"

    # Verify Android RSAPublicKey struct encoding
    encoded = encode_adb_public_key(public_key, identity="test@ha")
    assert encoded.endswith(b" test@ha\x00")
    b64_part = encoded.split(b" ")[0]
    raw_struct = base64.b64decode(b64_part)
    assert len(raw_struct) == 524

    words, n0inv = struct.unpack("<2I", raw_struct[:8])
    assert words == RSANUMWORDS == 64
    n_bytes = raw_struct[8 : 8 + RSANUMBYTES]
    rr_bytes = raw_struct[8 + RSANUMBYTES : 8 + 2 * RSANUMBYTES]
    (exponent,) = struct.unpack("<I", raw_struct[-4:])

    numbers = public_key.public_numbers()
    assert int.from_bytes(n_bytes, "little") == numbers.n
    assert int.from_bytes(rr_bytes, "little") == pow(2, 4096, numbers.n)
    assert exponent == numbers.e
    assert (numbers.n * n0inv) % (1 << 32) == (1 << 32) - 1

    # Verify token signing requires exactly 20 bytes
    token20 = b"\x55" * 20
    sig20 = sign_adb_token(private_key, token20)
    assert len(sig20) == 256
    public_key.verify(sig20, token20, padding.PKCS1v15(), utils.Prehashed(hashes.SHA1()))

    with pytest.raises(ValueError, match="ADB AUTH_TOKEN must be exactly 20 bytes"):
        sign_adb_token(private_key, b"short_token")

    with pytest.raises(ValueError, match="ADB AUTH_TOKEN must be exactly 20 bytes"):
        sign_adb_token(private_key, b"x" * 250)


@pytest.mark.asyncio
async def test_key_persistence_and_corrupt_key_recovery(tmp_path: Path) -> None:
    """Verify RSA key generation, persistence across instances, and corrupt key recovery."""
    key_path = tmp_path / ".storage" / "xgimi_adb_key"
    client1 = XgimiAdbClient("127.0.0.1", key_path=key_path)
    _priv1, pub1 = await client1.async_ensure_key()
    assert key_path.is_file()

    # Second instance loads identical key
    client2 = XgimiAdbClient("127.0.0.1", key_path=key_path)
    _priv2, pub2 = await client2.async_ensure_key()
    assert pub1.public_numbers().n == pub2.public_numbers().n

    # Corrupt file -> regenerates cleanly
    key_path.write_bytes(b"not-a-valid-pem-key")
    client3 = XgimiAdbClient("127.0.0.1", key_path=key_path)
    _priv3, pub3 = await client3.async_ensure_key()
    assert pub3.public_numbers().n != pub1.public_numbers().n


@pytest.mark.asyncio
async def test_non_prompting_probe_vs_interactive_authorize(adb_key_file: Path) -> None:
    """Verify async_is_authorized does not send AUTH_RSAPUBLICKEY while async_authorize does."""
    # Server that rejects signature but accepts pubkey pairing
    server = FakeAdbdServer(accept_signature=False, accept_pubkey=True)
    await server.start()
    try:
        client = XgimiAdbClient("127.0.0.1", port=server.port, key_path=adb_key_file)

        # Non-prompting check must NOT send AUTH_RSAPUBLICKEY
        assert await client.async_probe_status() == "unauthorized"
        assert server.received_auth_types == [AUTH_SIGNATURE]

        # Interactive authorize MUST send AUTH_RSAPUBLICKEY and succeed
        server.received_auth_types.clear()
        assert await client.async_authorize(timeout=5.0) is True
        assert server.received_auth_types == [AUTH_SIGNATURE, AUTH_RSAPUBLICKEY]
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_shell_execution_connection_pooling_and_retry(adb_key_file: Path) -> None:
    """Verify shell execution, persistent socket reuse, and transparent retry on stale socket."""
    server = FakeAdbdServer(accept_signature=True)
    server.shell_responses["dumpsys power"] = "mWakefulness=Awake\n"
    await server.start()
    try:
        client = XgimiAdbClient("[127.0.0.1]", port=server.port, key_path=adb_key_file)
        assert await client.async_probe_status() == "authorized"
        initial_conns = server.connections_made

        out = await client.async_shell("dumpsys power")
        assert out == "mWakefulness=Awake"
        await client.async_send_key(140)
        await client.async_send_key(24)

        # All 3 commands reused a single persistent ADB connection
        assert server.connections_made == initial_conns + 1
        assert server.executed_commands == [
            "dumpsys power",
            "input keyevent 140",
            "input keyevent 24",
        ]

        # Simulate stale pooled connection dropping on next A_OPEN -> should reconnect & succeed
        server.drop_first_open = True
        out2 = await client.async_shell("dumpsys power")
        assert out2 == "mWakefulness=Awake"
        assert server.connections_made == initial_conns + 2

        # Invalid keycodes rejected before network I/O
        with pytest.raises(ValueError, match="Invalid Android keycode"):
            await client.async_send_key(-1)
        with pytest.raises(ValueError, match="Invalid Android keycode"):
            await client.async_send_key(True)

        await client.async_close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_multi_chunk_wrte_rejected_open_and_cancellation(adb_key_file: Path) -> None:
    """Verify multi-chunk/empty A_WRTE assembly, rejected A_OPEN error, mid-stream EOF, and task cancellation."""
    server = FakeAdbdServer(accept_signature=True)
    await server.start()
    try:
        client = XgimiAdbClient("127.0.0.1", port=server.port, key_path=adb_key_file)

        # 1. Multi-chunk A_WRTE with an empty A_WRTE packet in the middle
        out = await client.async_shell("multi_chunk")
        assert out == "chunk1-chunk2"

        # 2. Rejected A_OPEN (A_CLSE(0, local_id) without A_OKAY) raises AdbError
        with pytest.raises(AdbError, match="ADB stream open rejected"):
            await client.async_shell("reject_open")

        # 3. Mid-stream EOF wraps IncompleteReadError into AdbConnectionError
        client2 = XgimiAdbClient("127.0.0.1", port=server.port, key_path=adb_key_file)
        with pytest.raises(AdbConnectionError):
            await client2.async_shell("drop_mid_stream")

        # 4. Cancelling an in-flight shell command closes the pooled socket so the next call succeeds cleanly
        task = asyncio.create_task(client.async_shell("hang_stream", timeout=5.0))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client._writer is None

        out_after_cancel = await client.async_shell("multi_chunk")
        assert out_after_cancel == "chunk1-chunk2"
        await client.async_close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_adb_error_paths(adb_key_file: Path) -> None:
    """Verify unreachable host, unauthorized shell error, and corrupt header handling."""
    # Unreachable port
    client = XgimiAdbClient("127.0.0.1", port=1, key_path=adb_key_file)
    assert await client.async_probe_status(timeout=0.3) == "unreachable"
    with pytest.raises(AdbConnectionError):
        await client.async_shell("echo hi", timeout=0.3)

    # Unauthorized shell attempt raises AdbAuthError
    unauth_server = FakeAdbdServer(accept_signature=False, accept_pubkey=False)
    await unauth_server.start()
    try:
        client2 = XgimiAdbClient("127.0.0.1", port=unauth_server.port, key_path=adb_key_file)
        with pytest.raises(AdbAuthError):
            await client2.async_shell("echo hi", timeout=1.0)
    finally:
        await unauth_server.stop()

    # Corrupt header magic raises AdbError
    reader = asyncio.StreamReader()
    reader.feed_data(struct.pack("<6I", A_CNXN, VERSION, MAX_PAYLOAD, 0, 0, 0xDEADBEEF))
    with pytest.raises(AdbError, match="Corrupt ADB header magic"):
        await XgimiAdbClient._read_message(reader, timeout=1.0)


@pytest.mark.asyncio
async def test_shell_total_timeout_bounds_execution(adb_key_file: Path) -> None:
    """Verify shell output arriving within packet timeout is aborted when exceeding total command deadline."""
    server = FakeAdbdServer(accept_signature=True)
    await server.start()
    try:
        client = XgimiAdbClient("127.0.0.1", port=server.port, key_path=adb_key_file)
        start = asyncio.get_running_loop().time()
        with pytest.raises(TimeoutError):
            await client.async_shell("slow_stream", timeout=0.08)
        elapsed = asyncio.get_running_loop().time() - start
        assert elapsed < 0.25
        assert client._writer is None
        await client.async_close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_handshake_cancellation_cleans_up_socket(adb_key_file: Path) -> None:
    """Verify cancelling during handshake closes the newly opened socket."""
    server = FakeAdbdServer(hang_handshake=True)
    await server.start()
    try:
        client = XgimiAdbClient("127.0.0.1", port=server.port, key_path=adb_key_file)

        closed_writers: list[asyncio.StreamWriter] = []
        orig_close_writer = XgimiAdbClient._close_writer

        async def _tracking_close_writer(writer: asyncio.StreamWriter) -> None:
            closed_writers.append(writer)
            await orig_close_writer(writer)

        with patch.object(XgimiAdbClient, "_close_writer", side_effect=_tracking_close_writer):
            task = asyncio.create_task(client.async_shell("echo hi", timeout=5.0))
            for _ in range(50):
                if server.connections_made > 0:
                    break
                await asyncio.sleep(0.01)
            assert server.connections_made > 0

            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            assert len(closed_writers) > 0
            assert client._writer is None
    finally:
        await server.stop()
