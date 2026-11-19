"""REST API over the Overtone engine: the Python port of `packages/core/src/http.ts`.

The routes, status codes and JSON shapes match the TypeScript handler, so the
existing web UI and TypeScript client work against either server.

    from overtone.api import create_app, serve

    app = create_app(api_key="s3cret", cors=["https://app.example"])
    serve(port=8787)                    # or: uvicorn overtone.api:create_app --factory --port 8787
    parent.mount("/overtone", app)      # or mount it under any ASGI app

Routes live at the root of the returned app, so it can be served directly or mounted
under any path. The engine is available as `app.state.engine`; when none is passed it is
created on the first request.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import math
import os
import re
import threading
import unicodedata
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, MutableMapping
from typing import Any, TypeVar
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from pydantic import ValidationError
from starlette.datastructures import Headers, UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .engine import Engine, create_engine
from .errors import OvertoneError
from .models import (
    ARTIFACT_LABEL,
    ArtifactRequest,
    FileInput,
    LiveDelegateRequest,
    LiveSessionRequest,
    Model,
    SourceInput,
    Source,
    TextInput,
    UrlInput,
)

log = logging.getLogger("overtone.api")

DEFAULT_MAX_UPLOAD = 50 * 1024 * 1024
# One request may carry several uploads; this caps them together (and every JSON body).
_REQUEST_BYTES_PER_UPLOAD = 4
# Multipart parts per request. Starlette's defaults are 1000 each, and text parts are held in memory.
_MAX_FORM_FILES = 50
_MAX_FORM_FIELDS = 100
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_EXPOSE_HEADERS = "Content-Range, Content-Length"
_PREFLIGHT_HEADERS = {
    "Access-Control-Allow-Methods": "GET,POST,PATCH,DELETE,OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Access-Control-Max-Age": "86400",
}
_NOTEBOOK_FIELDS = ("title", "description", "icon")
_AUDIO_PATH = re.compile(r"/artifacts/[^/]+/audio")

_MISSING: Any = object()  # "key absent" for optional JSON bodies (JSON null is a value)
_M = TypeVar("_M", bound=Model)
Handler = Callable[[Request], Awaitable[Response]]


class _Unsatisfiable:
    """Marker for a Range header that cannot be satisfied (416)."""


_UNSATISFIABLE: Any = _Unsatisfiable()


# ───────────────────────────── response helpers ─────────────────────────────


def _json(data: Any, status: int = 200, headers: Mapping[str, str] | None = None) -> JSONResponse:
    merged = {"Content-Type": "application/json; charset=utf-8", **(headers or {})}
    return JSONResponse(data, status_code=status, headers=merged)


def _json_error(
    status: int, message: str, code: str, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    return _json({"error": {"message": message, "code": code}}, status, headers)


def _no_content() -> Response:
    return Response(status_code=204)


def _bad(message: str, code: str = "bad_request") -> OvertoneError:
    return OvertoneError(message, status=400, code=code)


def _valid_status(status: int) -> int:
    return status if isinstance(status, int) and 400 <= status <= 599 else 500


def _readable(exc: ValidationError) -> str:
    errors = exc.errors()
    first = errors[0] if errors else {}
    loc = ".".join(str(part) for part in first.get("loc", ()))
    message = str(first.get("msg", "invalid value"))
    return f"{loc}: {message}" if loc else message


def _parse(model: type[_M], data: Any) -> _M:
    """Validate a request body into a model. Failures become a 400 with a readable message."""
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise _bad(_readable(exc)) from None


def _error_response(exc: BaseException) -> JSONResponse:
    """The one place where an exception becomes an HTTP error."""
    if isinstance(exc, OvertoneError):
        return _json_error(_valid_status(exc.status), exc.message, exc.code or "bad_request")
    if isinstance(exc, ValidationError):
        return _json_error(400, _readable(exc), "bad_request")
    log.exception("Unhandled error", exc_info=exc)
    return _json_error(500, "Internal error", "internal")


async def _read_optional_json(request: Request) -> Any:
    """The parsed JSON body, `_MISSING` when the body is empty, or a 400 for malformed JSON."""
    raw = await request.body()
    if not raw.strip():
        return _MISSING
    try:
        return json.loads(raw)
    except ValueError:
        raise OvertoneError("Request body must be valid JSON.", status=400, code="bad_json") from None


def _string_fields(body: Any, keys: tuple[str, ...], nullable: bool = False) -> dict[str, str]:
    """Picks string fields from a JSON object. `null` maps to "" (clears a field) when `nullable`."""
    if body is _MISSING:
        return {}
    if not isinstance(body, dict):
        raise _bad("Expected a JSON object.")
    out: dict[str, str] = {}
    for key in keys:
        if key not in body:
            continue
        value = body[key]
        if value is None and nullable:
            out[key] = ""
            continue
        if not isinstance(value, str):
            raise _bad(f'"{key}" must be a string.')
        out[key] = value
    return out


def _format_mb(num_bytes: int) -> str:
    """Matches the TypeScript message: whole numbers print without a decimal point."""
    value = math.floor(num_bytes / (1024 * 1024) * 10 + 0.5) / 10
    return f"{int(value) if value.is_integer() else value} MB"


# ───────────────────────────── sources ─────────────────────────────


def _parse_source_input(raw: Any, where: str) -> SourceInput:
    """One JSON source. `where` is "sources" or "oneshot" (the one-shot route takes no files)."""
    if not isinstance(raw, dict):
        raise _bad("Each source must be a JSON object.")
    title = raw["title"] if isinstance(raw.get("title"), str) else None
    kind = raw.get("type")
    if kind == "text":
        if not isinstance(raw.get("text"), str):
            raise _bad('Text sources need a "text" string.')
        return TextInput(text=raw["text"], title=title)
    if kind == "url":
        url = raw.get("url")
        if not isinstance(url, str) or not url.strip():
            raise _bad('URL sources need a "url" string.')
        return UrlInput(url=url.strip(), title=title)
    if kind == "file":
        if where == "oneshot":
            raise OvertoneError(
                'POST /oneshot accepts "text" and "url" sources only.', status=400, code="unsupported_source"
            )
        raise OvertoneError(
            'File uploads cannot be sent as JSON. Use multipart/form-data with a "file" field.',
            status=400,
            code="multipart_required",
        )
    raise _bad('Each source needs type "text" or "url".')


def _parse_json_sources(body: Any) -> list[SourceInput]:
    if not isinstance(body, dict):
        raise _bad('Expected a JSON source or {"sources": [...]}.')
    if "sources" in body:
        if not isinstance(body["sources"], list):
            raise _bad('"sources" must be an array.')
        items: list[Any] = body["sources"]
    else:
        items = [body]
    if not items:
        raise OvertoneError("Provide at least one source.", status=400, code="no_sources")
    return [_parse_source_input(item, "sources") for item in items]


def _source_name(source: SourceInput) -> str:
    if isinstance(source, UrlInput):
        return source.url
    if isinstance(source, TextInput):
        return source.title if source.title is not None else source.text.strip()[:40]
    return source.filename


def _slugify(title: str) -> str:
    """ASCII slug for download filenames, same rules as the TypeScript handler."""
    decomposed = unicodedata.normalize("NFKD", title)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    slug = re.sub(r"[^a-z0-9]+", "-", stripped.lower()).strip("-")[:80]
    return slug or "overtone-audio"


def _parse_range(header: str | None, total: int) -> tuple[int, int] | None | _Unsatisfiable:
    """One `bytes=` range. None serves the whole body; the unsatisfiable marker gives a 416."""
    if not header:
        return None
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip(), re.IGNORECASE)
    if not match:
        return None
    first, last = match.groups()
    if first == "" and last == "":
        return None
    if first == "":
        suffix = int(last)
        if suffix == 0 or total == 0:
            return _UNSATISFIABLE
        return max(0, total - suffix), total - 1
    start = int(first)
    if start >= total:
        return _UNSATISFIABLE
    if last == "":
        return start, total - 1
    if int(last) < start:
        return None
    return start, min(int(last), total - 1)


# ───────────────────────────── auth, CORS and path middleware ─────────────────────────────


def _route_path(scope: Scope) -> str:
    """The path relative to the app, as Starlette routes it (strips a mount's root_path)."""
    path: str = scope["path"]
    root = scope.get("root_path", "")
    if root and path.startswith(root):
        return "" if path == root else (path[len(root) :] if path[len(root)] == "/" else path)
    return path


class _NormalizePathMiddleware:
    """Tolerates a trailing slash on every route, as the TypeScript handler does."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            route_path = _route_path(scope)
            if len(route_path) > 1 and route_path.endswith("/"):
                scope = {**scope, "path": scope["path"][:-1]}
        await self.app(scope, receive, send)


class _AuthMiddleware:
    """Bearer-token check on every request except OPTIONS and GET /health.

    `GET /artifacts/{id}/audio` also accepts `?key=` because an <audio> element cannot send headers.
    """

    def __init__(self, app: ASGIApp, *, api_key: str) -> None:
        self.app = app
        self.api_key = api_key

    def _same(self, candidate: str) -> bool:
        return hmac.compare_digest(candidate.encode("utf-8"), self.api_key.encode("utf-8"))

    def _allowed(self, scope: Scope) -> bool:
        method = scope["method"]
        path = _route_path(scope)
        if method == "OPTIONS" or (method == "GET" and path == "/health"):
            return True
        match = re.fullmatch(r"Bearer\s+(.+)", Headers(scope=scope).get("authorization", "").strip(), re.IGNORECASE)
        if match and self._same(match.group(1).strip()):
            return True
        if method == "GET" and _AUDIO_PATH.fullmatch(path):
            keys = parse_qs(scope.get("query_string", b"").decode("latin-1")).get("key")
            return bool(keys and keys[0] and self._same(keys[0]))
        return False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._allowed(scope):
            await self.app(scope, receive, send)
            return
        response = _json_error(
            401, "Missing or invalid API key.", "unauthorized", {"WWW-Authenticate": "Bearer"}
        )
        await response(scope, receive, send)


def _host_name(authority: str) -> str | None:
    """The host of a `Host` header or URL authority: lower-cased, no port, no brackets. None when malformed."""
    match = re.fullmatch(r"(?:\[([0-9a-f:.]+)\]|([a-z0-9._-]+))(?::[0-9]{1,5})?", authority.strip().lower())
    if not match:
        return None
    return (match.group(1) or match.group(2)).rstrip(".") or None


def _ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


class _LocalGuardMiddleware:
    """Keeps web pages away from an API that has no key.

    Without a key, the only thing protecting the notebooks is that the caller is the person at
    this machine. A browser breaks that assumption in two ways, and this closes both:

    - DNS rebinding: a page on `evil.example` re-points its own name at 127.0.0.1 and is then
      same-origin with this API. The browser still sends `Host: evil.example`, so a request is
      answered only when `Host` is `localhost`, an IP address (a rebinding page cannot have one
      as its name) or a name in `allowed_hosts`.
    - Cross-site requests: any page can POST here without asking (a form, or `fetch` with
      `mode: "no-cors"`). Browsers put the page's origin on those, so a request that changes
      something is answered only when `Origin` is absent (not a browser), this server itself,
      a loopback origin (the bundled web UI) or an origin on the CORS list.
    """

    def __init__(
        self, app: ASGIApp, *, allowed_hosts: list[str] | None, allowed_origins: list[str] | None
    ) -> None:
        self.app = app
        # None means "any": the caller opted out of the check.
        self.allowed_hosts = None if allowed_hosts is None else {h.strip().lower().rstrip(".") for h in allowed_hosts}
        self.allowed_origins = None if allowed_origins is None else {o.strip().lower().rstrip("/") for o in allowed_origins}

    def _host_ok(self, host_header: str | None) -> bool:
        if self.allowed_hosts is None:
            return True
        host = _host_name(host_header or "")
        if host is None:
            return False
        return host == "localhost" or _ip_literal(host) is not None or host in self.allowed_hosts

    def _origin_ok(self, origin: str, host_header: str | None) -> bool:
        if self.allowed_origins is None:
            return True
        origin = origin.strip().lower().rstrip("/")
        if origin in self.allowed_origins:
            return True
        try:
            parts = urlsplit(origin)
        except ValueError:
            return False
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return False  # includes "null" (sandboxed frames, file: pages, stripped referrers)
        if parts.netloc == (host_header or "").strip().lower():
            return True
        host = _host_name(parts.netloc)
        if host is None:
            return False
        ip = _ip_literal(host)
        return host == "localhost" or (ip is not None and ip.is_loopback)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        host_header = headers.get("host")
        if not self._host_ok(host_header):
            response = _json_error(
                403,
                "This API has no API key, so it only answers requests addressed to localhost or an IP address. "
                "Set OVERTONE_API_KEY, or allow this host name with OVERTONE_ALLOWED_HOSTS.",
                "forbidden_host",
            )
            await response(scope, receive, send)
            return
        origin = headers.get("origin")
        if scope["method"] in _UNSAFE_METHODS and origin is not None and not self._origin_ok(origin, host_header):
            response = _json_error(
                403,
                "This API has no API key, so it does not accept requests from other websites. "
                "Set OVERTONE_API_KEY, or allow this origin with the cors option.",
                "forbidden_origin",
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class _BodyLimitMiddleware:
    """Refuses request bodies over `max_bytes`, by `Content-Length` when sent and by counting otherwise."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    def _too_large(self) -> OvertoneError:
        return OvertoneError(
            f"The request is larger than the {_format_mb(self.max_bytes)} limit.", status=413, code="too_large"
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = Headers(scope=scope).get("content-length", "")
        if declared.isdigit() and int(declared) > self.max_bytes:
            raise self._too_large()
        seen = 0

        async def counted() -> MutableMapping[str, Any]:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    raise self._too_large()
            return message

        await self.app(scope, counted, send)


class _ErrorMiddleware:
    """Turns anything the app raises into the JSON error shape (500s carry no details)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def track(message: MutableMapping[str, Any]) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, track)
        except Exception as exc:
            if started:
                # Headers are already on the wire (for example mid-SSE): the only option left is to abort.
                log.exception("Response failed after it started")
                raise
            await _error_response(exc)(scope, receive, send)


class _CorsMiddleware:
    """CORS with the same semantics as the TypeScript handler. Applies to every response."""

    def __init__(self, app: ASGIApp, *, cors: bool | str | list[str]) -> None:
        self.app = app
        self.allow_any = cors is True
        self.allowed: list[str] = [] if cors is True else ([cors] if isinstance(cors, str) else list(cors))

    def _headers_for(self, origin: str | None) -> dict[str, str]:
        if self.allow_any:
            return {"Access-Control-Allow-Origin": "*", "Access-Control-Expose-Headers": _EXPOSE_HEADERS}
        headers = {"Vary": "Origin", "Access-Control-Expose-Headers": _EXPOSE_HEADERS}
        if origin and origin in self.allowed:
            headers["Access-Control-Allow-Origin"] = origin
        return headers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        cors_headers = self._headers_for(Headers(scope=scope).get("origin"))
        if scope["method"] == "OPTIONS":
            await Response(status_code=204, headers={**_PREFLIGHT_HEADERS, **cors_headers})(scope, receive, send)
            return

        ours = {name.lower().encode(): value.encode() for name, value in cors_headers.items()}

        async def with_cors(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                kept = [(k, v) for k, v in message.get("headers", []) if k.lower() not in ours]
                message = {**message, "headers": kept + list(ours.items())}
            await send(message)

        await self.app(scope, receive, with_cors)


# ───────────────────────────── app factory ─────────────────────────────


def create_app(
    engine: Engine | None = None,
    *,
    api_key: str | None = None,
    cors: bool | str | list[str] = False,
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD,
    allowed_hosts: str | list[str] | None = None,
) -> FastAPI:
    """Build the REST API as a FastAPI app.

    Args:
        engine: The engine to serve. Default: `create_engine()`, built on the first request.
        api_key: When set, every request except OPTIONS and GET /health needs `Authorization: Bearer <key>`.
            Default: the `OVERTONE_API_KEY` environment variable. Pass "" to run without a key even when it is set.
        cors: False (off), True (any origin), or an origin or list of origins to allow.
        max_upload_bytes: Largest accepted upload per file. A whole request may be four times this.
        allowed_hosts: Only used without an API key. Extra host names (beyond `localhost` and IP addresses)
            the API answers to, for example the name a reverse proxy forwards. "*" answers to any name.
            Default: the comma-separated `OVERTONE_ALLOWED_HOSTS` environment variable.
    """
    if api_key is None:
        api_key = os.environ.get("OVERTONE_API_KEY")
    api_key = api_key or None
    if allowed_hosts is None:
        allowed_hosts = [h.strip() for h in os.environ.get("OVERTONE_ALLOWED_HOSTS", "").split(",") if h.strip()]
    elif isinstance(allowed_hosts, str):
        allowed_hosts = [allowed_hosts]
    app = FastAPI(title="Overtone API", version=__version__, docs_url="/docs", openapi_url="/openapi.json")
    app.state.engine = engine
    engine_lock = threading.Lock()

    def eng() -> Engine:
        if app.state.engine is None:
            with engine_lock:
                if app.state.engine is None:
                    app.state.engine = create_engine()
        return app.state.engine

    # ── sources ──

    async def add_sources(notebook_id: str, request: Request) -> Response:
        """Adds files, URLs and text in turn. Partial failures are reported in `errors`."""
        engine_ = eng()
        await engine_.notebooks.get(notebook_id)
        # (name, input) to add, or (name, failure status/code/message) when already known to fail.
        pending: list[tuple[str, SourceInput | None, tuple[int, str, str] | None]] = []

        if request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
            try:
                # Starlette caps text fields at 1 MB by default. Pasted text has no limit in the TypeScript
                # handler, so use a generous guard that is independent of the per-file upload limit.
                form = await request.form(
                    max_files=_MAX_FORM_FILES,
                    max_fields=_MAX_FORM_FIELDS,
                    max_part_size=max(DEFAULT_MAX_UPLOAD, max_upload_bytes),
                )
            except OvertoneError:
                raise  # the request-size limit
            except Exception:
                raise _bad("Malformed multipart body.", "bad_multipart") from None
            # `title` fields pair with `text` fields by position.
            titles = [value.strip() if isinstance(value, str) else "" for key, value in form.multi_items() if key == "title"]
            text_index = 0
            for key, value in form.multi_items():
                if key in ("file", "files") and isinstance(value, UploadFile):
                    name = value.filename or "upload"
                    if value.size is not None and value.size > max_upload_bytes:
                        pending.append((name, None, (413, "too_large", f'"{name}" is larger than the {_format_mb(max_upload_bytes)} upload limit.')))
                        continue
                    data = await value.read(max_upload_bytes + 1)
                    if len(data) > max_upload_bytes:
                        pending.append((name, None, (413, "too_large", f'"{name}" is larger than the {_format_mb(max_upload_bytes)} upload limit.')))
                    else:
                        pending.append((name, FileInput(filename=name, data=data, mime_type=value.content_type or None), None))
                elif key == "url" and isinstance(value, str) and value.strip():
                    url = value.strip()
                    pending.append((url, UrlInput(url=url), None))
                elif key == "text" and isinstance(value, str) and value.strip():
                    title = (titles[text_index] if text_index < len(titles) else "") or None
                    text_index += 1
                    pending.append((title or "Pasted text", TextInput(text=value, title=title), None))
        else:
            body = await _read_optional_json(request)
            if body is _MISSING:
                raise OvertoneError("Provide at least one source.", status=400, code="no_sources")
            for item in _parse_json_sources(body):
                pending.append((_source_name(item), item, None))

        if not pending:
            raise OvertoneError("Provide at least one file, url or text field.", status=400, code="no_sources")

        # Sequential on purpose: ingestion and storage are per-source and cheap to order.
        sources: list[Source] = []
        failures: list[dict[str, Any]] = []
        for name, source_input, failure in pending:
            if failure is not None:
                status, code, message = failure
                failures.append({"name": name, "error": message, "code": code, "status": status})
                continue
            assert source_input is not None
            try:
                sources.append(await engine_.sources.add(notebook_id, source_input))
            except Exception as exc:
                status, code, message = _describe_failure(exc)
                failures.append({"name": name, "error": message, "code": code, "status": status})

        errors = [{"name": f["name"], "error": f["error"], "code": f["code"]} for f in failures]
        if sources:
            return _json({"sources": [s.to_json() for s in sources], "errors": errors}, 201)
        first = failures[0]
        return _json(
            {"error": {"message": first["error"], "code": first["code"]}, "sources": [], "errors": errors},
            first["status"],
        )

    async def source_list(request: Request) -> Response:
        sources = await eng().sources.list(request.path_params["id"])
        return _json({"sources": [s.to_json() for s in sources]})

    async def source_get(request: Request) -> Response:
        return _json({"source": (await eng().sources.get(request.path_params["id"])).to_json()})

    async def source_delete(request: Request) -> Response:
        await eng().sources.delete(request.path_params["id"])
        return _no_content()

    async def source_text(request: Request) -> Response:
        text = await eng().sources.get_text(request.path_params["id"])
        return Response(content=text.encode("utf-8"), media_type="text/plain; charset=utf-8")

    async def source_add(request: Request) -> Response:
        return await add_sources(request.path_params["id"], request)

    # ── notebooks ──

    async def notebook_list(request: Request) -> Response:
        return _json({"notebooks": [n.to_json() for n in await eng().notebooks.list()]})

    async def notebook_create(request: Request) -> Response:
        fields = _string_fields(await _read_optional_json(request), _NOTEBOOK_FIELDS)
        notebook = await eng().notebooks.create(**fields)
        return _json({"notebook": notebook.to_json()}, 201)

    async def notebook_get(request: Request) -> Response:
        return _json({"notebook": (await eng().notebooks.get(request.path_params["id"])).to_json()})

    async def notebook_update(request: Request) -> Response:
        fields = _string_fields(await _read_optional_json(request), _NOTEBOOK_FIELDS, nullable=True)
        notebook = await eng().notebooks.update(request.path_params["id"], **fields)
        return _json({"notebook": notebook.to_json()})

    async def notebook_delete(request: Request) -> Response:
        await eng().notebooks.delete(request.path_params["id"])
        return _no_content()

    async def notebook_guide(request: Request) -> Response:
        refresh = request.query_params.get("refresh") in ("1", "true")
        guide = await eng().notebooks.guide(request.path_params["id"], refresh=refresh)
        return _json({"guide": guide.to_json()})

    # ── messages and chat ──

    async def messages_list(request: Request) -> Response:
        messages = await eng().chat.history(request.path_params["id"])
        return _json({"messages": [m.to_json() for m in messages]})

    async def messages_clear(request: Request) -> Response:
        await eng().chat.clear(request.path_params["id"])
        return _no_content()

    async def chat(request: Request) -> Response:
        body = await _read_optional_json(request)
        if not isinstance(body, dict):
            raise _bad("Expected a JSON body with a message.")
        message = body.get("message")
        if not isinstance(message, str) or not message.strip():
            raise OvertoneError("Message is empty.", status=400, code="empty_message")
        source_ids: list[str] | None = None
        if "sourceIds" in body:
            ids = body["sourceIds"]
            if not (isinstance(ids, list) and all(isinstance(s, str) for s in ids)):
                raise _bad('"sourceIds" must be an array of source ids.')
            source_ids = ids
        notebook_id = request.path_params["id"]
        engine_ = eng()
        await engine_.notebooks.get(notebook_id)  # a missing notebook is a plain 404, not a stream
        if body.get("stream") is False:
            reply = await engine_.chat.send(notebook_id, message, source_ids=source_ids)
            return _json({"message": reply.to_json()})
        events = engine_.chat.stream(notebook_id, message, source_ids=source_ids)
        return StreamingResponse(
            _sse(events),
            media_type="text/event-stream; charset=utf-8",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    # ── artifacts ──

    async def artifact_list(request: Request) -> Response:
        artifacts = await eng().artifacts.list(request.path_params["id"])
        return _json({"artifacts": [a.to_json() for a in artifacts]})

    async def artifact_create(request: Request) -> Response:
        body = await _read_optional_json(request)
        if not isinstance(body, dict) or not isinstance(body.get("kind"), str):
            raise _bad('Expected an artifact request such as {"kind": "summary"}.')
        engine_ = eng()
        notebook_id = request.path_params["id"]
        await engine_.notebooks.get(notebook_id)
        if body["kind"] not in ARTIFACT_LABEL:
            raise OvertoneError("Unknown artifact kind.", status=400, code="bad_kind")
        artifact = await engine_.artifacts.create(notebook_id, _parse(ArtifactRequest, body))
        return _json({"artifact": artifact.to_json()}, 202)

    async def artifact_get(request: Request) -> Response:
        return _json({"artifact": (await eng().artifacts.get(request.path_params["id"])).to_json()})

    async def artifact_delete(request: Request) -> Response:
        await eng().artifacts.delete(request.path_params["id"])
        return _no_content()

    async def artifact_audio(request: Request) -> Response:
        engine_ = eng()
        artifact_id = request.path_params["id"]
        found = await engine_.artifacts.get_audio(artifact_id)
        if found is None:
            return _json_error(404, "This artifact has no audio.", "no_audio")
        data, mime_type = found
        total = len(data)
        headers = {
            "Content-Type": mime_type,
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, max-age=3600",
        }
        if request.query_params.get("download") in ("1", "true"):
            artifact = await engine_.artifacts.get(artifact_id)
            headers["Content-Disposition"] = f'attachment; filename="{_slugify(artifact.title)}.mp3"'

        window = _parse_range(request.headers.get("range"), total)
        if window is _UNSATISFIABLE:
            return Response(
                status_code=416, headers={"Accept-Ranges": "bytes", "Content-Range": f"bytes */{total}"}
            )
        if window is not None:
            start, end = window
            headers["Content-Range"] = f"bytes {start}-{end}/{total}"
            headers["Content-Length"] = str(end - start + 1)
            return Response(content=data[start : end + 1], status_code=206, headers=headers)
        headers["Content-Length"] = str(total)
        return Response(content=data, status_code=200, headers=headers)

    # ── one-shot ──

    async def oneshot(request: Request) -> Response:
        body = await _read_optional_json(request)
        if not isinstance(body, dict):
            raise _bad('Expected {"sources": [...], "request"?, "title"?}.')
        raw_sources = body.get("sources")
        if not isinstance(raw_sources, list) or not raw_sources:
            raise OvertoneError("Provide at least one source.", status=400, code="no_sources")
        sources = [_parse_source_input(item, "oneshot") for item in raw_sources]
        if "request" in body and not isinstance(body["request"], dict):
            raise _bad('"request" must be an object.')
        if "title" in body and not isinstance(body["title"], str):
            raise _bad('"title" must be a string.')
        artifact_request = _parse(ArtifactRequest, body["request"]) if "request" in body else None
        result = await eng().one_shot(sources, artifact_request, title=body.get("title"))
        # Audio bytes are omitted; clients fetch GET /artifacts/{id}/audio.
        return _json(
            {
                "notebook": result.notebook.to_json(),
                "sources": [s.to_json() for s in result.sources],
                "artifact": result.artifact.to_json(),
            }
        )

    # ── live voice ──

    async def live_session(request: Request) -> Response:
        body = await _read_optional_json(request)
        if not isinstance(body, dict):
            raise _bad("Expected a JSON body with an SDP offer.")
        session = await eng().live.create_session(request.path_params["id"], _parse(LiveSessionRequest, body))
        return _json({"session": session.to_json()}, 201)

    async def live_delegate(request: Request) -> Response:
        body = await _read_optional_json(request)
        if not isinstance(body, dict):
            raise _bad("Expected a JSON body with a transcript.")
        result = await eng().live.delegate(request.path_params["id"], _parse(LiveDelegateRequest, body))
        return _json(result.to_json())

    async def health(request: Request) -> Response:
        return _json(eng().capabilities().to_json())

    # ── routing ──

    def route(path: str, **handlers: Handler) -> None:
        """One route per (path, method), then a hidden catch-all that answers any other method with 405 and a full `Allow`."""
        table: dict[str, Handler] = {method.upper(): handler for method, handler in handlers.items()}
        for method, handler in table.items():
            app.add_api_route(path, _endpoint(handler), methods=[method], response_model=None)

        allow = ", ".join(table)

        async def method_not_allowed(request: Request) -> Response:
            return _json_error(
                405,
                f"Method {request.method} is not allowed here.",
                "method_not_allowed",
                {"Allow": allow},
            )

        app.add_api_route(
            path,
            method_not_allowed,
            methods=["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT", "TRACE"],
            include_in_schema=False,
            response_model=None,
        )

    route("/health", get=health)
    route("/notebooks", get=notebook_list, post=notebook_create)
    route("/notebooks/{id}", get=notebook_get, patch=notebook_update, delete=notebook_delete)
    route("/notebooks/{id}/guide", get=notebook_guide)
    route("/notebooks/{id}/sources", get=source_list, post=source_add)
    route("/sources/{id}", get=source_get, delete=source_delete)
    route("/sources/{id}/text", get=source_text)
    route("/notebooks/{id}/messages", get=messages_list, delete=messages_clear)
    route("/notebooks/{id}/chat", post=chat)
    route("/notebooks/{id}/artifacts", get=artifact_list, post=artifact_create)
    route("/artifacts/{id}", get=artifact_get, delete=artifact_delete)
    route("/artifacts/{id}/audio", get=artifact_audio)
    route("/oneshot", post=oneshot)
    route("/notebooks/{id}/live/sessions", post=live_session)
    route("/notebooks/{id}/live/delegate", post=live_delegate)

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(request: Request, exc: StarletteHTTPException) -> Response:
        if exc.status_code == 404:
            return _json_error(404, "Not found.", "not_found")
        if exc.status_code == 405:
            return _json_error(405, f"Method {request.method} is not allowed here.", "method_not_allowed", exc.headers)
        return _json_error(exc.status_code, str(exc.detail), "http_error")

    # Middleware: the last one added is the outermost. Order outside-in: CORS, path, errors, body limit, auth.
    # Without a key the local guard takes the place of auth.
    if api_key:
        app.add_middleware(_AuthMiddleware, api_key=api_key)
    else:
        any_origin = cors is True
        if any_origin:
            log.warning("cors=True without an API key: any website can read and change every notebook.")
        origins = [] if cors is False or cors is None or cors is True else ([cors] if isinstance(cors, str) else list(cors))
        app.add_middleware(
            _LocalGuardMiddleware,
            allowed_hosts=None if "*" in allowed_hosts else allowed_hosts,
            allowed_origins=None if any_origin else origins,
        )
    app.add_middleware(_BodyLimitMiddleware, max_bytes=_REQUEST_BYTES_PER_UPLOAD * max(DEFAULT_MAX_UPLOAD, max_upload_bytes))
    app.add_middleware(_ErrorMiddleware)
    app.add_middleware(_NormalizePathMiddleware)
    if cors is not False and cors is not None:
        app.add_middleware(_CorsMiddleware, cors=cors)
    return app


def _endpoint(handler: Handler) -> Callable[[Request], Awaitable[Response]]:
    """Adapts a handler for FastAPI. Each route has one method, so each gets its own operation id."""

    async def endpoint(request: Request) -> Response:
        return await handler(request)

    return endpoint


def _describe_failure(exc: BaseException) -> tuple[int, str, str]:
    """(status, code, message) for one source that could not be added. Unknown errors are not leaked."""
    if isinstance(exc, OvertoneError):
        return _valid_status(exc.status), exc.code or "bad_request", exc.message
    log.exception("Source could not be added", exc_info=exc)
    return 500, "internal", "Could not process this source."


async def _sse(events: AsyncIterator[dict[str, Any]]) -> AsyncIterator[bytes]:
    """Frames engine events as server-sent events. Closing the stream closes the engine generator."""
    try:
        async for event in events:
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8", "replace")
    finally:
        close = getattr(events, "aclose", None)
        if close is not None:
            await close()


def serve(
    engine: Engine | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    **app_options: Any,
) -> None:
    """Run the API with uvicorn (blocking)."""
    import uvicorn

    uvicorn.run(create_app(engine, **app_options), host=host, port=port)
