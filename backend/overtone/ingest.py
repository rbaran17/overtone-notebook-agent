"""Turn pasted text, uploaded files and URLs into plain text plus a title.

Port of `packages/core/src/ingest.ts`. The text is never truncated: the engine
decides what to do with long documents.
"""

from __future__ import annotations

import asyncio
import codecs
import html as html_lib
import io
import ipaddress
import itertools
import os
import re
import socket
import urllib.request
import zipfile
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal
from urllib.parse import SplitResult, unquote, urljoin, urlsplit, urlunsplit

import docx
import httpx
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader

from .errors import OvertoneError
from .models import ExtractedSource, FileInput, SourceInput, TextInput, UrlInput

MAX_BYTES = 25 * 1024 * 1024
FETCH_TIMEOUT_S = 20.0
MAX_REDIRECTS = 5
# A .docx is a zip: a few hundred kilobytes can unpack to gigabytes. Refuse before unpacking.
MAX_DOCX_XML_BYTES = 64 * 1024 * 1024
MAX_DOCX_PARTS = 5000
USER_AGENT = "Mozilla/5.0 (compatible; OvertoneBot/0.1)"
ACCEPT = "text/html,application/pdf,text/plain,*/*"
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
SUPPORTED_HINT = "Supported: PDF, DOCX, HTML, Markdown and plain text."

Format = Literal["pdf", "docx", "html", "text"]
Classification = Literal["pdf", "docx", "html", "text", "unsupported"]

# ───────────────────────── Format detection ─────────────────────────

_EXT_FORMAT: dict[str, Format] = {
    "pdf": "pdf",
    "docx": "docx",
    "html": "html",
    "htm": "html",
    "xhtml": "html",
}

# Plain-text-like extensions, including common source-code files.
_TEXT_EXTS = frozenset(
    (
        "txt text md markdown mdx rst adoc csv tsv json jsonl ndjson xml yaml yml toml ini cfg conf log "
        "srt vtt svg tex bib js jsx ts tsx mjs cjs py rb go rs java kt kts swift c h cc cpp cxx hpp cs php "
        "sh bash zsh ps1 sql css scss less vue svelte r lua pl scala clj ex exs hs dart gradle env properties diff patch"
    ).split()
)

# Formats we know we cannot read; rejected by name before any sniffing.
_UNSUPPORTED_EXTS = frozenset(
    (
        "doc dot rtf odt ods odp xls xlsx xlsm ppt pptx pages numbers key epub mobi "
        "zip gz tgz tar 7z rar bz2 xz exe dll so dylib bin dmg iso "
        "png jpg jpeg gif webp bmp tif tiff heic heif ico psd avif "
        "mp3 wav m4a aac ogg oga opus flac wma mp4 m4v mov avi mkv webm wmv flv"
    ).split()
)

_EXT_MIME: dict[str, str] = {
    "md": "text/markdown",
    "markdown": "text/markdown",
    "csv": "text/csv",
    "tsv": "text/tab-separated-values",
    "json": "application/json",
    "xml": "application/xml",
    "svg": "image/svg+xml",
    "yaml": "application/yaml",
    "yml": "application/yaml",
}

_UNSUPPORTED_MIME_PREFIXES = ("image/", "audio/", "video/", "font/", "model/")
_UNSUPPORTED_MIMES = frozenset(
    {
        "application/rtf",
        "text/rtf",
        "application/msword",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
        "application/vnd.oasis.opendocument.text",
        "application/zip",
        "application/x-zip-compressed",
        "application/x-tar",
        "application/gzip",
        "application/epub+zip",
        "application/x-7z-compressed",
        "application/x-rar-compressed",
    }
)
# application/* types that are really text.
_TEXT_APP_MIME = re.compile(
    r"^application/(json|xml|yaml|x-yaml|javascript|x-javascript|ecmascript|typescript|x-sh|sql|csv|x-ndjson|x-httpd-php)$"
    r"|\+(json|xml)$"
)

