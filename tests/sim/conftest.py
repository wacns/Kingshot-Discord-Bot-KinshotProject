"""Fixtures for the SimCord UI suite. Run with: pytest tests/sim --sim"""
import logging
import socket

import pytest
import pytest_asyncio

import sim_harness

LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Asyncio on Windows and the screenshot preview need loopback; everything else is blocked.

    Name lookups are blocked too: Windows' proactor loop connects without going
    through socket.connect, but every platform resolves the host first, so this
    catches outside calls on Windows as well as Linux."""
    attempts = []
    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def guarded_connect(sock, address):
        if isinstance(address, tuple) and address[0] in LOOPBACK:
            return real_connect(sock, address)
        attempts.append(address)
        raise OSError(f"network blocked in the SimCord suite: {address}")

    def guarded_getaddrinfo(host, *args, **kwargs):
        name = host.decode() if isinstance(host, bytes) else host
        if name is None or name in LOOPBACK:
            return real_getaddrinfo(host, *args, **kwargs)
        attempts.append(name)
        raise socket.gaierror(f"name lookup blocked in the SimCord suite: {name}")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    yield
    assert not attempts, f"code tried to reach the network: {attempts}"


class _ErrorLog(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)
        self.messages = []

    def emit(self, record):
        self.messages.append(f"{record.name}: {record.getMessage()}")


@pytest.fixture
def logged_errors():
    handler = _ErrorLog()
    root = logging.getLogger()
    root.addHandler(handler)
    yield handler.messages
    root.removeHandler(handler)


def _sim_fixture(scenario, **options):
    @pytest_asyncio.fixture
    async def fixture(tmp_path, monkeypatch, logged_errors):
        monkeypatch.chdir(tmp_path)
        async with sim_harness.running(scenario, **options) as sim:
            sim.logged_errors = logged_errors
            yield sim
    return fixture


sim = _sim_fixture("populated")
empty_sim = _sim_fixture("empty")
sim_without_admin = _sim_fixture("populated", bot_admin=False)
