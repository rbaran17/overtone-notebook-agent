"""Tests for overtone.ingest (port of packages/core/test/ingest.test.ts)."""

from __future__ import annotations

import io
import re
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import docx
import pytest

from overtone import ingest
from overtone.errors import OvertoneError
from overtone.ingest import (
    decode_entities,
    extract_source,
    html_og_title,
    html_title,
    html_to_text,
    normalize_text,
)
from overtone.models import FileInput, TextInput, UrlInput

PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


def enc(s: str) -> bytes:
    return s.encode("utf-8")


def build_pdf(text: str) -> bytes:
    """Minimal one-page PDF with a correct xref table, containing `text` in Helvetica."""
    stream = f"BT /F1 18 Tf 20 60 Td ({text}) Tj ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = "%PDF-1.4\n"
    offsets: list[int] = []
    for i, body in enumerate(objects):
        offsets.append(len(out))
        out += f"{i + 1} 0 obj\n{body}\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n"
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"
    return out.encode("ascii")


def build_docx() -> bytes:
    """A small real DOCX: heading, two paragraphs and a 2x2 table, in that order."""
    document = docx.Document()
    document.add_heading("Quarterly plan", level=1)
    document.add_paragraph("First paragraph about revenue.")
    document.add_paragraph("Second paragraph about hiring.")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Alpha"
    table.cell(0, 1).text = "Beta"
    table.cell(1, 0).text = "Gamma"
    table.cell(1, 1).text = "Delta"
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


async def rejects_with(coro, status: int, code: str) -> None:
    with pytest.raises(OvertoneError) as info:
        await coro
    assert info.value.status == status
    assert info.value.code == code


@pytest.fixture
def blocked_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """URL validation runs with private addresses blocked."""
    monkeypatch.delenv("OVERTONE_ALLOW_PRIVATE_URLS", raising=False)