# Charset labels whose WHATWG meaning differs from Python's codec of the same name.
_CHARSET_ALIASES: dict[str, str] = {
    "ascii": "cp1252",
    "us-ascii": "cp1252",
    "iso-8859-1": "cp1252",
    "iso8859-1": "cp1252",
    "latin1": "cp1252",
    "l1": "cp1252",
    "windows-1252": "cp1252",
}


# Text encodings a page or file may declare. Anything else falls back to UTF-8: Python also knows codecs
# that are not text encodings ("zlib") or whose decoder is quadratic in the input ("punycode").
_TEXT_CODECS = frozenset(
    codecs.lookup(name).name
    for name in (
        "utf-8 utf-8-sig utf-16 utf-16-le utf-16-be utf-32 utf-32-le utf-32-be ascii latin-1 "
        "cp1250 cp1251 cp1252 cp1253 cp1254 cp1255 cp1256 cp1257 cp1258 cp437 cp850 cp866 cp874 "
        "iso8859-2 iso8859-3 iso8859-4 iso8859-5 iso8859-6 iso8859-7 iso8859-8 iso8859-9 iso8859-10 "
        "iso8859-13 iso8859-14 iso8859-15 iso8859-16 koi8-r koi8-u mac-roman mac-cyrillic tis-620 "
        "gbk gb2312 gb18030 big5 big5hkscs euc-jp shift_jis cp932 iso2022-jp euc-kr cp949"
    ).split()
)


def _basename(name: str) -> str:
    return re.split(r"[\\/]", name)[-1]


def _ext_of(name: str) -> str | None:
    m = re.search(r"\.([a-z0-9]+)\Z", _basename(name), re.IGNORECASE)
    return m.group(1).lower() if m else None


def _classify_ext(ext: str | None) -> Classification | None:
    if not ext:
        return None
    if ext in _UNSUPPORTED_EXTS:
        return "unsupported"
    fmt = _EXT_FORMAT.get(ext)
    if fmt:
        return fmt
    if ext in _TEXT_EXTS:
        return "text"
    return None


def _mime_base(mime_type: str | None) -> str:
    """Lower-cased media type with parameters removed ("" when absent)."""
    if not mime_type:
        return ""
    return mime_type.split(";")[0].strip().lower()


def _classify_mime(mime_type: str | None) -> Classification | None:
    m = _mime_base(mime_type)
    if not m:
        return None
    if m == PDF_MIME:
        return "pdf"
    if m == DOCX_MIME:
        return "docx"
    if m in ("text/html", "application/xhtml+xml"):
        return "html"
    if m == "image/svg+xml":
        return "text"
    if m in _UNSUPPORTED_MIMES or m.startswith(_UNSUPPORTED_MIME_PREFIXES):
        return "unsupported"
    if m.startswith("text/") or _TEXT_APP_MIME.search(m):
        return "text"
    return None


def _classify(name: str, mime_type: str | None, mime_first: bool) -> Classification | None:
    """Extension and mime type, in the order the caller prefers."""
    if mime_first:
        return _classify_mime(mime_type) or _classify_ext(_ext_of(name))
    return _classify_ext(_ext_of(name)) or _classify_mime(mime_type)


def _is_pdf_bytes(data: bytes) -> bool:
    return data.startswith(b"%PDF-")


def _looks_like_text(data: bytes) -> bool:
    """No NUL bytes in the first few KB, and the whole buffer is valid UTF-8."""
    if b"\x00" in data[:4096]:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _text_mime_type(name: str, mime_type: str | None) -> str:
    by_ext = _EXT_MIME.get(_ext_of(name) or "")
    if by_ext:
        return by_ext
    m = _mime_base(mime_type)
    if m and (m.startswith("text/") or _TEXT_APP_MIME.search(m)):
        return m
    return "text/plain"


def _unsupported(name: str) -> OvertoneError:
    return OvertoneError(f"Unsupported file type: {name}. {SUPPORTED_HINT}", status=415, code="unsupported_file")


def _unreadable(what: str, exc: BaseException) -> OvertoneError:
    return OvertoneError(f"Could not read that {what}: {_describe(exc)}", status=422, code="unreadable_file")


