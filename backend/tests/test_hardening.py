"""Regression tests for the security review: each one replays an attack that used to work."""

from __future__ import annotations

import io
import random
import re
import socket
import threading
import time
import zipfile
from collections.abc import AsyncIterator, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from overtone import MemoryStorage, MockChatClient, cli, create_engine, ingest
from overtone.api import create_app
from overtone.errors import INTERNAL_MESSAGE, OvertoneError, public_message
from overtone.models import FileInput, UrlInput


def make_app(**options: Any) -> Any:
    return create_app(create_engine(storage=MemoryStorage(), chat_client=MockChatClient(delay=0)), **options)


def client(app: Any, host: str = "127.0.0.1:8787") -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://{host}")


@pytest.fixture(autouse=True)
def no_ambient_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OVERTONE_API_KEY", raising=False)
    monkeypatch.delenv("OVERTONE_ALLOWED_HOSTS", raising=False)


# ───────────────────── A keyless API answers only to its own machine ─────────────────────


@pytest.mark.parametrize("host", ["127.0.0.1:8787", "localhost:8787", "LOCALHOST", "[::1]:8787", "192.168.1.20:8787"])
async def test_keyless_api_answers_to_localhost_and_ip_addresses(host: str) -> None:
    async with client(make_app(), host) as c:
        assert (await c.get("/notebooks")).status_code == 200


@pytest.mark.parametrize("host", ["attacker.example:8787", "attacker.example", "127.0.0.1.attacker.example", "a b", ""])
async def test_dns_rebinding_host_is_refused(host: str) -> None:
    """A page on attacker.example re-pointed at 127.0.0.1 is same-origin, but still sends its own name."""
    async with client(make_app()) as c:
        res = await c.get("/notebooks", headers={"Host": host})
        assert res.status_code == 403
        assert res.json()["error"]["code"] == "forbidden_host"
        created = await c.post("/notebooks", json={"title": "x"}, headers={"Host": host})
        assert created.status_code == 403


async def test_extra_host_names_can_be_allowed_by_option_or_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    async with client(make_app(allowed_hosts=["notes.internal"]), "notes.internal") as c:
        assert (await c.get("/notebooks")).status_code == 200
        assert (await c.get("/notebooks", headers={"Host": "other.internal"})).status_code == 403
    async with client(make_app(allowed_hosts="*"), "anything.example") as c:
        assert (await c.get("/notebooks")).status_code == 200
    monkeypatch.setenv("OVERTONE_ALLOWED_HOSTS", "notes.internal, wiki.internal")
    async with client(make_app(), "wiki.internal:8787") as c:
        assert (await c.get("/notebooks")).status_code == 200


@pytest.mark.parametrize("origin", ["https://evil.example", "http://attacker.example:8787", "null", "http://203.0.113.9"])
async def test_cross_site_request_cannot_change_anything(origin: str) -> None:
    """The blind CSRF from the review: a text/plain POST needs no preflight and used to be accepted."""
    app = make_app()
    async with client(app) as c:
        res = await c.post(
            "/notebooks", content=b'{"title":"from another site"}', headers={"Content-Type": "text/plain", "Origin": origin}
        )
        assert res.status_code == 403
        assert res.json()["error"]["code"] == "forbidden_origin"
        oneshot = await c.post(
            "/oneshot",
            content=b'{"sources":[{"type":"text","text":"x"}]}',
            headers={"Content-Type": "text/plain", "Origin": origin},
        )
        assert oneshot.status_code == 403
        assert (await c.get("/notebooks")).json() == {"notebooks": []}


@pytest.mark.parametrize(
    "origin", [None, "http://127.0.0.1:8787", "http://localhost:3000", "http://127.0.0.1:3000", "http://[::1]:3000"]
)
async def test_local_callers_and_the_bundled_ui_still_work(origin: str | None) -> None:
    headers = {} if origin is None else {"Origin": origin}
    async with client(make_app()) as c:
        assert (await c.post("/notebooks", json={"title": "mine"}, headers=headers)).status_code == 201


