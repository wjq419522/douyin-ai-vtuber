"""AI 虚拟主播「佳垚」中控服务。

  python -m server.main            # 在项目根目录运行
  overlay（OBS 浏览器源）: http://127.0.0.1:8765/overlay
  操作员控制台:             http://127.0.0.1:8765/console
"""
import asyncio
import itertools
import os
import pathlib
import time

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import douyin, lipsync, safety
from .brain import Brain, generate_image
from .host import Host
from .memory import Memory


def load_env(path=".env"):
    p = pathlib.Path(path)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


load_env()
ROOT = pathlib.Path(__file__).resolve().parent.parent
AUDIO = ROOT / "runtime_audio"
AUDIO.mkdir(exist_ok=True)

app = FastAPI(title="AI虚拟主播·佳垚")
app.mount("/web", StaticFiles(directory=ROOT / "web"), name="web")
app.mount("/audio", StaticFiles(directory=AUDIO), name="audio")

brain = Brain()
mem = Memory(str(ROOT / "runtime_memory.db"))
host = Host(mem)
limiter = safety.RateLimiter()
queue: asyncio.Queue = asyncio.Queue(maxsize=200)
overlays: set[WebSocket] = set()
consoles: set[WebSocket] = set()
pending: dict[int, dict] = {}
ids = itertools.count(1)
state = {
    "mode": os.getenv("REPLY_MODE", "review"),       # auto / review / paused
    "operator_seen": 0.0,
    "timeout": int(os.getenv("OPERATOR_TIMEOUT", "120")),
    "tts": os.getenv("TTS_ENGINE", "edge"),
}


def operator_alive() -> bool:
    return time.time() - state["operator_seen"] < state["timeout"]


async def broadcast(group: set, msg: dict):
    for ws in list(group):
        try:
            await ws.send_json(msg)
        except Exception:
            group.discard(ws)


async def status():
    s = {"type": "status", "mode": state["mode"], "operator": operator_alive(),
         "llm": brain.online, "queue": queue.qsize(), "pending": len(pending)}
    await broadcast(consoles, s)
    await broadcast(overlays, s)


async def tts(text: str, rid: int) -> str | None:
    if state["tts"] != "edge":
        return None
    try:
        import edge_tts
        f = AUDIO / f"{rid}.mp3"
        await edge_tts.Communicate(text, os.getenv("TTS_VOICE", "zh-CN-XiaoyiNeural"), rate="+5%").save(str(f))
        return f"/audio/{rid}.mp3"
    except Exception as e:
        print("[tts] edge-tts 不可用，改用浏览器TTS：", e)
        return None


async def perform(item: dict):
    """把一条已通过审核的回复推送给 overlay。"""
    out = item["out"]
    host.touch()
    if item.get("kind", "").startswith("program:"):
        mem.start_window(item["kind"].split(":", 1)[1])
    if out.get("action") == "paint":
        p = out.get("paint", {})
        out["paint"]["image"] = await generate_image(p.get("prompt", "山水"), p.get("style", "水墨"))
    out["audio"] = await tts(out.get("say", ""), item["id"])
    out["talk_video"] = await lipsync.render(out["audio"]) if out["audio"] else None
    out["browser_tts"] = state["tts"] == "browser" or (state["tts"] == "edge" and not out["audio"])
    await broadcast(overlays, {"type": "perform", "id": item["id"], "user": item["user"],
                               "question": item["text"], **out})
    await broadcast(consoles, {"type": "log", "level": "out", "text": f"[播出] {out.get('say')}"})


async def handle(ev: dict):
    user, text, kind = ev["user"][:16], ev["text"][:100], ev["kind"]
    synthetic = kind.startswith("program") or kind in ("rollcall", "product")
    if not synthetic:
        await broadcast(overlays, {"type": "danmaku", "user": user, "text": text, "kind": kind})
        mem.count(kind)
        host.touch(ev["uid"], user)
    info = mem.see(ev["uid"], user) if not synthetic else {}

    if kind in ("like", "enter"):
        return
    if kind == "gift":
        mem.note_gift(ev["uid"])
        out = {"say": host.gift_thanks(user, info), "action": "nod"}
    elif kind == "rollcall":
        out = {"say": text, "action": "wave"}
    elif synthetic:
        out = await brain.reply("直播间", text, "；这是你主动发起的节目，不是观众提问")
    else:
        if cat := safety.check_input(text):
            await broadcast(consoles, {"type": "log", "level": "block", "text": f"[拦截输入/{cat}] {user}: {text}"})
            if cat in safety.SAFE_DEFLECT and limiter.allow("__deflect__" + cat, cat):
                out = {"say": safety.SAFE_DEFLECT[cat], "action": "think"}
            else:
                return
        elif safety.is_identity_question(text):
            out = {"say": "我是AI虚拟主播佳垚，不是真人哦～直播间由真人运营者负责管理。", "action": "nod"}
        else:
            mem.log_question(text)
            hint = host.greet_hint(user, info)
            if facts := mem.recall(text):
                hint += "；可参考已审核的知识：" + " | ".join(facts)
            out = await brain.reply(user, text, hint)
            mem.note_topic(ev["uid"], text[:12])

    said = out.get("say", "") + "".join(out.get("poem", {}).get("lines", []))
    cat = safety.check_output(said) or (kind == "product" and host.ad_check(said) and "广告法用语")
    if cat:
        await broadcast(consoles, {"type": "log", "level": "block", "text": f"[拦截输出/{cat}] {out.get('say')}"})
        out = {"say": "嗯…这个问题佳垚换个话题聊吧～", "action": "think"}

    item = {"id": next(ids), "user": "" if synthetic else user, "text": text, "out": out, "kind": kind}
    if state["mode"] == "review" or kind == "product":   # 带货讲解强制人工审核
        pending[item["id"]] = item
        await broadcast(consoles, {"type": "pending", **{k: item[k] for k in ("id", "user", "text", "out", "kind")}})
        await status()
    else:
        await perform(item)


