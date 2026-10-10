"""网页值班台的静态文件与浏览器登录(商业化 B1)。

- **静态文件**:``web/``(``web/console`` 用 Vite 构建出来的,随站点包装)。``/`` 回 ``index.html``,
  ``/assets/<文件>`` 回构建产物(文件名里带内容哈希,可以长缓存);别的非 ``/api`` 路径也回
  ``index.html``(单页应用自己的路由)。每个回复带严格的安全头:CSP 只许本站的脚本、样式、图片、
  连接,不许被嵌进别的页面。
- **浏览器登录用 cookie**:``<img>`` 放实时画面、``EventSource`` 收事件都带不了 ``Authorization``
  头,所以网页登录时站点种一个 ``HttpOnly; Secure; SameSite=Strict`` 的 cookie(页面上的脚本读不到,
  别的网站带不过来),**登录回复里不再给令牌**。
- **防跨站伪造**:靠 cookie 认的人,改东西的请求(非 GET)必须带 ``X-D1Max-Web: 1`` 头 —— 别的
  网站的页面加不了这个头(要预检,站点不答应跨域)。手机、命令行用 ``Authorization`` 头的不受影响。
"""

from __future__ import annotations

import mimetypes
from http.cookies import CookieError, SimpleCookie
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent / "web"
COOKIE = "d1max_session"
CSRF_HEADER = "X-D1Max-Web"

#: 每个静态回复都带的安全头。
SECURITY_HEADERS = (
    ("Content-Security-Policy",
     "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
     "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
     "form-action 'self'; frame-ancestors 'none'"),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
)

_TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
          ".html": "text/html; charset=utf-8", ".svg": "image/svg+xml", ".woff2": "font/woff2",
          ".png": "image/png", ".json": "application/json; charset=utf-8"}


def session_cookie(token: str) -> str:
    return f"{COOKIE}={token}; Path=/; HttpOnly; Secure; SameSite=Strict"


def clear_cookie() -> str:
    return f"{COOKIE}=; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=0"


def cookie_token(header: str | None) -> str | None:
    if not header:
        return None
    try:
        c = SimpleCookie()
        c.load(header)
    except CookieError:
        return None
    m = c.get(COOKIE)
    return m.value if m is not None and m.value else None


def resolve(path: str, web_dir: Path = WEB_DIR) -> tuple[Path, str, bool] | None:
    """静态路径 → ``(文件, Content-Type, 能不能长缓存)``;没构建过(没有 index.html)回 None。

    只认 ``/assets/<一层文件名>``,别的都回 ``index.html``:不拼用户给的路径,走不出 ``web/``。"""
    root = web_dir.resolve()

    def _inside(f: Path) -> bool:
        # B 阶段外审 M1:跟着符号链接走出 web/ 的不给(先解析出真实路径再比)
        try:
            return f.resolve(strict=True).is_relative_to(root)
        except OSError:
            return False

    index = web_dir / "index.html"
    if not index.is_file() or not _inside(index):
        return None
    if path.startswith("/assets/"):
        name = path[len("/assets/"):]
        if name and "/" not in name and "\\" not in name and not name.startswith(".") \
                and all(c.isalnum() or c in "-_." for c in name):
            f = web_dir / "assets" / name
            if f.is_file() and _inside(f):
                ext = f.suffix.lower()
                typ = _TYPES.get(ext) or mimetypes.guess_type(name)[0] or "application/octet-stream"
                return f, typ, True
        return None
    return index, _TYPES[".html"], False
