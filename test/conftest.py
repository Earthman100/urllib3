import contextlib
import platform
import socket
import ssl
import sys
import threading
from pathlib import Path
from typing import AbstractSet, Any, Generator, NamedTuple, Optional

import pytest
import trustme
from tornado import ioloop, web

from dummyserver.handlers import TestingApp
from dummyserver.server import HAS_IPV6, run_tornado_app
from dummyserver.testcase import HTTPSDummyServerTestCase
from urllib3.util import ssl_

from .tz_stub import stub_timezone_ctx


# The Python 3.8+ default loop on Windows breaks Tornado
@pytest.fixture(scope="session", autouse=True)
def configure_windows_event_loop() -> None:
    if sys.version_info >= (3, 8) and platform.system() == "Windows":
        import asyncio

        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]


class ServerConfig(NamedTuple):
    scheme: str
    host: str
    port: int
    ca_certs: str

    @property
    def base_url(self) -> str:
        host = self.host
        if ":" in host:
            host = f"[{host}]"
        return f"{self.scheme}://{host}:{self.port}"


@contextlib.contextmanager
def run_server_in_thread(
    scheme: str, host: str, tmpdir: Path, ca: trustme.CA, server_cert: trustme.LeafCert
) -> Generator[ServerConfig, None, None]:
    ca_cert_path = str(tmpdir / "ca.pem")
    server_cert_path = str(tmpdir / "server.pem")
    server_key_path = str(tmpdir / "server.key")
    ca.cert_pem.write_to_path(ca_cert_path)
    server_cert.private_key_pem.write_to_path(server_key_path)
    server_cert.cert_chain_pems[0].write_to_path(server_cert_path)
    server_certs = {"keyfile": server_key_path, "certfile": server_cert_path}

    io_loop = ioloop.IOLoop.current()
    app = web.Application([(r".*", TestingApp)])
    server, port = run_tornado_app(app, io_loop, server_certs, scheme, host)
    server_thread = threading.Thread(target=io_loop.start)
    server_thread.start()

    yield ServerConfig("https", host, port, ca_cert_path)

    io_loop.add_callback(server.stop)
    io_loop.add_callback(io_loop.stop)
    server_thread.join()


@pytest.fixture(params=["localhost", "127.0.0.1", "::1"])
def loopback_host(request: Any) -> Generator[str, None, None]:
    host = request.param
    if host == "::1" and not HAS_IPV6:
        pytest.skip("Test requires IPv6 on loopback")
    yield host


@pytest.fixture()
def san_server(
    loopback_host: str, tmp_path_factory: pytest.TempPathFactory
) -> Generator[ServerConfig, None, None]:
    tmpdir = tmp_path_factory.mktemp("certs")
    ca = trustme.CA()

    server_cert = ca.issue_cert(loopback_host)

    with run_server_in_thread("https", loopback_host, tmpdir, ca, server_cert) as cfg:
        yield cfg


@pytest.fixture()
def no_san_server(
    loopback_host: str, tmp_path_factory: pytest.TempPathFactory
) -> Generator[ServerConfig, None, None]:
    tmpdir = tmp_path_factory.mktemp("certs")
    ca = trustme.CA()
    server_cert = ca.issue_cert(common_name=loopback_host)

    with run_server_in_thread("https", loopback_host, tmpdir, ca, server_cert) as cfg:
        yield cfg


@pytest.fixture
def ip_san_server(
    tmp_path_factory: pytest.TempPathFactory,
) -> Generator[ServerConfig, None, None]:
    tmpdir = tmp_path_factory.mktemp("certs")
    ca = trustme.CA()
    # IP address in Subject Alternative Name
    server_cert = ca.issue_cert("127.0.0.1")

    with run_server_in_thread("https", "127.0.0.1", tmpdir, ca, server_cert) as cfg:
        yield cfg


@pytest.fixture
def ipv6_san_server(
    tmp_path_factory: pytest.TempPathFactory,
) -> Generator[ServerConfig, None, None]:
    if not HAS_IPV6:
        pytest.skip("Only runs on IPv6 systems")

    tmpdir = tmp_path_factory.mktemp("certs")
    ca = trustme.CA()
    # IP address in Subject Alternative Name
    server_cert = ca.issue_cert("::1")

    with run_server_in_thread("https", "::1", tmpdir, ca, server_cert) as cfg:
        yield cfg


@pytest.fixture
def stub_timezone(request: pytest.FixtureRequest) -> Generator[None, None, None]:
    """
    A pytest fixture that runs the test with a stub timezone.
    """
    with stub_timezone_ctx(request.param):  # type: ignore[attr-defined]
        yield


@pytest.fixture(scope="session")
def supported_tls_versions() -> AbstractSet[Optional[str]]:
    # We have to create an actual TLS connection
    # to test if the TLS version is not disabled by
    # OpenSSL config. Ubuntu 20.04 specifically
    # disables TLSv1 and TLSv1.1.
    tls_versions = set()

    _server = HTTPSDummyServerTestCase()
    _server._start_server()
    for _ssl_version_name in (
        "PROTOCOL_TLSv1",
        "PROTOCOL_TLSv1_1",
        "PROTOCOL_TLSv1_2",
        "PROTOCOL_TLS",
    ):
        _ssl_version = getattr(ssl, _ssl_version_name, 0)
        if _ssl_version == 0:
            continue
        _sock = socket.create_connection((_server.host, _server.port))
        try:
            _sock = ssl_.ssl_wrap_socket(
                _sock, cert_reqs=ssl.CERT_NONE, ssl_version=_ssl_version
            )
        except ssl.SSLError:
            pass
        else:
            tls_versions.add(_sock.version())
        _sock.close()
    _server._stop_server()
    return tls_versions


@pytest.fixture(scope="function")
def requires_tlsv1(supported_tls_versions: AbstractSet[str]) -> None:
    """Test requires TLSv1 available"""
    if not hasattr(ssl, "PROTOCOL_TLSv1") or "TLSv1" not in supported_tls_versions:
        pytest.skip("Test requires TLSv1")


@pytest.fixture(scope="function")
def requires_tlsv1_1(supported_tls_versions: AbstractSet[str]) -> None:
    """Test requires TLSv1.1 available"""
    if not hasattr(ssl, "PROTOCOL_TLSv1_1") or "TLSv1.1" not in supported_tls_versions:
        pytest.skip("Test requires TLSv1.1")


@pytest.fixture(scope="function")
def requires_tlsv1_2(supported_tls_versions: AbstractSet[str]) -> None:
    """Test requires TLSv1.2 available"""
    if not hasattr(ssl, "PROTOCOL_TLSv1_2") or "TLSv1.2" not in supported_tls_versions:
        pytest.skip("Test requires TLSv1.2")


@pytest.fixture(scope="function")
def requires_tlsv1_3(supported_tls_versions: AbstractSet[str]) -> None:
    """Test requires TLSv1.3 available"""
    if (
        not getattr(ssl, "HAS_TLSv1_3", False)
        or "TLSv1.3" not in supported_tls_versions
    ):
        pytest.skip("Test requires TLSv1.3")
