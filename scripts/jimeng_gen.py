"""用即梦（火山引擎视觉智能）批量生成佳垚的写实素材。

两步走：
  1) 生成定妆照候选：  python scripts/jimeng_gen.py portraits --n 8
     → 输出到 runtime_gen/portraits/，人工挑一张（并用以图搜图确认不像任何真人）
  2) 用选定的定妆照生成动作片段：
     python scripts/jimeng_gen.py clips --portrait runtime_gen/portraits/p3.png
     → 视频保存到 web/avatar/，并自动写好 web/avatar/clips.json

环境变量：VOLC_ACCESS_KEY / VOLC_SECRET_KEY（火山引擎 AK/SK）
可选：JIMENG_T2I_REQ_KEY（文生图模型，默认 jimeng_t2i_v31）、JIMENG_VIDEO_REQ_KEY（默认 jimeng_ti2v_v30_pro）
加 --dry-run 只打印请求、不调用接口。

⚠️ 接口字段按公开示例整理（视频：jimeng_ti2v_v30_pro，Action=CVSync2AsyncSubmitTask/CVSync2AsyncGetResult，
Version=2022-08-31，签名 region=cn-north-1 service=cv）。文生图的 req_key 和返回字段以控制台文档为准，不对时改环境变量即可。
"""
import argparse
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import pathlib
import sys
import time

import httpx

HOST = "visual.volcengineapi.com"
REGION, SERVICE, VERSION = "cn-north-1", "cv", "2022-08-31"
ROOT = pathlib.Path(__file__).resolve().parent.parent
T2I_KEY = os.getenv("JIMENG_T2I_REQ_KEY", "jimeng_t2i_v31")
VIDEO_KEY = os.getenv("JIMENG_VIDEO_REQ_KEY", "jimeng_ti2v_v30_pro")

PORTRAIT_PROMPT = (
    "写实人像摄影，一位二十多岁的东方女性，五官端正柔和，杏眼，远山眉，自然淡妆，气质温婉端庄大方，"
    "黑色长发盘成发髻并插一支简洁的发簪，身着浅青色交领汉服，半身像，正面微侧，看向镜头浅浅微笑，"
    "柔和自然光，纯绿色背景，皮肤质感真实，85mm 镜头，竖构图"
)

# 动作片段：名称 → (clips.json 里的动作, 提示词)。同一机位、绿幕、首尾姿态接近，便于循环
CLIPS = {
    "idle_1": ("idle", "她安静地站着，轻轻呼吸，偶尔眨眼，看着镜头温柔微笑，镜头固定不动，背景保持纯绿色"),
    "idle_2": ("idle", "她微微侧头整理鬓边的头发，然后回到看向镜头的姿势，镜头固定，纯绿色背景"),
    "idle_3": ("idle", "她双手交叠在身前，目光自然地看向镜头，轻轻点头微笑，镜头固定，纯绿色背景"),
    "talk_1": ("talk", "她面对镜头自然地说话，表情亲切，有轻微的手势，镜头固定，纯绿色背景"),
    "talk_2": ("talk", "她面对镜头微笑着讲述，偶尔点头，手在胸前轻轻比划，镜头固定，纯绿色背景"),
    "wave":   ("wave", "她抬起右手向镜头优雅地挥手打招呼，然后放下手微笑，镜头固定，纯绿色背景"),
    "nod":    ("nod", "她看着镜头认真地点了两下头，表示赞同，镜头固定，纯绿色背景"),
    "shy":    ("shy", "她被夸奖后有些害羞，低头浅笑，用手轻轻掩住嘴角，然后抬头看镜头，镜头固定，纯绿色背景"),
    "think":  ("think", "她微微歪头，右手食指轻点下巴，若有所思，然后露出笑容，镜头固定，纯绿色背景"),
    "poem":   ("poem", "她手持一把团扇，轻轻摇扇，神情悠然地吟诵诗句，镜头固定，纯绿色背景"),
    "sing":   ("sing", "她面对镜头轻声歌唱，神情投入，身体随节奏轻轻摇摆，镜头固定，纯绿色背景"),
    "dance_1": ("dance", "她跳一段优雅的中国古典舞，长袖舒展，转身，动作柔美流畅，全身入镜，镜头固定，纯绿色背景"),
    "dance_2": ("dance", "她跳古典舞，水袖翻飞，脚步轻盈，最后回到站立姿势，全身入镜，镜头固定，纯绿色背景"),
    "guzheng": ("play", "她坐在古筝前，双手在琴弦上弹奏，手指动作自然，神情专注，镜头固定，纯绿色背景"),
    "pipa":    ("play", "她坐着怀抱琵琶弹奏，右手轮指，左手按弦，神情专注，镜头固定，纯绿色背景"),
    "dizi":    ("play", "她横持竹笛吹奏，手指在笛孔上起落，神情悠扬，镜头固定，纯绿色背景"),
    "paint":   ("paint", "她站在书案前，手执毛笔在宣纸上作画，动作从容，偶尔抬头看镜头，镜头固定，纯绿色背景"),
}


