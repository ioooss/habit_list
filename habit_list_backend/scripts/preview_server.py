"""同源手机预览网关。

本地后端继续只监听 127.0.0.1:8780；这个小网关把 app.html 和 design/
暴露到局域网，并把 /api、/health、/ready 代理回本机后端。网关在服务端
注入本地预览 token，浏览器和手机端不需要把 token 放进 URL 或页面源码。

它只用于开发机局域网预览，不是生产反向代理，也不应绑定公网地址。
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from app.core.config import get_settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_HTML = PROJECT_ROOT / "app.html"
DESIGN_ROOT = PROJECT_ROOT / "design"
BACKEND_URL = os.getenv("INNER_TERRAIN_BACKEND_URL", "http://127.0.0.1:8780").rstrip("/")
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def _preview_token() -> str:
    explicit = os.getenv("INNER_TERRAIN_PREVIEW_TOKEN", "").strip()
    if explicit:
        return explicit
    settings = get_settings()
    if settings.auth_mode == "legacy":
        return settings.api_auth_token
    raise RuntimeError(
        "手机预览需要 legacy 本地令牌；请设置 INNER_TERRAIN_PREVIEW_TOKEN，"
        "或在本地预览环境使用 AUTH_MODE=legacy"
    )


def _target_url(request: Request) -> str:
    parsed = urlsplit(str(request.url))
    path = parsed.path or "/"
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{BACKEND_URL}{path}{query}"


def _forward_headers(request: Request, token: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in request.headers.items():
        if key.lower() in _HOP_BY_HOP or key.lower() in {"host", "content-length", "authorization"}:
            continue
        headers[key] = value
    headers["Authorization"] = f"Bearer {token}"
    return headers


def _response_headers(headers: httpx.Headers) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in _HOP_BY_HOP and key.lower() not in {"content-length", "content-encoding"}
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.client = httpx.AsyncClient(
        timeout=httpx.Timeout(None, connect=10.0),
        follow_redirects=False,
    )
    app.state.token = _preview_token()
    try:
        yield
    finally:
        await app.state.client.aclose()


app = FastAPI(title="Inner Terrain local phone preview", lifespan=lifespan)
if DESIGN_ROOT.is_dir():
    app.mount("/design", StaticFiles(directory=DESIGN_ROOT), name="design")


@app.get("/", include_in_schema=False)
@app.get("/app.html", include_in_schema=False)
async def index() -> HTMLResponse:
    """Serve the app with same-origin API configuration for phone previews.

    The gateway owns authentication and proxies ``/api`` back to the local
    backend.  Leaving the browser's default ``localhost:8780`` target in
    place makes a phone call its own localhost instead of this computer.
    Keep the injected config token-free; ``_forward_headers`` adds the local
    token server-side for every proxied request.
    """

    html = APP_HTML.read_text(encoding="utf-8")
    runtime_config = (
        '<script>window.__INNER_TERRAIN_CONFIG__={'
        'backendBaseUrl:window.location.origin,apiAuthToken:""};</script>'
    )
    return HTMLResponse(
        html.replace("</head>", f"{runtime_config}</head>", 1),
        media_type="text/html",
    )


async def _proxy(request: Request) -> Response:
    client: httpx.AsyncClient = request.app.state.client
    body = await request.body()
    upstream_request = client.build_request(
        request.method,
        _target_url(request),
        headers=_forward_headers(request, request.app.state.token),
        content=body,
    )
    upstream = await client.send(upstream_request, stream=True)
    headers = _response_headers(upstream.headers)
    content_type = upstream.headers.get("content-type", "")
    if upstream.status_code >= 400 and "text/event-stream" not in content_type:
        payload = await upstream.aread()
        await upstream.aclose()
        return Response(
            content=payload,
            status_code=upstream.status_code,
            headers=headers,
            media_type=None,
        )
    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers=headers,
        background=BackgroundTask(upstream.aclose),
        media_type=None,
    )


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"], include_in_schema=False)
async def proxy_api(request: Request) -> Response:
    return await _proxy(request)


@app.api_route("/health", methods=["GET"], include_in_schema=False)
@app.api_route("/ready", methods=["GET"], include_in_schema=False)
async def proxy_meta(request: Request) -> Response:
    return await _proxy(request)


if __name__ == "__main__":
    uvicorn.run(
        "scripts.preview_server:app",
        host=os.getenv("INNER_TERRAIN_PREVIEW_HOST", "0.0.0.0"),
        port=int(os.getenv("INNER_TERRAIN_PREVIEW_PORT", "8081")),
        reload=False,
    )