async def proactive():
    """冷场暖场 / 点名 / 节目效果结算。"""
    while True:
        await asyncio.sleep(3)
        mem.close_window()
        if state["mode"] == "paused" or not operator_alive() or not queue.empty() or pending:
            continue
        if nxt := host.next_action():
            kind, prompt = nxt
            await queue.put({"kind": kind, "uid": "__host__", "user": "佳垚", "text": prompt})


async def worker():
    while True:
        ev = await queue.get()
        # 合规关键点：没有真人操作员在线时，机器人不自动回复（防止“无人直播”）
        if state["mode"] == "paused" or not operator_alive():
            continue
        try:
            await handle(ev)
        except Exception as e:
            print("[worker] error:", e)
        await asyncio.sleep(0.3)


async def watchdog():
    last = None
    while True:
        alive = operator_alive()
        if alive != last:
            await status()
            last = alive
        await asyncio.sleep(2)


@app.on_event("startup")
async def _startup():
    asyncio.create_task(worker())
    asyncio.create_task(watchdog())
    asyncio.create_task(proactive())
    print(f"LLM: {'在线 ' + brain.model if brain.online else '离线演示大脑'} | 模式: {state['mode']}")
    print("overlay: http://127.0.0.1:8765/overlay   控制台: http://127.0.0.1:8765/console")


async def enqueue(ev: dict) -> bool:
    if not ev.get("text"):
        return False
    # 操作员注入(op-)不限流；观众弹幕限流+去重
    if ev["kind"] == "comment" and not ev["uid"].startswith("op-") and not limiter.allow(ev["uid"], ev["text"]):
        return False
    try:
        queue.put_nowait(ev)
        return True
    except asyncio.QueueFull:
        return False


@app.get("/overlay")
def overlay():
    return FileResponse(ROOT / "web" / "overlay.html")


@app.get("/console")
def console():
    return FileResponse(ROOT / "web" / "console.html")


@app.post("/api/danmaku")
async def api_danmaku(req: Request):
    """通用入口：本地测试、粉丝群机器人(官方接口)等都可以往这里 POST {user, text}。"""
    d = await req.json()
    ok = await enqueue({"kind": d.get("kind", "comment"), "uid": d.get("uid", d.get("user", "?")),
                        "user": d.get("user", "观众"), "text": d.get("text", "")})
    return {"queued": ok}


@app.post("/douyin/push")
async def douyin_push(req: Request):
    body = await req.body()
    headers = {k.lower(): v for k, v in req.headers.items()}
    if not douyin.verify(headers, body):
        return JSONResponse({"err": "bad signature"}, status_code=401)
    for ev in douyin.normalize(headers, body):
        await enqueue(ev)
    return {"err_no": 0}


@app.websocket("/ws/overlay")
async def ws_overlay(ws: WebSocket):
    await ws.accept()
    overlays.add(ws)
    await status()
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        overlays.discard(ws)


async def send_learning(ws):
    await ws.send_json({"type": "learning", "pending": mem.pending_knowledge(),
                        "programs": mem.program_stats(), "hot": mem.hot_questions(),
                        "products": [p["name"] for p in host.products],
                        "segment": host.current_segment()["name"]})


@app.websocket("/ws/console")
async def ws_console(ws: WebSocket):
    await ws.accept()
    consoles.add(ws)
    state["operator_seen"] = time.time()
    await status()
    await send_learning(ws)
    try:
        while True:
            m = await ws.receive_json()
            state["operator_seen"] = time.time()
            t = m.get("type")
            if t == "mode":
                state["mode"] = m["mode"]
            elif t == "approve" and (it := pending.pop(m["id"], None)):
                if m.get("say") and m["say"] != it["out"].get("say") and it["user"]:
                    # 操作员改过的回答 = 更好的答案 → 进入候选知识（仍需在“学习”面板批准）
                    mem.propose(it["text"], m["say"], "操作员修改")
                    it["out"]["say"] = m["say"]
                    await send_learning(ws)
                await perform(it)
            elif t == "reject":
                pending.pop(m["id"], None)
            elif t == "inject":
                await enqueue({"kind": "comment", "uid": "op-" + m.get("user", "测试观众"),
                               "user": m.get("user", "测试观众"), "text": m["text"]})
            elif t == "teach":
                mem.propose(m["q"], m["a"], "操作员手动教学")
                await send_learning(ws)
            elif t == "review_knowledge":
                mem.review(m["id"], m["ok"], m.get("a"))
                await send_learning(ws)
            elif t == "learning":
                await send_learning(ws)
            elif t == "product":
                if pr := host.product_prompt(int(m.get("idx", 0))):
                    await queue.put({"kind": "product", "uid": "__host__", "user": "佳垚", "text": pr})
            elif t == "stop":      # 一键急停：清空队列与待审，overlay 立即停止
                pending.clear()
                while not queue.empty():
                    queue.get_nowait()
                state["mode"] = "paused"
                await broadcast(overlays, {"type": "stop"})
            if t != "heartbeat":
                await status()
    except WebSocketDisconnect:
        consoles.discard(ws)
        await status()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PORT", "8765")))
