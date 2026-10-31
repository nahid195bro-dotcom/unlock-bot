import os, hmac, hashlib, json, time
from urllib.parse import parse_qsl
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse

TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_KEY"]
APP_URL = os.environ["APP_URL"].rstrip("/")
BLOCK_ID = os.environ.get("ADSGRAM_BLOCK_ID", "")
H = {"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY, "Content-Type": "application/json"}
TG = f"https://api.telegram.org/bot{TOKEN}/"
app = FastAPI()
last_ad = {}


async def sb(method, path, **kw):
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.request(method, f"{SB_URL}/rest/v1/{path}", headers=H, **kw)
        return r.json() if r.text else []


async def tg(method, **data):
    async with httpx.AsyncClient(timeout=20) as c:
        await c.post(TG + method, json=data)


def user_id(init):
    d = dict(parse_qsl(init, keep_blank_values=True))
    h = d.pop("hash", "")
    s = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(hmac.new(key, s.encode(), hashlib.sha256).hexdigest(), h):
        raise HTTPException(401, "bad init data")
    return json.loads(d["user"])["id"]


@app.on_event("startup")
async def startup():
    await tg("setWebhook", url=APP_URL + "/webhook")


@app.get("/")
async def home():
    return FileResponse("index.html")


@app.get("/api/ping")
async def ping():
    return {"ok": 1}


@app.get("/api/config")
async def config():
    return {"block": BLOCK_ID}


@app.post("/api/items")
async def items(req: Request):
    uid = user_id((await req.json())["init"])
    rows = await sb("GET", "items?select=*&order=id")
    prog = await sb("GET", f"progress?user_id=eq.{uid}&select=item_id,watched")
    got = {p["item_id"]: p["watched"] for p in prog}
    out = []
    for r in rows:
        w = got.get(r["id"], 0)
        done = w >= r["ads"]
        out.append({"id": r["id"], "title": r["title"], "ads": r["ads"], "watched": min(w, r["ads"]),
                    "link": r["link"] if done else None})
    return out


@app.post("/api/ad")
async def ad(req: Request):
    b = await req.json()
    uid = user_id(b["init"])
    now = time.time()
    if now - last_ad.get(uid, 0) < 8:
        raise HTTPException(429, "too fast")
    last_ad[uid] = now
    iid = int(b["item_id"])
    item = await sb("GET", f"items?id=eq.{iid}&select=ads")
    if not item:
        raise HTTPException(404)
    cur = await sb("GET", f"progress?user_id=eq.{uid}&item_id=eq.{iid}&select=watched")
    w = (cur[0]["watched"] if cur else 0)
    if w < item[0]["ads"]:
        w += 1
    await sb("POST", "progress?on_conflict=user_id,item_id",
             headers={**H, "Prefer": "resolution=merge-duplicates"},
             json={"user_id": uid, "item_id": iid, "watched": w})
    return {"watched": w}


@app.post("/webhook")
async def webhook(req: Request):
    u = await req.json()
    m = u.get("message")
    if not m or "text" not in m:
        return {"ok": 1}
    chat, uid, t = m["chat"]["id"], m["from"]["id"], m["text"].strip()
    if t.startswith("/start"):
        await tg("sendMessage", chat_id=chat, text="Open the app to unlock panel links, videos and giveaways.",
                 reply_markup={"inline_keyboard": [[{"text": "Open App", "web_app": {"url": APP_URL}}]]})
    elif uid == ADMIN_ID:
        if t.startswith("/additem"):
            p = [x.strip() for x in t[8:].split("|")]
            if len(p) == 3 and p[1].isdigit():
                await sb("POST", "items", json={"title": p[0], "ads": int(p[1]), "link": p[2]})
                await tg("sendMessage", chat_id=chat, text="Added.")
            else:
                await tg("sendMessage", chat_id=chat, text="Use: /additem Title | 5 | https://link")
        elif t.startswith("/items"):
            rows = await sb("GET", "items?select=*&order=id")
            txt = "\n".join(f"{r['id']}: {r['title']} ({r['ads']} ads)" for r in rows) or "No items."
            await tg("sendMessage", chat_id=chat, text=txt)
        elif t.startswith("/delitem"):
            await sb("DELETE", f"items?id=eq.{t[8:].strip()}")
            await tg("sendMessage", chat_id=chat, text="Deleted.")
        elif t.startswith("/setads"):
            p = t[7:].split()
            if len(p) == 2 and all(x.isdigit() for x in p):
                await sb("PATCH", f"items?id=eq.{p[0]}", json={"ads": int(p[1])})
                await tg("sendMessage", chat_id=chat, text="Updated.")
        elif t.startswith("/setlink"):
            p = t[8:].strip().split(None, 1)
            if len(p) == 2 and p[0].isdigit():
                await sb("PATCH", f"items?id=eq.{p[0]}", json={"link": p[1]})
                await tg("sendMessage", chat_id=chat, text="Updated.")
    return {"ok": 1}
