"""FastAPI application serving the analytical layer and the built Vue client."""
from __future__ import annotations

import contextlib
import logging
import os
import secrets
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from collector.collect import ROOT
from api import auth as auth_module, dispatch_db
from api.db import connect, schema_gaps
from api.sources import name_sources
from api.routes import (alerts, assistant, audit, auth, benchmark, claims, companies, diffs, duplicates,
                        elements, events,
                        ingestion, issues, meta, notifications, notify, profiling, propagation,
                        reports, review, risks, search, timeline, users)

@asynccontextmanager
async def lifespan(_: FastAPI):
    # A generated password nobody can read is the same as a locked-out platform.
    notice = auth_module.startup_notice()
    if notice:
        print(notice, flush=True)
    # 库比代码旧时，平台只读打开、修不了它，而症状是某个页面 500 而不是启动失败。
    # 部署时就说出来，别等到有人点开事件详情。
    try:
        with contextlib.closing(connect()) as probe:
            gaps = schema_gaps(probe)
        if gaps:
            print("！数据库结构比这份代码旧：" + "；".join(gaps)
                  + "\n  平台以只读方式打开数据库，无法自行升级。在装有源码的机器上跑一次："
                  + "\n  python3 -m processor.repair_labels && python3 -m processor.finalize",
                  flush=True)
    except Exception as exc:  # 数据库还没到位时不该拦住启动
        print(f"（跳过结构检查：{exc}）", flush=True)
    # 处置与投递那几张表由平台自己建（只读连接建不了，但 dispatch_db 是可写的那条路）。
    # 在这里建一次，好让风险事项库的只读 JOIN 不必先问「这张表在不在」——一个全新的
    # 库第一次被读之前，表就已经在了。
    try:
        with contextlib.closing(dispatch_db.connect()):
            pass
    except Exception as exc:
        print(f"（跳过处置表初始化：{exc}）", flush=True)
    yield


# 接口文档不对外：/docs、/redoc、/openapi.json 此前无需登录即可读，列出全部 91 个
# 接口与请求字段（I-06 全平台排查）。开发时要看，在本机临时打开即可。
class PublicJSONResponse(JSONResponse):
    """每个接口的出口：来源编码旁边一律补上中文名（I-06）。"""

    def render(self, content) -> bytes:
        return super().render(name_sources(content))


app = FastAPI(title="上市公司事件智能平台", version="1.0.0", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None,
              default_response_class=PublicJSONResponse)