def _describe(exc: BaseException) -> str:
    """Short human-readable reason for an error, including its cause when there is one."""
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return f"timed out after {FETCH_TIMEOUT_S:g} seconds"
    message = str(exc)
    cause = exc.__cause__
    if cause is not None and str(cause):
        return f"{message} ({cause})" if message else str(cause)
    return message or "unknown error"


# ───────────────────────── Format converters ─────────────────────────


def _pdf_to_text(data: bytes) -> str:
    """Text of every page, joined with blank lines. CPU-bound: run in a thread."""
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:
        raise OvertoneError(f"Could not read that PDF: {_describe(exc)}", status=422, code="unreadable_file") from exc


def _docx_to_text(data: bytes) -> str:
    """Paragraphs and tables in document order (table rows as " | "-separated lines). Run in a thread.

    Cells are joined with " | " rather than tabs: normalize_text collapses tabs inside a line,
    which would make adjacent cells indistinguishable.
    """
    _assert_docx_unpacks_small(data)
    try:
        document = docx.Document(io.BytesIO(data))
        lines: list[str] = []
        for child in document.element.body.iterchildren():
            if child.tag == qn("w:p"):
                lines.append(Paragraph(child, document).text)
            elif child.tag == qn("w:tbl"):
                for row in Table(child, document).rows:
                    lines.append(" | ".join(cell.text for cell in row.cells))
        return "\n".join(lines)
    except Exception as exc:
        raise _unreadable("Word document", exc) from exc


