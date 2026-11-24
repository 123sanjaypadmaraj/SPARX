"""The suite's promise that no test reaches beyond this machine is enforced by the no_network fixture in conftest.py."""
import socket

import pytest


def test_a_connection_beyond_this_machine_fails_at_once_naming_the_address():
    s = socket.socket()
    try:
        with pytest.raises(OSError, match=r"tests must not use the network: tried 203\.0\.113\.1:80"):
            s.connect(("203.0.113.1", 80))          # TEST-NET-3: would hang until a timeout without the guard
        with pytest.raises(OSError, match="tests must not use the network"):
            s.connect_ex(("198.51.100.7", 443))
    finally:
        s.close()


def test_loopback_connections_still_work():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    client = socket.socket()
    try:
        client.connect(server.getsockname())
        conn, _ = server.accept()
        conn.close()
    finally:
        client.close()
        server.close()


@pytest.mark.network
def test_a_test_marked_network_is_not_guarded():
    assert "_guarded" not in getattr(socket.socket.connect, "__qualname__", "")
