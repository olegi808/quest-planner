"""Сервер планера: отдаёт мини-апп и хранит резервные копии данных пользователей.

Каждый запрос подписан Telegram (initData) — проверяем подпись токеном бота,
поэтому прочитать или записать чужие данные нельзя.
"""
import hashlib
import hmac
import json
import os
import sqlite3
import time
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web

TOKEN = os.environ["BOT_TOKEN"]
DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
WEB_DIR = Path(os.environ.get("WEB_DIR", "/app/web"))
MAX_BODY = 6 * 1024 * 1024          # одно состояние планера — до 6 МБ
KEEP_DAYS = 60                      # сколько дней хранить ежедневные снимки
INIT_TTL = 30 * 24 * 3600           # насколько старый initData ещё принимаем

DATA_DIR.mkdir(parents=True, exist_ok=True)
db = sqlite3.connect(DATA_DIR / "planner.db", check_same_thread=False)
db.executescript(
    """
    create table if not exists state (user_id integer primary key, data text not null, updated integer not null);
    create table if not exists snaps (user_id integer not null, key text not null, data text not null,
                                      updated integer not null, size integer not null, primary key (user_id, key));
    """
)
SECRET = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()


def user_from_init(init_data: str) -> int:
    """Проверка подписи Telegram WebApp initData. Возвращает id пользователя."""
    pairs = dict(parse_qsl(init_data or "", keep_blank_values=True))
    got = pairs.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    calc = hmac.new(SECRET, check.encode(), hashlib.sha256).hexdigest()
    if not got or not hmac.compare_digest(calc, got):
        raise web.HTTPUnauthorized(text="bad signature")
    if time.time() - int(pairs.get("auth_date", "0")) > INIT_TTL:
        raise web.HTTPUnauthorized(text="expired")
    return int(json.loads(pairs["user"])["id"])


def data_size(d: dict) -> int:
    return sum(len(d.get(k) or []) for k in ("tasks", "habits", "fin", "goals", "asc", "wheel"))


def snapshot(uid: int, key: str, raw: str, updated: int, size: int) -> None:
    db.execute(
        "insert into snaps(user_id,key,data,updated,size) values(?,?,?,?,?) "
        "on conflict(user_id,key) do update set data=excluded.data, updated=excluded.updated, size=excluded.size",
        (uid, key, raw, updated, size),
    )


async def body(request: web.Request) -> dict:
    if (request.content_length or 0) > MAX_BODY:
        raise web.HTTPRequestEntityTooLarge(max_size=MAX_BODY, actual_size=request.content_length)
    try:
        return json.loads((await request.read()).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise web.HTTPBadRequest(text="bad json")


async def api_load(request: web.Request):
    uid = user_from_init((await body(request)).get("initData"))
    row = db.execute("select data from state where user_id=?", (uid,)).fetchone()
    return web.json_response({"data": json.loads(row[0]) if row else None})


async def api_save(request: web.Request):
    req = await body(request)
    uid = user_from_init(req.get("initData"))
    data = req.get("data")
    if not isinstance(data, dict) or not data.get("v"):
        raise web.HTTPBadRequest(text="bad data")
    raw = json.dumps(data, ensure_ascii=False)
    now_ms = int(time.time() * 1000)
    updated, size = int(data.get("updated") or now_ms), data_size(data)
    old = db.execute("select data, updated from state where user_id=?", (uid,)).fetchone()
    if old:
        old_data = json.loads(old[0])
        old_size = data_size(old_data)
        # данных резко стало меньше (очистка или сбой) — сохраняем прежнюю версию отдельно
        if old_size >= 5 and size < old_size * 0.5:
            snapshot(uid, "before-" + time.strftime("%Y-%m-%d %H:%M", time.gmtime()), old[0], now_ms, old_size)
    db.execute(
        "insert into state(user_id,data,updated) values(?,?,?) "
        "on conflict(user_id) do update set data=excluded.data, updated=excluded.updated",
        (uid, raw, updated),
    )
    snapshot(uid, "day-" + time.strftime("%Y-%m-%d", time.gmtime()), raw, now_ms, size)
    cutoff = int((time.time() - KEEP_DAYS * 86400) * 1000)
    db.execute("delete from snaps where user_id=? and updated<?", (uid, cutoff))
    db.commit()
    return web.json_response({"ok": True})


async def api_versions(request: web.Request):
    uid = user_from_init((await body(request)).get("initData"))
    rows = db.execute(
        "select key, updated, size from snaps where user_id=? order by updated desc limit 40", (uid,)
    ).fetchall()
    return web.json_response({"versions": [{"key": k, "updated": u, "size": s} for k, u, s in rows]})


async def api_restore(request: web.Request):
    req = await body(request)
    uid = user_from_init(req.get("initData"))
    row = db.execute("select data from snaps where user_id=? and key=?", (uid, str(req.get("key")))).fetchone()
    if not row:
        raise web.HTTPNotFound(text="no such version")
    return web.json_response({"data": json.loads(row[0])})


OLD_ORIGIN = "https://olegi808.github.io"  # прежний адрес планера на GitHub Pages


@web.middleware
async def cors_for_old_origin(request: web.Request, handler):
    """Старая версия с GitHub может прислать копию данных с устройства (только /api/stash)."""
    if request.path == "/api/stash" and request.method == "OPTIONS":
        resp = web.Response()
    else:
        resp = await handler(request)
    if request.path == "/api/stash" and request.headers.get("Origin") == OLD_ORIGIN:
        resp.headers.update({
            "Access-Control-Allow-Origin": OLD_ORIGIN,
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        })
    return resp


async def api_stash(request: web.Request):
    """Копия данных с устройства (со старого адреса) — кладём отдельным снимком, текущие данные не трогаем."""
    req = await body(request)
    uid = user_from_init(req.get("initData"))
    data = req.get("data")
    if not isinstance(data, dict) or not data.get("v") or not data_size(data):
        return web.json_response({"ok": False})
    raw = json.dumps(data, ensure_ascii=False)
    snapshot(uid, "device-" + str(req.get("kind") or "local") + "-" + time.strftime("%Y-%m-%d %H:%M", time.gmtime()),
             raw, int(time.time() * 1000), data_size(data))
    db.commit()
    return web.json_response({"ok": True})


async def index(request: web.Request):
    # без кэша — чтобы обновления приложения доходили сразу
    return web.FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})


app = web.Application(client_max_size=MAX_BODY, middlewares=[cors_for_old_origin])
app.add_routes([
    web.post("/api/stash", api_stash),
    web.options("/api/stash", lambda r: web.Response()),
    web.get("/", index),
    web.get("/index.html", index),
    web.post("/api/load", api_load),
    web.post("/api/save", api_save),
    web.post("/api/versions", api_versions),
    web.post("/api/restore", api_restore),
    web.get("/health", lambda r: web.Response(text="ok")),
])

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=8090)