def _assert_docx_unpacks_small(data: bytes) -> None:
    """Refuse a document whose zip members would unpack to far more than was uploaded.

    python-docx inflates every part and builds a tree for the XML ones, so the cost is set by the
    unpacked size, not the upload. The sizes come from the zip directory; the zip reader stops at
    the declared size, so a member cannot unpack to more than it declares.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
    except (zipfile.BadZipFile, ValueError, OSError):
        return  # not a zip at all: python-docx reports that as an unreadable document
    total = sum(m.file_size for m in members)
    xml = sum(m.file_size for m in members if m.filename.lower().endswith((".xml", ".rels")))
    if len(members) > MAX_DOCX_PARTS or xml > MAX_DOCX_XML_BYTES or total > max(4 * MAX_BYTES, 2 * len(data)):
        raise OvertoneError(
            "That Word document is too large when unpacked.", status=413, code="too_large"
        )


def _decode_text(data: bytes, charset: str | None) -> str:
    """Decode with the declared charset (UTF-8 if missing or unknown). A leading BOM is dropped."""
    encoding = "utf-8"
    if charset:
        label = charset.strip().lower()
        label = _CHARSET_ALIASES.get(label, label)
        try:
            if codecs.lookup(label).name in _TEXT_CODECS:
                encoding = label
        except LookupError:
            pass
    text = data.decode(encoding, errors="replace")
    return text[1:] if text.startswith("﻿") else text


@dataclass
class _Extracted:
    format: Format
    text: str
    mime_type: str
    # Only set for HTML: the <title> element and og:title meta, if any.
    html_title: str | None = None
    og_title: str | None = None


async def _from_bytes(
    *, name: str, data: bytes, mime_type: str | None, mime_first: bool, charset: str | None = None
) -> _Extracted:
    """Decide the format of some bytes and convert them to text.

    `mime_first` picks the precedence between the mime type and the name's
    extension (URLs trust the Content-Type header; uploaded files trust the filename).
    """
    fmt: Classification | None = _classify(name, mime_type, mime_first)
    if fmt == "unsupported":
        raise _unsupported(name)
    if fmt is None:
        if _is_pdf_bytes(data):
            fmt = "pdf"
        elif _looks_like_text(data):
            fmt = "text"
        else:
            raise _unsupported(name)

    if fmt == "pdf":
        return _Extracted("pdf", await asyncio.to_thread(_pdf_to_text, data), PDF_MIME)
    if fmt == "docx":
        return _Extracted("docx", await asyncio.to_thread(_docx_to_text, data), DOCX_MIME)
    if fmt == "html":
        return await asyncio.to_thread(_html_extracted, data, charset)
    return _Extracted("text", await asyncio.to_thread(_decode_text, data, charset), _text_mime_type(name, mime_type))


def _html_extracted(data: bytes, charset: str | None) -> _Extracted:
    """Decode and convert an HTML document. CPU-bound: run in a thread."""
    html = _decode_text(data, charset)
    return _Extracted("html", html_to_text(html), "text/html", html_title(html), html_og_title(html))


# ─────────────────────────────── HTML ────────────────────────────────

_NAMED_OR_NUMERIC_ENTITY = re.compile(r"&(#[xX][0-9a-fA-F]+|#[0-9]+|[a-zA-Z][a-zA-Z0-9]*);")
_COMMENT_RE = re.compile(r"<!--[\s\S]*?(?:-->|\Z)")
_TAG_RE = re.compile(r"<[^>]*>")
_META_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_OG_TITLE_ATTR_RE = re.compile(r"(?:property|name)\s*=\s*[\"']og:title[\"']", re.IGNORECASE)
_CONTENT_ATTR_RE = re.compile(r"content\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", re.IGNORECASE)

_STRIP_TAGS = ("head", "title", "script", "style", "noscript", "svg", "template", "iframe")
_CHROME_TAGS = ("nav", "header", "footer", "aside", "form")
_BLOCK_TAGS = (
    "address|article|aside|blockquote|body|canvas|caption|dd|details|dialog|div|dl|dt|fieldset|figcaption|figure|"
    "footer|form|h[1-6]|header|hr|html|main|nav|ol|p|pre|section|summary|table|tbody|tfoot|thead|tr|ul|video|audio"
)
_BLOCK_RE = re.compile(rf"</?(?:{_BLOCK_TAGS})\b[^>]*>", re.IGNORECASE)
_BR_RE = re.compile(r"<br\b[^>]*>", re.IGNORECASE)
_LI_RE = re.compile(r"<li\b[^>]*>", re.IGNORECASE)
_DECL_RE = re.compile(r"<![^>]*>")
_TAG_ANY_RE = re.compile(r"</?[a-zA-Z][^>]*>")
_WS_RE = re.compile(r"\s+")


def decode_entities(text: str) -> str:
    """Decode HTML character references (named and numeric) that end in a semicolon."""
    return _NAMED_OR_NUMERIC_ENTITY.sub(lambda m: html_lib.unescape(m.group(0)), text)


def _collapse(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def _clean_opt(text: str | None) -> str | None:
    """Collapse whitespace; None when nothing is left."""
    if text is None:
        return None
    return _collapse(text) or None


def _strip_comments(html: str) -> str:
    return _COMMENT_RE.sub(" ", html)


# Every tag pattern here has the shape "<name[^>]*>". Run over text with many "<name" and no ">" after
# them ("<a<a<a..."), each one scans to the end of the document before failing: quadratic time, tens of
# seconds for a 200 KB page, with the server frozen meanwhile. No tag can start after the last ">", so
# the helpers below never let a pattern look past it; before it, every "<name" finds its ">".


def _tag_end(html: str) -> int:
    """Index just past the last ">": the end of the part of the document that can contain tags."""
    return html.rfind(">") + 1


def _sub_tags(pattern: re.Pattern[str], replacement: str, text: str) -> str:
    """`pattern.sub` for a tag pattern, in time linear in the text."""
    cut = _tag_end(text)
    return pattern.sub(replacement, text[:cut]) + text[cut:]


_ELEMENT_RES: dict[str, tuple[re.Pattern[str], re.Pattern[str]]] = {}


def _elements(html: str, tag: str) -> Iterator[tuple[int, int, int, int]]:
    """(start, inner start, inner end, end) of each `tag` element, left to right, without overlaps.

    An element ends at its first closing tag; one that is never closed runs to the end of the
    document (inner end == end). Linear in the size of the document.
    """
    patterns = _ELEMENT_RES.get(tag)
    if patterns is None:
        patterns = _ELEMENT_RES[tag] = (
            re.compile(rf"<{tag}\b[^>]*>", re.IGNORECASE),
            re.compile(rf"</{tag}\s*>", re.IGNORECASE),
        )
    open_re, close_re = patterns
    limit = _tag_end(html)
    pos = 0
    while pos < limit:
        opened = open_re.search(html, pos, limit)
        if opened is None:
            return
        closed = close_re.search(html, opened.end(), limit)
        if closed is None:
            yield opened.start(), opened.end(), len(html), len(html)
            return
        yield opened.start(), opened.end(), closed.start(), closed.end()
        pos = closed.end()


def _remove_elements(html: str, tags: tuple[str, ...]) -> str:
    """Remove whole elements (open tag, content, close tag; unclosed runs to the end)."""
    out = html
    for tag in tags:
        kept: list[str] = []
        last = 0
        for start, _, _, end in _elements(out, tag):
            kept += (out[last:start], " ")
            last = end
        if last:
            kept.append(out[last:])
            out = "".join(kept)
    return out


def _inner_of_all(html: str, tag: str) -> list[str]:
    """Inner HTML of every element with this tag name."""
    return [html[inner_start:inner_end] for _, inner_start, inner_end, _ in _elements(html, tag)]


def _fragment_to_text(fragment: str) -> str:
    """Body text of an HTML fragment that has already been scoped and stripped of chrome."""
    s = _WS_RE.sub(" ", fragment)  # HTML source whitespace is not meaningful
    s = _sub_tags(_BR_RE, "\n", s)
    s = _sub_tags(_LI_RE, "\n- ", s)
    s = _sub_tags(_BLOCK_RE, "\n", s)
    s = _sub_tags(_DECL_RE, "", s)
    s = _sub_tags(_TAG_ANY_RE, "", s)
    s = decode_entities(s)
    lines = [line.strip() for line in s.split("\n")]
    return normalize_text("\n".join(lines))


def html_to_text(html: str) -> str:
    """Convert an HTML document to plain text. Good enough for an LLM, not a readability engine."""
    doc = _remove_elements(_strip_comments(html), _STRIP_TAGS)
    mains = _inner_of_all(doc, "main")
    articles = [] if mains else _inner_of_all(doc, "article")
    scoped: str | None = None
    if mains:
        scoped = mains[0]
    elif articles:
        scoped = "\n".join(articles)
    if scoped is not None:
        text = _fragment_to_text(scoped)
        if text:
            return text
    return _fragment_to_text(_remove_elements(doc, _CHROME_TAGS))


def html_title(html: str) -> str | None:
    """The document's <title>, entity-decoded, or None."""
    doc = _strip_comments(html)
    for _, inner_start, inner_end, end in _elements(doc, "title"):
        if end == inner_end:
            return None  # never closed: not a title
        return _collapse(decode_entities(_sub_tags(_TAG_RE, "", doc[inner_start:inner_end]))) or None
    return None


