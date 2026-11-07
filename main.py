import os, hmac, hashlib, json
from urllib.parse import parse_qsl
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse

TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_KEY"]
APP_URL = os.environ["APP_URL"].rstrip("/")
H = {"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY, "Content-Type": "application/json"}
TG = f"https://api.telegram.org/bot{TOKEN}/"
app = FastAPI()


async def sb(method, path, headers=None, **kw):
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.request(method, f"{SB_URL}/rest/v1/{path}", headers=headers or H, **kw)
        if r.status_code >= 400:
            print("SUPABASE ERROR", r.status_code, r.text)
            return []
        return r.json() if r.text else []


async def tg(method, **data):
    async with httpx.AsyncClient(timeout=20) as c:
        await c.post(TG + method, json=data)


def user(init):
    d = dict(parse_qsl(init, keep_blank_values=True))
    h = d.pop("hash", "")
    s = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(hmac.new(key, s.encode(), hashlib.sha256).hexdigest(), h):
        raise HTTPException(401, "bad init data")
    return json.loads(d["user"])


async def setting(k):
    r = await sb("GET", "settings", params={"key": f"eq.{k}"})
    return r[0]["value"] if r else ""


async def save(k, v):
    await sb("POST", "settings", params={"on_conflict": "key"},
             headers={**H, "Prefer": "resolution=merge-duplicates"}, json={"key": k, "value": v})


def num(x):
    try:
        return int(x)
    except Exception:
        raise HTTPException(400, "Enter a valid amount")


async def balance(uid):
    rows = await sb("GET", "txns", params={"user_id": f"eq.{uid}", "select": "amount,status"})
    return sum(r["amount"] for r in rows
               if r["status"] != "rejected" and not (r["amount"] > 0 and r["status"] == "pending"))


@app.on_event("startup")
async def startup():
    await tg("setWebhook", url=APP_URL + "/webhook")


@app.get("/")
async def home():
    return FileResponse("index.html")


@app.get("/api/ping")
async def ping():
    return {"ok": 1}


@app.post("/api/modes")
async def modes(req: Request):
    user((await req.json())["init"])
    rows = await sb("GET", "matches", params={"status": "eq.open", "select": "mode"})
    out = {}
    for r in rows:
        out[r["mode"]] = out.get(r["mode"], 0) + 1
    return [{"mode": k, "count": v} for k, v in out.items()]


@app.post("/api/matches")
async def matches(req: Request):
    b = await req.json()
    uid = user(b["init"])["id"]
    rows = await sb("GET", "matches", params={"status": "eq.open", "mode": f"eq.{b['mode']}", "order": "id"})
    ids = ",".join(str(r["id"]) for r in rows)
    js = await sb("GET", "joins", params={"match_id": f"in.({ids})", "select": "match_id,user_id"}) if ids else []
    for r in rows:
        mine = [j for j in js if j["match_id"] == r["id"]]
        r["joined"] = len(mine)
        r["me"] = any(j["user_id"] == uid for j in mine)
        r.pop("room", None)
        r.pop("result", None)
    return rows


@app.post("/api/join")
async def join(req: Request):
    b = await req.json()
    uid = user(b["init"])["id"]
    mid = num(b["match_id"])
    ign = str(b.get("ign", "")).strip()[:40]
    if not ign:
        raise HTTPException(400, "Enter your in-game name")
    m = await sb("GET", "matches", params={"id": f"eq.{mid}", "status": "eq.open"})
    if not m:
        raise HTTPException(404, "Match is closed")
    js = await sb("GET", "joins", params={"match_id": f"eq.{mid}", "select": "user_id"})
    if any(j["user_id"] == uid for j in js):
        raise HTTPException(400, "Already joined")
    if len(js) >= m[0]["slots"]:
        raise HTTPException(400, "Match is full")
    fee = m[0].get("fee") or 0
    if fee and await balance(uid) < fee:
        raise HTTPException(400, f"Not enough balance. Entry fee is {fee} TK")
    await sb("POST", "joins", json={"match_id": mid, "user_id": uid, "ign": ign})
    if fee:
        await sb("POST", "txns", json={"user_id": uid, "amount": -fee, "kind": "fee",
                                       "status": "approved", "note": f"Match #{mid}"})
    return {"ok": 1}


@app.post("/api/my")
async def my(req: Request):
    uid = user((await req.json())["init"])["id"]
    js = await sb("GET", "joins", params={"user_id": f"eq.{uid}", "order": "id.desc"})
    ids = ",".join(str(j["match_id"]) for j in js)
    ms = await sb("GET", "matches", params={"id": f"in.({ids})"}) if ids else []
    byid = {m["id"]: m for m in ms}
    return [{"ign": j["ign"], **byid[j["match_id"]]} for j in js if j["match_id"] in byid]


@app.post("/api/results")
async def results(req: Request):
    user((await req.json())["init"])
    return await sb("GET", "matches", params={"result": "not.is.null", "order": "id.desc", "limit": "30",
                                               "select": "id,mode,title,start_time,result"})


@app.post("/api/info")
async def info(req: Request):
    u = user((await req.json())["init"])
    return {"name": u.get("first_name", ""), "id": u["id"], "rules": await setting("rules"),
            "support": await setting("support")}


@app.post("/api/wallet")
async def wallet(req: Request):
    uid = user((await req.json())["init"])["id"]
    rows = await sb("GET", "txns", params={"user_id": f"eq.{uid}", "order": "id.desc", "limit": "30"})
    return {"balance": await balance(uid), "pay": await setting("pay_info"),
            "minw": int(await setting("min_withdraw") or 100),
            "txns": [{k: r.get(k) for k in ("id", "amount", "kind", "status", "method", "note")} for r in rows]}


async def request_tx(uid, amount, kind, method, ref):
    r = await sb("POST", "txns", headers={**H, "Prefer": "return=representation"},
                 json={"user_id": uid, "amount": amount, "kind": kind, "method": method, "ref": ref})
    if not r:
        raise HTTPException(500, "Could not save. Try again.")
    i = r[0]["id"]
    await tg("sendMessage", chat_id=ADMIN_ID,
             text=f"{kind.title()} #{i}: {abs(amount)} TK, {method}, {ref}, user {uid}\n/approve {i}  or  /reject {i}")


@app.post("/api/deposit")
async def deposit(req: Request):
    b = await req.json()
    uid = user(b["init"])["id"]
    amt = num(b.get("amount"))
    ref = str(b.get("ref", "")).strip()[:40]
    method = str(b.get("method", "")).strip()[:20]
    if amt < 10 or not ref or not method:
        raise HTTPException(400, "Fill amount, method and transaction ID")
    await request_tx(uid, amt, "deposit", method, ref)
    return {"ok": 1}


@app.post("/api/withdraw")
async def withdraw(req: Request):
    b = await req.json()
    uid = user(b["init"])["id"]
    amt = num(b.get("amount"))
    number = str(b.get("number", "")).strip()[:20]
    method = str(b.get("method", "")).strip()[:20]
    minw = int(await setting("min_withdraw") or 100)
    if amt < minw:
        raise HTTPException(400, f"Minimum withdraw is {minw} TK")
    if not number or not method:
        raise HTTPException(400, "Fill method and number")
    if await balance(uid) < amt:
        raise HTTPException(400, "Not enough balance")
    await request_tx(uid, -amt, "withdraw", method, number)
    return {"ok": 1}


@app.post("/webhook")
async def webhook(req: Request):
    u = await req.json()
    m = u.get("message")
    if not m or "text" not in m:
        return {"ok": 1}
    chat, uid, t = m["chat"]["id"], m["from"]["id"], m["text"].strip()

    async def say(x):
        await tg("sendMessage", chat_id=chat, text=x)

    if t.startswith("/start"):
        await tg("sendMessage", chat_id=chat, text="Welcome! Open the app to join matches.",
                 reply_markup={"inline_keyboard": [[{"text": "Open App", "web_app": {"url": APP_URL}}]]})
    elif uid == ADMIN_ID:
        cmd, _, rest = t.partition(" ")
        rest = rest.strip()
        if cmd == "/addmatch":
            p = [x.strip() for x in rest.split("|")]
            if len(p) in (5, 6) and p[3].isdigit() and (len(p) == 5 or p[5].isdigit()):
                await sb("POST", "matches", json={"mode": p[0], "title": p[1], "start_time": p[2],
                                                  "slots": int(p[3]), "prize": p[4],
                                                  "fee": int(p[5]) if len(p) == 6 else 0})
                await say("Added.")
            else:
                await say("Use: /addmatch Mode | Title | Time | Slots | Prize | Fee")
        elif cmd == "/matches":
            rows = await sb("GET", "matches", params={"order": "id.desc", "limit": "20"})
            await say("\n".join(f"{r['id']}: [{r['mode']}] {r['title']} fee {r.get('fee') or 0} ({r['status']})"
                                for r in rows) or "None.")
        elif cmd == "/players":
            js = await sb("GET", "joins", params={"match_id": f"eq.{rest}", "order": "id"})
            await say("\n".join(f"{i+1}. {j['ign']}" for i, j in enumerate(js)) or "No players.")
        elif cmd in ("/setroom", "/setresult"):
            a, _, b = rest.partition(" ")
            if a.isdigit() and b:
                await sb("PATCH", "matches", params={"id": f"eq.{a}"},
                         json={("room" if cmd == "/setroom" else "result"): b})
                await say("Updated.")
        elif cmd == "/close":
            await sb("PATCH", "matches", params={"id": f"eq.{rest}"}, json={"status": "closed"})
            await say("Closed.")
        elif cmd == "/delmatch":
            await sb("DELETE", "matches", params={"id": f"eq.{rest}"})
            await say("Deleted.")
        elif cmd in ("/setrules", "/setsupport", "/setpay") and rest:
            await save({"/setrules": "rules", "/setsupport": "support", "/setpay": "pay_info"}[cmd], rest)
            await say("Saved.")
        elif cmd == "/setminw" and rest.isdigit():
            await save("min_withdraw", rest)
            await say("Saved.")
        elif cmd in ("/approve", "/reject"):
            r = await sb("GET", "txns", params={"id": f"eq.{rest}"})
            if r and r[0]["status"] == "pending":
                st = "approved" if cmd == "/approve" else "rejected"
                await sb("PATCH", "txns", params={"id": f"eq.{rest}"}, json={"status": st})
                await tg("sendMessage", chat_id=r[0]["user_id"],
                         text=f"Your {r[0]['kind']} of {abs(r[0]['amount'])} TK was {st}.")
                await say("Done.")
            else:
                await say("Not found or already done.")
        elif cmd == "/txns":
            r = await sb("GET", "txns", params={"status": "eq.pending", "order": "id"})
            await say("\n".join(f"#{x['id']} {x['kind']} {abs(x['amount'])} {x['method']} {x['ref']} u{x['user_id']}"
                                for x in r) or "No pending.")
        elif cmd == "/addbal":
            p = rest.split(None, 2)
            if len(p) >= 2 and p[0].isdigit() and p[1].lstrip("-").isdigit():
                await sb("POST", "txns", json={"user_id": int(p[0]), "amount": int(p[1]), "kind": "prize",
                                               "status": "approved", "note": p[2] if len(p) > 2 else "Prize"})
                await tg("sendMessage", chat_id=int(p[0]), text=f"{p[1]} TK added to your wallet.")
                await say("Added.")
            else:
                await say("Use: /addbal user_id amount note")
    return {"ok": 1}