# 技术日志：原始异常与堆栈只进这里，不进任何响应。读者看到的是一句中文和一个编号，
# 编号能在这份日志里检索到原文。
ERROR_LOG = ROOT / "data" / "logs" / "api-errors.log"
technical_log = logging.getLogger("lcei.api")
if not technical_log.handlers:
    try:
        ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(ERROR_LOG, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        technical_log.addHandler(handler)
    except OSError:  # 只读部署时退回标准错误输出，不拦住启动
        technical_log.addHandler(logging.StreamHandler())
    technical_log.setLevel(logging.INFO)
    technical_log.propagate = False

#: 框架默认的英文提示换成读者看得懂的话；业务代码自己写的中文 detail 原样保留。
STATUS_MESSAGES = {
    400: "请求有误，请检查后重试", 401: "未登录或会话已过期", 403: "当前账号没有此操作的权限",
    404: "要找的内容不存在或已被移除", 405: "不支持此操作", 409: "内容已被他人修改，请刷新后重试",
    413: "提交的内容过大", 422: "提交的内容不完整或格式不对", 429: "操作过于频繁，请稍后再试",
}


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", None) or "-"


def _readable(detail, status: int) -> str:
    """业务代码写的中文原样给出；框架或第三方库的英文、结构化报错换成通用说法。"""
    if isinstance(detail, str) and any("\u4e00" <= char <= "\u9fff" for char in detail):
        return detail
    return STATUS_MESSAGES.get(status, "服务暂时不可用，请稍后重试")


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    if exc.status_code >= 500:
        technical_log.error("request_id=%s status=%s detail=%r", _request_id(request), exc.status_code, exc.detail)
    return JSONResponse({"detail": _readable(exc.detail, exc.status_code), "request_id": _request_id(request)},
                        status_code=exc.status_code, headers=getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    # 默认响应会把提交的内容和字段约束原样回显。
    technical_log.info("request_id=%s validation=%r", _request_id(request), exc.errors()[:5])
    return JSONResponse({"detail": STATUS_MESSAGES[422], "request_id": _request_id(request)}, status_code=422)


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    technical_log.exception("request_id=%s path=%s unhandled", _request_id(request), request.url.path)
    return JSONResponse({"detail": "服务暂时不可用，请稍后重试", "request_id": _request_id(request)},
                        status_code=500)

# The Vue dev server runs on its own port; in production the built client is
# served from this app and the allowlist is unused.  Credentials are allowed
# because the session lives in a cookie rather than a bearer header.
# 构建产物有 1.1 MB（ECharts 一个 chunk 就 657 KB），此前是裸传的。JSON 也一样：
# 事件台账一页 50 条、检索一次问七组，都是高压缩比的文本。1000 字节以下不压，
# 免得为一个健康检查付 CPU。
app.add_middleware(GZipMiddleware, minimum_size=1000)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin for origin in os.environ.get(
        "API_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if origin],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.middleware("http")
async def require_session(request: Request, call_next):
    """Every data endpoint needs a session; the sign-in routes cannot.

    The gate sits in one place rather than on each route because a new endpoint
    added later must be protected by default — forgetting a dependency should
    not be the thing that publishes the corpus.
    """
    path = request.url.path
    if (path.startswith("/api/") and path not in auth_module.PUBLIC_PATHS
            and request.method != "OPTIONS"):
        if not auth_module.live_session(request.cookies.get(auth_module.COOKIE_NAME)):
            return JSONResponse({"detail": "未登录或会话已过期"}, status_code=401)
    return await call_next(request)


@app.middleware("http")
async def tag_request(request: Request, call_next):
    """每个请求一个短编号：出错时读者报这个编号，技术日志里按它找到原始报错。"""
    request.state.request_id = secrets.token_hex(6)
    try:
        response = await call_next(request)
    except Exception:  # 中间件链里漏出的异常同样不带原文出去
        technical_log.exception("request_id=%s path=%s unhandled", request.state.request_id, request.url.path)
        response = JSONResponse({"detail": "服务暂时不可用，请稍后重试",
                                 "request_id": request.state.request_id}, status_code=500)
    response.headers["X-Request-ID"] = request.state.request_id
    return response


for module in (auth, meta, events, claims, diffs, duplicates, elements, ingestion, profiling,
               benchmark,
               issues, timeline, propagation, alerts, risks, companies, review, notifications,
               notify, audit,
               reports, search, users, assistant):
    app.include_router(module.router)

CLIENT_DIST = ROOT / "web" / "dist"
if CLIENT_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=CLIENT_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def client(path: str) -> FileResponse:
        """Serve the built client, falling back to it for client-side routes.

        Routing lives in the browser, so a reload on /claims must return the
        app shell rather than a 404 — but only for paths the client owns; an
        unknown /api/* route still has to fail as a missing endpoint.  The shell
        itself is public: it has to load in order to show the sign-in page.
        """
        if path.startswith("api/"):
            raise HTTPException(status_code=404, detail="unknown endpoint")
        # 只发 web/dist 里面的文件。此前直接拼 CLIENT_DIST / path，「/%2e%2e/%2e%2e/.env」
        # 不登录就能读走密钥与整个数据目录（I-06 全平台排查）。解析后必须仍在 dist 之下，
        # 隐藏文件一律不发；其余情况一律回落到应用外壳。
        root = CLIENT_DIST.resolve()
        try:
            candidate = (CLIENT_DIST / path).resolve()
        except (OSError, ValueError):
            candidate = root
        if (path and candidate.is_relative_to(root) and candidate.is_file()
                and not any(part.startswith(".") for part in candidate.relative_to(root).parts)):
            return FileResponse(candidate)
        return FileResponse(CLIENT_DIST / "index.html")