def html_og_title(html: str) -> str | None:
    """The og:title meta content, or None."""
    for tag in _META_RE.findall(html, 0, _tag_end(html)):
        if not _OG_TITLE_ATTR_RE.search(tag):
            continue
        c = _CONTENT_ATTR_RE.search(tag)
        raw = ""
        if c:
            raw = c.group(1) if c.group(1) is not None else (c.group(2) or "")
        value = _collapse(decode_entities(raw))
        if value:
            return value
    return None


# ───────────────────────────── Normalization ─────────────────────────

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_INDENT_RE = re.compile(r"[ \t]*")
_INLINE_WS_RE = re.compile(r"[^\S\n]+")
_BLANK_RUN_RE = re.compile(r"\n{4,}")


def normalize_text(text: str) -> str:
    """Normalize extracted text: LF line endings, no control characters (except tab and newline),
    runs of spaces inside a line collapsed, trailing spaces dropped, 4+ newlines collapsed to 3,
    and trimmed. Leading indentation is kept, so code stays readable. Never truncates."""
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _CONTROL_RE.sub("", cleaned)
    if cleaned.startswith("﻿"):
        cleaned = cleaned[1:]
    lines: list[str] = []
    for line in cleaned.split("\n"):
        indent = _INDENT_RE.match(line)
        indent_text = indent.group(0) if indent else ""
        rest = _INLINE_WS_RE.sub(" ", line[len(indent_text) :]).rstrip()
        lines.append(indent_text + rest if rest else "")
    return _BLANK_RUN_RE.sub("\n\n\n", "\n".join(lines)).strip()