@pytest.fixture
def allow_private(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local test server is reachable; no system proxy may intercept it."""
    monkeypatch.setenv("OVERTONE_ALLOW_PRIVATE_URLS", "1")
    for var in PROXY_VARS:
        monkeypatch.delenv(var, raising=False)


# ───────────────────────────── Pasted text ─────────────────────────────


class TestPastedText:
    async def test_derives_the_title_from_the_first_non_empty_line_stripping_hashes(self) -> None:
        src = await extract_source(TextInput(text="\n\n## Quarterly plan\n\nDetails here"))
        assert src.kind == "text"
        assert src.title == "Quarterly plan"
        assert src.text == "## Quarterly plan\n\nDetails here"

    async def test_uses_the_explicit_title_when_given(self) -> None:
        src = await extract_source(TextInput(text="body", title="  Custom  title "))
        assert src.title == "Custom title"

    async def test_truncates_a_long_first_line_for_the_title_but_keeps_all_the_text(self) -> None:
        long = "word " * 60
        long = long.strip()
        src = await extract_source(TextInput(text=long))
        assert len(src.title) <= 81, f"title too long: {len(src.title)}"
        assert src.text == long

    async def test_falls_back_to_pasted_text_and_returns_empty_text_without_throwing(self) -> None:
        src = await extract_source(TextInput(text="   \n  "))
        assert src.title == "Pasted text"
        assert src.text == ""

    def test_normalizes_line_endings_control_characters_spaces_and_blank_lines(self) -> None:
        assert normalize_text("a\r\nb\x00c\t\tthing   \n\n\n\n\nend  ") == "a\nbc thing\n\n\nend"


# ───────────────────────────────── Files ─────────────────────────────────


class TestFiles:
    async def test_reads_markdown_and_derives_the_title_from_the_filename(self) -> None:
        src = await extract_source(
            FileInput(filename="meeting_notes-2024.md", data=enc("# Agenda\n\n- item one\n- item two\n"))
        )
        assert src.kind == "file"
        assert src.title == "meeting notes 2024"
        assert src.origin == "meeting_notes-2024.md"
        assert src.mime_type == "text/markdown"
        assert src.text == "# Agenda\n\n- item one\n- item two"

    async def test_uses_the_explicit_title_over_the_filename(self) -> None:
        src = await extract_source(FileInput(filename="a.txt", data=enc("hi"), title="Given"))
        assert src.title == "Given"

    async def test_strips_a_utf8_bom(self) -> None:
        data = b"\xef\xbb\xbf" + enc("Hello BOM")
        src = await extract_source(FileInput(filename="bom.txt", data=data))
        assert src.text == "Hello BOM"

    async def test_converts_an_html_file_dropping_chrome_and_decoding_entities_and_taking_title(self) -> None:
        html = """<!doctype html><html><head><title>Field &amp; Notes</title>
<style>.x { color: red; }</style><script>var secret = 1;</script></head>
<body><nav>Home | About</nav><header>Site header</header>
<main><h1>Heading One</h1><p>Use &amp; &lt;tags&gt; &mdash; ok &#65; &#x1F600;</p>
<ul><li>First</li><li>Second</li></ul></main>
<footer>Copyright footer</footer></body></html>"""
        src = await extract_source(FileInput(filename="page.html", data=enc(html)))
        assert src.title == "Field & Notes"
        assert src.mime_type == "text/html"
        assert re.search(r"^Heading One$", src.text, re.M)
        assert "Use & <tags> — ok A \U0001F600" in src.text, src.text
        assert re.search(r"^- First$", src.text, re.M)
        assert re.search(r"^- Second$", src.text, re.M)
        assert "secret" not in src.text, "script content leaked"
        assert "color" not in src.text, "style content leaked"
        assert "Home | About" not in src.text, "nav outside <main> leaked"
        assert "Site header" not in src.text, "header outside <main> leaked"
        assert "Copyright" not in src.text, "footer outside <main> leaked"

    def test_without_main_or_article_drops_nav_header_footer_aside_and_form(self) -> None:
        text = html_to_text(
            "<nav>Menu item</nav><aside>Side</aside><form><input></form><p>Body text</p><footer>Foot</footer>"
        )
        assert text == "Body text"

    def test_reads_title_and_og_title_from_html_head_metadata(self) -> None:
        assert html_title("<head><title> A &amp; B </title></head>") == "A & B"
        assert html_title("<p>no title</p>") is None
        assert html_og_title('<meta content="Social Title" property="og:title">') == "Social Title"
        assert html_og_title("<meta name='og:title' content='Single'>") == "Single"
        assert html_og_title("<p>none</p>") is None

    def test_decode_entities_handles_named_numeric_and_invalid_references(self) -> None:
        assert decode_entities("&amp; &#65; &#x41; &mdash;") == "& A A —"
        assert decode_entities("&#0; &#xD800;") == "� �"
        assert decode_entities("&unknown; &amp") == "&unknown; &amp"

    async def test_rejects_unknown_binary_data_with_415(self) -> None:
        await rejects_with(
            extract_source(FileInput(filename="blob.bin", data=bytes([0x89, 0x50, 0x4E, 0x47, 0, 1, 2, 3]))),
            415,
            "unsupported_file",
        )

    async def test_rejects_invalid_utf8_with_no_known_extension_with_415(self) -> None:
        await rejects_with(
            extract_source(FileInput(filename="data.dat", data=bytes([0xFF, 0xFE, 0xFD, 0xFC]))),
            415,
            "unsupported_file",
        )

    async def test_rejects_legacy_doc_with_415(self) -> None:
        await rejects_with(extract_source(FileInput(filename="old.doc", data=enc("whatever"))), 415, "unsupported_file")

    async def test_rejects_images_by_extension_with_415(self) -> None:
        await rejects_with(extract_source(FileInput(filename="photo.PNG", data=enc("nope"))), 415, "unsupported_file")

    async def test_maps_garbage_bytes_named_docx_to_422_unreadable_file(self) -> None:
        await rejects_with(
            extract_source(FileInput(filename="broken.docx", data=enc("this is not a zip"))),
            422,
            "unreadable_file",
        )

    async def test_extracts_text_from_a_hand_built_pdf(self) -> None:
        src = await extract_source(FileInput(filename="hello_overtone.pdf", data=build_pdf("Hello Overtone PDF")))
        assert src.kind == "file"
        assert src.title == "hello overtone"
        assert src.mime_type == "application/pdf"
        assert "Hello Overtone" in src.text, f"unexpected PDF text: {src.text!r}"

    async def test_maps_a_corrupt_pdf_to_422_unreadable_file(self) -> None:
        await rejects_with(
            extract_source(FileInput(filename="bad.pdf", data=enc("%PDF-1.4\ngarbage"))),
            422,
            "unreadable_file",
        )

    async def test_extracts_a_real_docx_in_document_order_with_table_rows(self) -> None:
        src = await extract_source(FileInput(filename="plan.docx", data=build_docx()))
        assert src.kind == "file"
        assert src.title == "plan"
        assert src.mime_type == ingest.DOCX_MIME
        text = src.text
        for needle in ("Quarterly plan", "First paragraph about revenue.", "Second paragraph about hiring."):
            assert needle in text, text
        assert "Alpha | Beta" in text, text
        assert "Gamma | Delta" in text, text
        assert text.index("Quarterly plan") < text.index("First paragraph") < text.index("Alpha | Beta")


# ──────────────────────────── URL validation ────────────────────────────


class TestUrlValidation:
    async def test_rejects_non_http_schemes_and_invalid_urls_with_400_bad_url(self, blocked_env: None) -> None:
        for url in ["ftp://example.com/file.txt", "file:///etc/passwd", "javascript:alert(1)", "not a url"]:
            await rejects_with(extract_source(UrlInput(url=url)), 400, "bad_url")

    async def test_blocks_loopback_private_and_link_local_addresses_with_400_blocked_url(self, blocked_env: None) -> None:
        blocked = [
            "http://localhost:8080/",
            "http://api.localhost/",
            "http://printer.local/",
            "http://db.internal/",
            "http://127.0.0.1/",
            "http://127.1/",
            "http://10.1.2.3/",
            "http://192.168.1.10/",
            "http://172.16.0.1/",
            "http://169.254.169.254/latest/meta-data",
            "http://0.0.0.0/",
            "http://[::1]/",
            "http://[fd00::1]/",
            "http://[fe80::1]/",
            "http://[::ffff:127.0.0.1]/",
            "http://[::ffff:7f00:1]/",
            "http://[64:ff9b::7f00:1]/",
        ]
        for url in blocked:
            await rejects_with(extract_source(UrlInput(url=url)), 400, "blocked_url")

    async def test_blocks_a_hostname_that_resolves_to_loopback_when_private_urls_are_not_allowed(
        self, blocked_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_resolve(host: str, port: int) -> list[str]:
            return ["127.0.0.1"]

        monkeypatch.setattr(ingest, "_resolve_host", fake_resolve)
        await rejects_with(extract_source(UrlInput(url="http://rebind.example.test/page")), 400, "blocked_url")

    async def test_blocks_when_any_resolved_address_is_private(
        self, blocked_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_resolve(host: str, port: int) -> list[str]:
            return ["93.184.216.34", "10.0.0.5"]

        monkeypatch.setattr(ingest, "_resolve_host", fake_resolve)
        await rejects_with(extract_source(UrlInput(url="http://mixed.example.test/")), 400, "blocked_url")

    async def test_maps_a_dns_failure_to_502_fetch_failed(self, blocked_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        async def failing_resolve(host: str, port: int) -> list[str]:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

        monkeypatch.setattr(ingest, "_resolve_host", failing_resolve)
        await rejects_with(extract_source(UrlInput(url="http://no-such-host.example.test/")), 502, "fetch_failed")

    async def test_does_not_resolve_names_when_private_urls_are_allowed(
        self, allow_private: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def must_not_resolve(host: str, port: int) -> list[str]:
            raise AssertionError("DNS should not be consulted when OVERTONE_ALLOW_PRIVATE_URLS=1")

        monkeypatch.setattr(ingest, "_resolve_host", must_not_resolve)
        # Refused connection on a closed port: proves the request went past validation.
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        await rejects_with(extract_source(UrlInput(url=f"http://rebind.example.test:{port}/")), 502, "fetch_failed")


# ──────────────────────── URL fetching (local server) ────────────────────────

PAGE_HTML = (
    "<html><head><title>Local Page</title></head><body><nav>Skip me</nav>"
    "<article><h2>Article heading</h2><p>Body &amp; more</p></article></body></html>"
)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, format: str, *args: object) -> None:  # silence the test server
        return

    def _send(self, status: int, content_type: str | None, body: bytes, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        if content_type is not None:
            self.send_header("Content-Type", content_type)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client gave up (size cap); expected

    def do_GET(self) -> None:
        path = self.path
        pdf = build_pdf("Hello Overtone PDF")
        if path == "/page.html":
            self._send(200, "text/html; charset=utf-8", PAGE_HTML.encode())
        elif path == "/plain":
            self._send(200, "text/plain", b"plain body\n")
        elif path == "/redirect":
            self._send(302, None, b"", {"Location": "/page.html"})
        elif path == "/loop":
            self._send(302, None, b"", {"Location": "/loop"})
        elif path == "/missing":
            self._send(404, "text/plain", b"nope")
        elif path == "/doc.pdf":
            self._send(200, "application/pdf", pdf)
        elif path == "/big":
            # Declared 26 MB: rejected from the headers alone.
            self._send(200, "text/plain", b"a" * (26 * 1024 * 1024))
        elif path == "/big-stream":
            # No Content-Length: the size cap must be enforced while streaming.
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            chunk = b"a" * (1024 * 1024)
            try:
                for _ in range(26):
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self._send(500, None, b"")


@pytest.fixture(scope="module")
def local_base() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


class TestUrlFetchingAgainstLocalServer:
    async def test_fetches_an_html_page_title_from_title_and_body_converted(
        self, local_base: str, allow_private: None
    ) -> None:
        src = await extract_source(UrlInput(url=f"{local_base}/page.html"))
        assert src.kind == "url"
        assert src.title == "Local Page"
        assert src.origin == f"{local_base}/page.html"
        assert src.mime_type == "text/html"
        assert re.search(r"Article heading", src.text)
        assert re.search(r"Body & more", src.text)
        assert "Skip me" not in src.text

    async def test_fetches_plain_text_title_falls_back_to_hostname_and_path(
        self, local_base: str, allow_private: None
    ) -> None:
        src = await extract_source(UrlInput(url=f"{local_base}/plain"))
        assert src.text == "plain body"
        assert src.title == "127.0.0.1/plain"
        assert src.mime_type == "text/plain"

    async def test_follows_a_redirect_and_reports_the_final_url_as_origin(
        self, local_base: str, allow_private: None
    ) -> None:
        src = await extract_source(UrlInput(url=f"{local_base}/redirect"))
        assert src.origin == f"{local_base}/page.html"
        assert src.title == "Local Page"

    async def test_gives_up_after_too_many_redirects_with_502(self, local_base: str, allow_private: None) -> None:
        await rejects_with(extract_source(UrlInput(url=f"{local_base}/loop")), 502, "fetch_failed")

    async def test_maps_a_404_to_502_fetch_failed_mentioning_the_status(self, local_base: str, allow_private: None) -> None:
        with pytest.raises(OvertoneError) as info:
            await extract_source(UrlInput(url=f"{local_base}/missing"))
        assert info.value.status == 502
        assert info.value.code == "fetch_failed"
        assert "404" in info.value.message

    async def test_maps_a_refused_connection_to_502_fetch_failed(self, allow_private: None) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        # The socket is closed again, so nothing listens on this port.
        await rejects_with(extract_source(UrlInput(url=f"http://127.0.0.1:{port}/")), 502, "fetch_failed")

    async def test_rejects_a_declared_body_over_25_mb_with_413_too_large(self, local_base: str, allow_private: None) -> None:
        await rejects_with(extract_source(UrlInput(url=f"{local_base}/big")), 413, "too_large")

    async def test_rejects_an_undeclared_body_over_25_mb_while_streaming_with_413(
        self, local_base: str, allow_private: None
    ) -> None:
        await rejects_with(extract_source(UrlInput(url=f"{local_base}/big-stream")), 413, "too_large")

    async def test_extracts_a_pdf_served_over_http(self, local_base: str, allow_private: None) -> None:
        src = await extract_source(UrlInput(url=f"{local_base}/doc.pdf"))
        assert src.mime_type == "application/pdf"
        assert "Hello Overtone" in src.text