async def test_origins_on_the_cors_list_are_accepted_and_reads_are_not_origin_checked() -> None:
    async with client(make_app(cors=["https://app.example"])) as c:
        ok = await c.post("/notebooks", json={"title": "x"}, headers={"Origin": "https://app.example"})
        assert ok.status_code == 201
        assert (await c.post("/notebooks", json={}, headers={"Origin": "https://evil.example"})).status_code == 403
        # A cross-site GET changes nothing and its response is unreadable without a CORS grant.
        assert (await c.get("/notebooks", headers={"Origin": "https://evil.example"})).status_code == 200


async def test_with_an_api_key_the_key_is_the_check() -> None:
    async with client(make_app(api_key="k"), "api.example.com") as c:
        assert (await c.get("/notebooks")).status_code == 401
        auth = {"Authorization": "Bearer k", "Origin": "https://app.example"}
        assert (await c.post("/notebooks", json={"title": "x"}, headers=auth)).status_code == 201


# ───────────────────── OVERTONE_API_KEY applies however the app is started ─────────────────────


async def test_create_app_reads_the_api_key_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """`uvicorn overtone.api:create_app --factory` with the variable exported used to serve an open API."""
    monkeypatch.setenv("OVERTONE_API_KEY", "s3cret")
    async with client(make_app()) as c:
        assert (await c.get("/notebooks")).status_code == 401
        assert (await c.get("/notebooks", headers={"Authorization": "Bearer s3cret"})).status_code == 200
    async with client(make_app(api_key="explicit")) as c:
        assert (await c.get("/notebooks", headers={"Authorization": "Bearer s3cret"})).status_code == 401
    async with client(make_app(api_key="")) as c:  # explicit opt-out
        assert (await c.get("/notebooks")).status_code == 200


# ───────────────────── Request size ─────────────────────


async def test_oversized_bodies_are_refused_with_413() -> None:
    limit = 1024
    app = make_app(max_upload_bytes=limit)
    cap = 4 * 50 * 1024 * 1024  # four uploads of the default size
    async with client(app) as c:
        declared = await c.post("/notebooks", content=b"{}", headers={"Content-Length": str(cap + 1)})
        assert declared.status_code == 413
        assert declared.json()["error"]["code"] == "too_large"


async def test_a_body_without_a_length_is_counted_as_it_arrives(monkeypatch: pytest.MonkeyPatch) -> None:
    from overtone import api as api_mod

    monkeypatch.setattr(api_mod, "DEFAULT_MAX_UPLOAD", 1024)
    app = make_app(max_upload_bytes=1024)  # request cap: 4 KiB

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(64):
            yield b"a" * 1024

    async with client(app) as c:
        res = await c.post("/notebooks", content=chunks())
        assert res.status_code == 413
        nb = (await c.post("/notebooks", json={"title": "x"})).json()["notebook"]["id"]
        form = await c.post(f"/notebooks/{nb}/sources", files={"file": ("big.txt", b"b" * 8192, "text/plain")})
        assert form.status_code == 413


async def test_a_multipart_body_cannot_carry_thousands_of_parts() -> None:
    async with client(make_app()) as c:
        nb = (await c.post("/notebooks", json={"title": "x"})).json()["notebook"]["id"]
        many = [("text", (None, "x")) for _ in range(500)]
        res = await c.post(f"/notebooks/{nb}/sources", files=many)
        assert res.status_code == 400
        assert res.json()["error"]["code"] == "bad_multipart"


# ───────────────────── URL fetching: addresses ─────────────────────


@pytest.mark.parametrize(
    "address",
    ["100.100.100.200", "100.64.0.1", "169.254.169.254", "10.0.0.1", "224.0.0.1", "fec0::1", "ff02::1", "::ffff:100.64.0.1"],
)
def test_addresses_off_the_public_internet_are_blocked(address: str) -> None:
    import ipaddress

    assert ingest._is_blocked_address(ipaddress.ip_address(address)) is True