def _title_from_text(text: str) -> str | None:
    for raw in text.split("\n"):
        line = re.sub(r"^#{1,6}\s*", "", raw.strip()).strip()
        if not line:
            continue
        return line if len(line) <= 80 else f"{line[:80].rstrip()}…"
    return None


def _title_from_filename(filename: str) -> str:
    base = _basename(filename)
    stem = re.sub(r"\.[^.]+\Z", "", base)
    title = _collapse(re.sub(r"[_-]+", " ", stem))
    return title or base or "Untitled"


# ───────────────────────────── URL fetching ──────────────────────────

_IPV4_LIKE = re.compile(r"(0x[0-9a-f]+|[0-9]+)(\.(0x[0-9a-f]+|[0-9]+)){0,3}", re.IGNORECASE)
_NAT64_PREFIX = bytes.fromhex("0064ff9b") + bytes(8)  # 64:ff9b::/96


def _is_blocked_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for every address that is not on the public internet: loopback, private, link-local,
    carrier-grade NAT (100.64.0.0/10, where some clouds put their metadata service and VPNs their
    peers), site-local, multicast, unspecified and reserved addresses.

    IPv4-mapped, IPv4-compatible and NAT64 IPv6 addresses are judged by the IPv4 address they embed.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return _is_blocked_address(ip.ipv4_mapped)
        prefix = ip.packed[:12]
        if prefix == bytes(12) or prefix == _NAT64_PREFIX:
            return _is_blocked_address(ipaddress.IPv4Address(ip.packed[12:]))
    return (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
        or (isinstance(ip, ipaddress.IPv6Address) and ip.is_site_local)
    )


def _parse_literal_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """An IP literal (including inet_aton shorthand such as "127.1"), or None for a hostname."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    if _IPV4_LIKE.fullmatch(host):
        try:
            return ipaddress.IPv4Address(socket.inet_aton(host))
        except OSError:
            return None
    return None


def _parse_url(raw: str) -> SplitResult:
    """Split a URL string; raises bad_url when it is not a URL at all."""
    try:
        parts = urlsplit(raw)
        _ = parts.port  # validates the port number
    except ValueError as exc:
        raise OvertoneError(f"Not a valid URL: {raw[:200]}", status=400, code="bad_url") from exc
    if not parts.scheme or (parts.scheme in ("http", "https") and not parts.hostname):
        raise OvertoneError(f"Not a valid URL: {raw[:200]}", status=400, code="bad_url")
    return parts


async def _resolve_host(host: str, port: int) -> list[str]:
    """Every address the hostname resolves to. Raises OSError on a DNS failure."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def _fetch_failed(message: str) -> OvertoneError:
    return OvertoneError(message, status=502, code="fetch_failed")


def _too_large() -> OvertoneError:
    return OvertoneError(f"That file is larger than {MAX_BYTES // (1024 * 1024)} MB", status=413, code="too_large")


def _uses_proxy(parts: SplitResult) -> bool:
    """True when the environment sends this URL through a proxy (HTTP(S)_PROXY, minus NO_PROXY)."""
    proxies = urllib.request.getproxies()
    if not (proxies.get(parts.scheme) or proxies.get("all")):
        return False
    try:
        return not urllib.request.proxy_bypass(parts.hostname or "")
    except Exception:
        return True