# ---------------- 火山引擎 V4 签名 ----------------
def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sign_headers(action: str, body: bytes, ak: str, sk: str, now: dt.datetime | None = None) -> tuple[str, dict]:
    now = now or dt.datetime.now(dt.timezone.utc)
    x_date, short = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    query = f"Action={action}&Version={VERSION}"
    payload_hash = hashlib.sha256(body).hexdigest()
    headers = {"content-type": "application/json", "host": HOST, "x-content-sha256": payload_hash, "x-date": x_date}
    signed = ";".join(sorted(headers))
    canonical = "\n".join(["POST", "/", query, "".join(f"{k}:{headers[k]}\n" for k in sorted(headers)), signed, payload_hash])
    scope = f"{short}/{REGION}/{SERVICE}/request"
    to_sign = "\n".join(["HMAC-SHA256", x_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    k = _hmac(_hmac(_hmac(_hmac(sk.encode(), short), REGION), SERVICE), "request")
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    out = {"Content-Type": headers["content-type"], "Host": HOST, "X-Content-Sha256": payload_hash, "X-Date": x_date,
           "Authorization": f"HMAC-SHA256 Credential={ak}/{scope}, SignedHeaders={signed}, Signature={sig}"}
    return f"https://{HOST}/?{query}", out


class Jimeng:
    def __init__(self, dry=False):
        self.ak, self.sk, self.dry = os.getenv("VOLC_ACCESS_KEY", ""), os.getenv("VOLC_SECRET_KEY", ""), dry
        if not dry and not (self.ak and self.sk):
            sys.exit("缺少 VOLC_ACCESS_KEY / VOLC_SECRET_KEY 环境变量")
        self.http = httpx.Client(timeout=60)

    def call(self, action: str, payload: dict) -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode()
        url, headers = sign_headers(action, body, self.ak or "AK", self.sk or "SK")
        if self.dry:
            shown = {k: (str(v)[:60] + "…" if len(str(v)) > 80 else v) for k, v in payload.items()}
            print("DRY", action, json.dumps(shown, ensure_ascii=False)[:300])
            return {"code": 10000, "data": {"task_id": "dry", "status": "done"}}
        r = self.http.post(url, headers=headers, content=body)
        data = r.json()
        if r.status_code != 200 or data.get("code") not in (10000, 0):
            raise RuntimeError(f"{action} 失败 HTTP {r.status_code}: {json.dumps(data, ensure_ascii=False)[:500]}")
        return data

    def run(self, req: dict, timeout=900) -> dict:
        task = self.call("CVSync2AsyncSubmitTask", req)["data"]["task_id"]
        start = time.time()
        while time.time() - start < timeout:
            d = self.call("CVSync2AsyncGetResult", {"req_key": req["req_key"], "task_id": task,
                                                     "req_json": json.dumps({"return_url": True})})["data"]
            st = d.get("status")
            if st in ("done", "success"):
                return d
            if st in ("failed", "error", "not_found", "expired"):
                raise RuntimeError(f"任务 {task} 状态 {st}: {d}")
            time.sleep(5)
        raise TimeoutError(f"任务 {task} 超时")

    def save(self, url_or_b64: str, path: pathlib.Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.dry:
            return print("DRY save", path)
        if url_or_b64.startswith("http"):
            r = self.http.get(url_or_b64, timeout=300)
            r.raise_for_status()  # 下载失败时报错，不覆盖已有的好文件
            data = r.content
        else:
            data = base64.b64decode(url_or_b64)
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(path)
        print("已保存", path)


def portraits(j: Jimeng, n: int):
    out = ROOT / "runtime_gen" / "portraits"
    for i in range(1, n + 1):
        d = j.run({"req_key": T2I_KEY, "prompt": PORTRAIT_PROMPT, "seed": -1, "width": 1080, "height": 1920,
                   "return_url": True})
        src = (d.get("image_urls") or [None])[0] or (d.get("binary_data_base64") or [None])[0]
        if j.dry:
            src = "dry"
        if not src:
            raise RuntimeError(f"文生图返回里没找到图片字段：{list(d)}")
        j.save(src, out / f"p{i}.png")
    print(f"\n挑一张最满意的，先以图搜图确认不像任何真人，再运行：python scripts/jimeng_gen.py clips --portrait {out}/p?.png")


def clips(j: Jimeng, portrait: str, only: list[str] | None, frames: int):
    p = pathlib.Path(portrait)
    img = {"image_urls": [portrait]} if portrait.startswith("http") else \
          {"binary_data_base64": [base64.b64encode(p.read_bytes()).decode() if p.exists() or not j.dry else ""]}
    avatar = ROOT / "web" / "avatar"
    manifest_path = avatar / "clips.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    for name, (action, prompt) in CLIPS.items():
        if only and name not in only:
            continue
        try:
            d = j.run({"req_key": VIDEO_KEY, "prompt": prompt, "frames": frames, "seed": -1, **img})
            url = "dry" if j.dry else d.get("video_url") or (d.get("video_urls") or [None])[0]
            if not url:
                raise RuntimeError(f"返回里没找到视频字段：{list(d)}")
            j.save(url, avatar / f"{name}.mp4")
            files = manifest.setdefault(action, [])
            if f"{name}.mp4" not in files:
                files.append(f"{name}.mp4")
        except Exception as e:  # 单条失败不影响其他片段
            print(f"[{name}] 失败：{e}")
    if not j.dry:
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        print("已更新", manifest_path)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["portraits", "clips"])
    ap.add_argument("--n", type=int, default=8, help="定妆照候选张数")
    ap.add_argument("--portrait", help="选定的定妆照（本地路径或公网 URL）")
    ap.add_argument("--only", nargs="*", help="只生成这些片段，如 --only idle_1 dance_1")
    ap.add_argument("--frames", type=int, default=121, help="121≈5秒，241≈10秒")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    jm = Jimeng(a.dry_run)
    if a.step == "portraits":
        portraits(jm, a.n)
    else:
        if not a.portrait:
            sys.exit("请用 --portrait 指定定妆照")
        clips(jm, a.portrait, a.only, a.frames)