@pytest.mark.parametrize("address", ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111"])
def test_public_addresses_are_not_blocked(address: str) -> None:
    import ipaddress

    assert ingest._is_blocked_address(ipaddress.ip_address(address)) is False


class _Named(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    body = b""
    seen: list[str]

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        type(self).seen.append(self.headers.get("Host", ""))
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)


def _serve(address: str, port: int, body: bytes) -> tuple[ThreadingHTTPServer, list[str]]:
    seen: list[str] = []
    handler = type("Handler", (_Named,), {"body": body, "seen": seen})
    server = ThreadingHTTPServer((address, port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


@pytest.fixture
def two_faced_name(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, list[str], list[str]]]:
    """`rebind.test` answers 127.0.0.1 when it is checked and 127.0.0.2 when anything looks it up again."""
    checked, seen_checked = _serve("127.0.0.1", 0, b"the page that was checked\n")
    port = checked.server_address[1]
    try:
        internal, seen_internal = _serve("127.0.0.2", port, b"internal secret\n")
    except OSError:
        checked.shutdown()
        checked.server_close()
        pytest.skip("needs a second loopback address (127.0.0.2)")

    async def first_answer(host: str, port_: int) -> list[str]:
        return ["127.0.0.1"]

    real_getaddrinfo = socket.getaddrinfo

    def second_answer(host: Any, *args: Any, **kwargs: Any) -> Any:
        return real_getaddrinfo("127.0.0.2" if host in ("rebind.test", b"rebind.test") else host, *args, **kwargs)

    monkeypatch.delenv("OVERTONE_ALLOW_PRIVATE_URLS", raising=False)
    monkeypatch.setattr(ingest, "_resolve_host", first_answer)
    monkeypatch.setattr(ingest, "_is_blocked_address", lambda ip: False)  # stand-in for "a public address"
    monkeypatch.setattr(ingest, "_uses_proxy", lambda parts: False)
    monkeypatch.setattr(socket, "getaddrinfo", second_answer)
    try:
        yield f"http://rebind.test:{port}/page", seen_checked, seen_internal
    finally:
        for server in (checked, internal):
            server.shutdown()
            server.server_close()


async def test_the_connection_goes_to_the_address_that_was_checked(two_faced_name: tuple[str, list[str], list[str]]) -> None:
    """DNS rebinding against the SSRF filter: the name used to be resolved again for the connection."""
    url, seen_checked, seen_internal = two_faced_name
    source = await ingest.extract_source(UrlInput(url=url))
    assert source.text == "the page that was checked"
    assert seen_internal == []
    # The request still names the host, so virtual hosts (and TLS, which uses the same name) keep working.
    assert seen_checked == [url.split("/")[2]]


async def test_a_literal_address_is_pinned_and_a_proxy_disables_pinning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OVERTONE_ALLOW_PRIVATE_URLS", raising=False)
    monkeypatch.setattr(ingest, "_uses_proxy", lambda parts: False)
    assert await ingest._assert_fetchable("https://93.184.216.34/x") == ["93.184.216.34"]

    async def resolve(host: str, port: int) -> list[str]:
        return ["93.184.216.34", "93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"]

    monkeypatch.setattr(ingest, "_resolve_host", resolve)
    assert await ingest._assert_fetchable("https://example.com/") == ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"]
    monkeypatch.setattr(ingest, "_uses_proxy", lambda parts: True)  # the proxy does the lookup
    assert await ingest._assert_fetchable("https://example.com/") == []


def test_pinning_keeps_the_host_header_and_tls_name() -> None:
    import asyncio

    seen: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.update(url=str(request.url), host=request.headers["host"], sni=request.extensions.get("sni_hostname"))
        return httpx.Response(200, text="ok")

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            async with ingest._open_pinned(http, "https://Example.com:8443/a?b=1", ["2001:db8::7"]) as res:
                assert res.status_code == 200

    asyncio.run(run())
    assert seen == {"url": "https://[2001:db8::7]:8443/a?b=1", "host": "example.com:8443", "sni": "example.com"}


# ───────────────────── HTML conversion: linear time, same results ─────────────────────

_OLD_TITLE = re.compile(r"<title\b[^>]*>([\s\S]*?)</title\s*>", re.IGNORECASE)


def _old_remove(html: str, tags: tuple[str, ...]) -> str:
    out = html
    for tag in tags:
        out = re.sub(rf"<{tag}\b[^>]*>[\s\S]*?(?:</{tag}\s*>|\Z)", " ", out, flags=re.IGNORECASE)
    return out


def _old_inner(html: str, tag: str) -> list[str]:
    pattern = rf"<{tag}\b[^>]*>([\s\S]*?)(?:</{tag}\s*>|\Z)"
    return [m.group(1) for m in re.finditer(pattern, html, flags=re.IGNORECASE)]


def _old_fragment(fragment: str) -> str:
    s = ingest._WS_RE.sub(" ", fragment)
    s = ingest._BR_RE.sub("\n", s)
    s = ingest._LI_RE.sub("\n- ", s)
    s = ingest._BLOCK_RE.sub("\n", s)
    s = ingest._DECL_RE.sub("", s)
    s = ingest._TAG_ANY_RE.sub("", s)
    s = ingest.decode_entities(s)
    return ingest.normalize_text("\n".join(line.strip() for line in s.split("\n")))


def _old_title(html: str) -> str | None:
    m = _OLD_TITLE.search(ingest._strip_comments(html))
    if not m:
        return None
    return ingest._collapse(ingest.decode_entities(ingest._TAG_RE.sub("", m.group(1)))) or None


_PIECES = [
    "<title>", "<title lang='en'>", "</title>", "</TITLE >", "<main>", "</main>", "<MAIN class=x>", "<article>",
    "</article>", "<script>", "</script >", "<style", "</style>", "<nav>", "</nav>", "<header>", "</header>",
    "<head>", "</head>", "<!--", "-->", "<p>", "</p>", "<br>", "<br/", "<li>", "<div a='>'>", "</div>", "<a", "<",
    ">", "< ", "<!doctype html>", "&amp;", "&#x41;", "&bogus", "text", " ", "\n", "x<y", "<svg", "</svg>", "<title",
]  # fmt: skip


def test_the_linear_scanners_agree_with_the_patterns_they_replaced() -> None:
    rng = random.Random(20261119)
    for _ in range(4000):
        html = "".join(rng.choice(_PIECES) for _ in range(rng.randint(0, 14)))
        assert ingest._remove_elements(html, ingest._STRIP_TAGS) == _old_remove(html, ingest._STRIP_TAGS), html
        assert ingest._remove_elements(html, ingest._CHROME_TAGS) == _old_remove(html, ingest._CHROME_TAGS), html
        for tag in ("main", "article", "title"):
            assert ingest._inner_of_all(html, tag) == _old_inner(html, tag), html
        assert ingest._fragment_to_text(html) == _old_fragment(html), html
        assert ingest.html_title(html) == _old_title(html), html
        assert ingest.html_og_title(html) == ingest.html_og_title(html + " ")


@pytest.mark.parametrize(
    "page",
    [
        "<a" * 100_000,
        "<title>" * 30_000,
        "<main" * 40_000,
        "<head<script<style<nav" * 10_000,
        "<br" * 70_000,
        "<!x" * 70_000,
        "<meta" * 40_000,
        "</title " * 25_000 + "<title>" * 5_000,
        "<title>" + "<b" * 90_000 + "</title>",
    ],
    ids=["a", "title", "main", "stripped", "br", "decl", "meta", "close", "title-inner"],
)
def test_a_crafted_page_cannot_stall_the_converter(page: str) -> None:
    """200 KB of `<title>` used to take 52 seconds, and the whole server was frozen for all of it."""
    start = time.perf_counter()
    ingest.html_to_text(page)
    ingest.html_title(page)
    ingest.html_og_title(page)
    assert time.perf_counter() - start < 5.0


def test_only_real_text_encodings_are_honoured() -> None:
    assert ingest._decode_text("Привет".encode("cp1251"), "windows-1251") == "Привет"
    assert ingest._decode_text("こんにちは".encode("shift_jis"), "Shift_JIS") == "こんにちは"
    for codec in ("punycode", "zlib", "idna", "unicode_escape", "utf-7", "rot13", "hex"):
        assert ingest._decode_text(b"plain \\u0041 text", codec) == "plain \\u0041 text", codec


# ───────────────────── DOCX: unpacked size ─────────────────────


def _docx_bomb(megabytes: int) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", b" " * (megabytes * 1024 * 1024))
    return out.getvalue()


async def test_a_docx_that_unpacks_far_larger_than_it_is_refused_before_unpacking() -> None:
    bomb = _docx_bomb(80)
    assert len(bomb) < 200 * 1024  # 80 MB of XML in under 200 KB
    start = time.perf_counter()
    with pytest.raises(OvertoneError) as raised:
        await ingest.extract_source(FileInput(filename="report.docx", data=bomb))
    assert (raised.value.status, raised.value.code) == (413, "too_large")
    assert time.perf_counter() - start < 2.0


def test_docx_limits_count_parts_and_total_size() -> None:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as archive:
        for i in range(ingest.MAX_DOCX_PARTS + 1):
            archive.writestr(f"word/media/{i}.bin", b"")
    with pytest.raises(OvertoneError):
        ingest._assert_docx_unpacks_small(out.getvalue())
    ingest._assert_docx_unpacks_small(b"not a zip")  # left for python-docx to report as unreadable


# ───────────────────── Errors and the CLI ─────────────────────


def test_only_errors_written_for_the_caller_reach_the_caller() -> None:
    assert public_message(OvertoneError("Message is empty.", status=400, code="empty_message")) == "Message is empty."
    leaked = PermissionError(13, "Permission denied", "/srv/overtone/data/.overtone/messages/abc.json.tmp")
    assert public_message(leaked) == INTERNAL_MESSAGE
    assert "/srv" not in public_message(leaked)


class _Io:
    def __init__(self) -> None:
        self.out, self.err = "", ""

    def stdout(self, text: str) -> None:
        self.out += text

    def stderr(self, text: str) -> None:
        self.err += text


def _serve_cli(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> tuple[int, _Io, dict[str, Any]]:
    import sys
    import types

    created: dict[str, Any] = {}

    def fake_create_app(engine: Any = None, **options: Any) -> str:
        created.update(options)
        return "APP"

    async def run_server(app: Any, *, host: str, port: int) -> None:
        return None

    fake_api = types.ModuleType("overtone.api")
    fake_api.create_app = fake_create_app  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "overtone.api", fake_api)
    monkeypatch.setattr(cli, "_run_server", run_server)
    io_ = _Io()
    code = cli.main(argv, stdout=io_.stdout, stderr=io_.stderr, engine=SimpleNamespace(), stdin=None)
    return code, io_, created


def test_serve_refuses_cors_for_every_origin_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    code, io_, created = _serve_cli(monkeypatch, ["serve", "--cors"])
    assert code != 0
    assert "needs an API key" in io_.err
    assert created == {}
    code, _, created = _serve_cli(monkeypatch, ["serve", "--cors", "--api-key", "k"])
    assert code == 0
    assert created == {"api_key": "k", "cors": True}
    code, _, created = _serve_cli(monkeypatch, ["serve", "--cors", "http://localhost:3000"])
    assert code == 0
    assert created == {"api_key": "", "cors": ["http://localhost:3000"]}


def test_serve_warns_when_listening_beyond_this_machine_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _, io_, _ = _serve_cli(monkeypatch, ["serve", "--host", "0.0.0.0"])
    assert "without an API key" in io_.err
    for argv in (["serve"], ["serve", "--host", "localhost"], ["serve", "--host", "0.0.0.0", "--api-key", "k"]):
        _, quiet, _ = _serve_cli(monkeypatch, argv)
        assert "Warning" not in quiet.err, argv


def test_serve_passes_allowed_host_names_through(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, created = _serve_cli(monkeypatch, ["serve", "--allow-host", "notes.internal, wiki.internal"])
    assert created["allowed_hosts"] == ["notes.internal", "wiki.internal"]


def test_a_default_output_name_never_overwrites_an_existing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The name comes from a model-written title: "requirements" must not replace requirements.txt."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "requirements.txt").write_text("fastapi\n")
    first = cli._unused_path("requirements", ".txt")
    assert Path(first).name == "requirements-2.txt"
    Path(first).write_text("x")
    assert Path(cli._unused_path("requirements", ".txt")).name == "requirements-3.txt"
    assert Path(cli._unused_path("episode", ".mp3")).name == "episode.mp3"
    assert (tmp_path / "requirements.txt").read_text() == "fastapi\n"