async def _assert_fetchable(raw: str) -> list[str]:
    """Scheme and SSRF checks for one hop (every redirect target is checked again).

    Returns the addresses the connection may be made to, all of them checked. The caller must
    connect to one of these and not look the name up again: DNS may answer differently the second
    time (a public address for the check, 127.0.0.1 for the connection). An empty list means
    "connect by name": private URLs are allowed, or a proxy does the lookup, so pinning cannot apply.
    """
    parts = _parse_url(raw)
    if parts.scheme not in ("http", "https"):
        raise OvertoneError(
            f"Only http and https URLs can be added (got {parts.scheme}:)", status=400, code="bad_url"
        )
    if os.environ.get("OVERTONE_ALLOW_PRIVATE_URLS") == "1":
        return []

    host = (parts.hostname or "").rstrip(".")
    if not host:
        raise OvertoneError(f"Not a valid URL: {raw[:200]}", status=400, code="bad_url")

    def blocked() -> OvertoneError:
        return OvertoneError(f"That address is not allowed: {host}", status=400, code="blocked_url")

    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise blocked()

    literal = _parse_literal_ip(host)
    if literal is not None:
        if _is_blocked_address(literal):
            raise blocked()
        return [] if _uses_proxy(parts) else [str(literal)]

    # A public-looking name may still resolve to a private address: check every answer.
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        addresses = await _resolve_host(host, port)
    except OSError as exc:
        raise _fetch_failed(f"Could not resolve {host}: {exc.strerror or exc}") from exc
    checked: list[str] = []
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError:
            raise blocked() from None  # fail closed on anything unparseable
        if _is_blocked_address(ip):
            raise blocked()
        if str(ip) not in checked:
            checked.append(str(ip))
    if not checked:
        raise _fetch_failed(f"Could not resolve {host}: no addresses")
    return [] if _uses_proxy(parts) else checked


_MAX_PINNED_ATTEMPTS = 3


@asynccontextmanager
async def _open_pinned(client: httpx.AsyncClient, url: str, addresses: list[str]) -> AsyncIterator[httpx.Response]:
    """GET `url`, connecting to one of `addresses` instead of resolving the host name again.

    The request is built for the real URL, so the Host header, TLS server name and certificate
    check all still use the name; only the socket goes to the checked address. The next address
    is tried when one cannot be connected to.
    """
    targets: list[str | None] = [*addresses[:_MAX_PINNED_ATTEMPTS]] if addresses else [None]
    response: httpx.Response | None = None
    for attempt, address in enumerate(targets):
        request = client.build_request("GET", url)
        if address is not None:
            request.extensions = {**request.extensions, "sni_hostname": request.url.raw_host.decode("ascii")}
            request.url = request.url.copy_with(host=address)
        try:
            response = await client.send(request, stream=True)
            break
        except (httpx.ConnectError, httpx.ConnectTimeout):
            if attempt == len(targets) - 1:
                raise
    assert response is not None
    try:
        yield response
    finally:
        await response.aclose()


@dataclass
class _Fetched:
    url: str
    content_type: str | None
    data: bytes


def _declared_length(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


async def _read_capped(res: httpx.Response, url: str) -> bytes:
    """Read the body, failing as soon as it passes the size cap."""
    chunks: list[bytes] = []
    total = 0
    try:
        async for chunk in res.aiter_bytes():
            total += len(chunk)
            if total > MAX_BYTES:
                raise _too_large()
            chunks.append(chunk)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise _fetch_failed(f"Could not download {url}: {_describe(exc)}") from exc
    return b"".join(chunks)


async def _fetch_guarded(start: str) -> _Fetched:
    """Fetch with manual redirects (each hop re-checked) and a size cap."""
    current = start
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(FETCH_TIMEOUT_S),
        headers={"User-Agent": USER_AGENT, "Accept": ACCEPT},
    ) as client:
        for hop in itertools.count():
            addresses = await _assert_fetchable(current)
            try:
                async with _open_pinned(client, current, addresses) as res:
                    if res.status_code in REDIRECT_STATUSES:
                        location = res.headers.get("location")
                        if not location:
                            raise _fetch_failed(
                                f"Could not fetch {current}: HTTP {res.status_code} without a Location header"
                            )
                        if hop >= MAX_REDIRECTS:
                            raise _fetch_failed(f"Could not fetch {start}: too many redirects")
                        try:
                            current = urljoin(current, location)
                        except ValueError as exc:
                            raise _fetch_failed(f"Could not fetch {current}: invalid redirect target") from exc
                        continue

                    if not res.is_success:
                        raise _fetch_failed(f"Could not fetch {current}: HTTP {res.status_code}")

                    declared = _declared_length(res.headers.get("content-length"))
                    if declared is not None and declared > MAX_BYTES:
                        raise _too_large()

                    data = await _read_capped(res, current)
                    return _Fetched(current, res.headers.get("content-type"), data)
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                raise _fetch_failed(f"Could not fetch {current}: {_describe(exc)}") from exc
    raise AssertionError("unreachable")  # itertools.count() never ends


async def _fetch_with_budget(start: str) -> _Fetched:
    """One overall time budget across every hop and the body."""
    try:
        async with asyncio.timeout(FETCH_TIMEOUT_S):
            return await _fetch_guarded(start)
    except TimeoutError as exc:
        raise _fetch_failed(f"Could not fetch {start}: timed out after {FETCH_TIMEOUT_S:g} seconds") from exc


def _charset_of(content_type: str | None) -> str | None:
    if not content_type:
        return None
    m = re.search(r"charset\s*=\s*\"?([^\";\s]+)", content_type, re.IGNORECASE)
    return m.group(1) if m else None


def _safe_decode(s: str) -> str:
    try:
        return unquote(s, errors="strict")
    except UnicodeDecodeError:
        return s


def _host_and_path(parts: SplitResult) -> str:
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    path = parts.path[:-1] if parts.path.endswith("/") else parts.path
    return host + path


# ───────────────────────────── Entry points ──────────────────────────


def _extract_pasted_text(input: TextInput) -> ExtractedSource:
    text = normalize_text(str(input.text or ""))
    title = _clean_opt(input.title) or _title_from_text(text) or "Pasted text"
    return ExtractedSource(kind="text", title=title, text=text)


async def _extract_file(input: FileInput) -> ExtractedSource:
    filename = str(input.filename or "")
    data = bytes(input.data)
    extracted = await _from_bytes(name=filename, data=data, mime_type=input.mime_type, mime_first=False)
    title = (
        _clean_opt(input.title)
        or (extracted.html_title if extracted.format == "html" else None)
        or _title_from_filename(filename)
    )
    return ExtractedSource(
        kind="file",
        title=title,
        text=await asyncio.to_thread(normalize_text, extracted.text),
        origin=filename,
        mime_type=extracted.mime_type,
    )


async def _extract_url(input: UrlInput) -> ExtractedSource:
    raw = str(input.url or "").strip()
    fetched = await _fetch_with_budget(raw)
    parts = urlsplit(fetched.url)
    name = _safe_decode(_basename(parts.path)) or (parts.hostname or "")
    extracted = await _from_bytes(
        name=name,
        data=fetched.data,
        mime_type=fetched.content_type,
        mime_first=True,
        charset=_charset_of(fetched.content_type),
    )
    html_derived: str | None = None
    if extracted.format == "html":
        html_derived = extracted.html_title or extracted.og_title
    title = _clean_opt(input.title) or html_derived or _host_and_path(parts)
    return ExtractedSource(
        kind="url",
        title=title,
        text=await asyncio.to_thread(normalize_text, extracted.text),
        origin=urlunsplit(parts),
        mime_type=extracted.mime_type,
    )


async def extract_source(input: SourceInput) -> ExtractedSource:
    """Turn pasted text, a URL or an uploaded file into plain text plus a title."""
    if isinstance(input, TextInput):
        return _extract_pasted_text(input)
    if isinstance(input, FileInput):
        return await _extract_file(input)
    if isinstance(input, UrlInput):
        return await _extract_url(input)
    raise OvertoneError(f"Unknown source type: {type(input).__name__}", status=400, code="bad_source")
