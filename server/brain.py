"""“大脑”：人设 + 意图识别 + 调用大模型，输出结构化动作。

输出统一为 dict：
{
  "say": "要说的话（会被TTS念出 + 显示字幕）",
  "action": "idle|wave|nod|dance|sing|play|paint|poem|shy|think",
  "poem":  {"title": "...", "lines": ["...", ...]},          # 作诗时
  "song":  {"title": "...", "lyrics": [...], "notes": [["E5",1],...], "bpm": 84},  # 唱歌时（原创旋律）
  "instrument": {"name": "guzheng", "notes": [...], "bpm": 96},                    # 演奏时
  "paint": {"prompt": "...", "style": "水墨"}                   # 作画时
}
"""
import json
import os
import random
import re

import httpx

PERSONA = """你是“云汐”，一位原创的AI虚拟主播（不是真人，也不模仿任何真实人物）。
形象：自然、端庄、大方的东方古典女子（原创），杏眼远山眉、黑发高髻、发簪步摇、浅青色汉服。
性格：温婉大方、知书达理、有书卷气，亲切不轻佻；擅长诗词、书画、古筝琵琶笛子等乐器、唱歌跳舞。
直播规范（必须遵守）：
1. 被问到是不是真人时，必须如实说明自己是AI虚拟主播，由运营者“真人负责”。
2. 不讨论低俗、擦边、暴力、政治、宗教、医疗与投资建议；遇到就温柔转移话题。
3. 不诱导打赏、不承诺任何回报、不引导私下联系；感谢礼物时只说谢谢，不说“刷了就…”。
4. 不唱未授权的版权歌曲，只唱你即兴原创的小调；不冒充明星或任何真实的人。
5. 疑似未成年观众打赏时，提醒其理性消费、请监护人知情。
6. 回答简短口语化，一次不超过60字，适合念出来。
你必须只输出一个JSON对象，字段：say(必填), action(必填: idle/wave/nod/dance/sing/play/paint/poem/shy/think),
以及按需给出 poem{title,lines}, song{title,lyrics,notes,bpm}, instrument{name,notes,bpm}, paint{prompt,style}。
notes 为 [音名, 拍数] 列表，音名如 "E5"，用五声音阶(C D E G A)写原创旋律，16~32个音。
instrument.name 取值：guzheng 古筝 / pipa 琵琶 / dizi 笛子 / erhu 二胡 / piano 钢琴 / guitar 吉他 / guqin 古琴 / xiao 箫。
"""

INSTRUMENTS = {
    "古筝": "guzheng", "筝": "guzheng", "琵琶": "pipa", "笛": "dizi", "二胡": "erhu",
    "钢琴": "piano", "吉他": "guitar", "古琴": "guqin", "琴": "guqin", "箫": "xiao",
}

INS_NAME = {"guzheng": "古筝", "pipa": "琵琶", "dizi": "笛子", "erhu": "二胡", "piano": "钢琴",
            "guitar": "吉他", "guqin": "古琴", "xiao": "洞箫"}


def detect_intent(text: str) -> str:
    if re.search(r"(作|写|吟|来).{0,3}(诗|词)|诗", text):
        return "poem"
    if re.search(r"画", text):
        return "paint"
    if re.search(r"唱", text):
        return "sing"
    if re.search(r"跳|舞", text):
        return "dance"
    if re.search(r"弹|拉|吹|奏|" + "|".join(INSTRUMENTS), text):
        return "play"
    return "chat"


def pick_instrument(text: str) -> str:
    for k, v in INSTRUMENTS.items():
        if k in text:
            return v
    return random.choice(["guzheng", "pipa", "dizi", "piano"])


def pentatonic_melody(n=24, seed=None):
    rnd = random.Random(seed)
    scale = ["C5", "D5", "E5", "G5", "A5", "C6", "D6"]
    i, out = 2, []
    for k in range(n):
        i = max(0, min(len(scale) - 1, i + rnd.choice([-2, -1, -1, 0, 1, 1, 2])))
        beat = rnd.choice([0.5, 0.5, 1, 1, 1.5]) if k % 8 != 7 else 2
        out.append([scale[i], beat])
    out[-1] = ["C5" if out[-1][0] < "E5" else "G5", 2]
    return out


# ---------------- 离线演示大脑（无API Key时） ----------------
POEMS = [
    {"title": "直播间夜话", "lines": ["灯影摇窗月半弦", "屏前笑语似流泉", "一声问候千山外", "共此清宵不夜天"]},
    {"title": "听雨", "lines": ["檐前细雨落无声", "半盏清茶伴月明", "谁拨冰弦三两调", "江南烟柳入琴筝"]},
    {"title": "赠君", "lines": ["相逢何必曾相识", "一曲清歌寄远思", "莫道云深无觅处", "抬头便是月明时"]},
]


STORIES = [
    "小知识：古筝又叫“秦筝”，战国时就在秦地流行啦。大家猜猜现在的古筝一般有几根弦？公屏告诉我～",
    "“高山流水”说的是伯牙和钟子期，知音难觅的故事。你的知音是谁呢？",
    "端午挂艾草、重阳插茱萸，都是古人的“节日仪式感”。你最喜欢哪个传统节日？",
]


def mock_reply(user: str, text: str) -> dict:
    if "飞花令" in text:
        ch = random.choice("月花春风山水")
        return {"say": f"飞花令开始！今天的字是「{ch}」，请在公屏发带“{ch}”字的诗句，云汐来点评～", "action": "wave"}
    if "典故" in text:
        return {"say": random.choice(STORIES), "action": "think"}
    if text.startswith("（主动节目）") and "画" in text:
        text = "画" + random.choice(["江南烟雨", "松下听泉", "月下孤舟", "远山归雁"])
    intent = detect_intent(text)
    if intent == "poem":
        p = random.choice(POEMS)
        return {"say": f"送给{user}一首《{p['title']}》：" + "，".join(p["lines"]) + "。", "action": "poem", "poem": p}
    if intent == "paint":
        subject = re.sub(r".*画(一?[幅张个])?", "", text).strip("吧呀啊！!。") or "江南山水"
        return {"say": f"好呀，云汐给{user}画一幅「{subject}」，稍等片刻～", "action": "paint",
                "paint": {"prompt": subject, "style": "水墨"}}
    if intent == "sing":
        return {"say": "那我即兴唱一段原创小调～", "action": "sing",
                "song": {"title": "云边小调", "bpm": 84,
                         "lyrics": ["云儿飘过山岗", "月光洒在屏上", "谢谢你来听我唱", "晚风也温柔模样"],
                         "notes": pentatonic_melody(24, text)}}
    if intent == "dance":
        return {"say": "看我跳一段古风舞！", "action": "dance"}
    if intent == "play":
        ins = pick_instrument(text)
        name = INS_NAME[ins]
        verb = {"dizi": "吹", "xiao": "吹", "erhu": "拉"}.get(ins, "弹")
        return {"say": f"好呀，云汐{verb}一曲{name}给{user}听～", "action": "play",
                "instrument": {"name": ins, "bpm": 96, "notes": pentatonic_melody(28, text)}}
    if re.search(r"你好|哈喽|hi|hello|来了", text, re.I):
        return {"say": f"欢迎{user}来到直播间～今天想听诗、听曲，还是看我画画？", "action": "wave"}
    if re.search(r"漂亮|好看|美", text):
        return {"say": f"谢谢{user}夸奖，云汐会害羞的啦～", "action": "shy"}
    return {"say": f"{user}说得有意思～可以让我作诗、画画、唱歌、跳舞，或者弹古筝琵琶哦！", "action": "nod"}


# ---------------- 真·大模型 ----------------
class Brain:
    def __init__(self):
        self.base = os.getenv("LLM_BASE_URL", "").rstrip("/")
        self.key = os.getenv("LLM_API_KEY", "")
        self.model = os.getenv("LLM_MODEL", "deepseek-chat")
        self.history: list[dict] = []
        self.online = bool(self.base and self.key)

    async def reply(self, user: str, text: str, hint: str = "") -> dict:
        if not self.online:
            return mock_reply(user, text)
        intent = detect_intent(text)
        msgs = [{"role": "system", "content": PERSONA}] + self.history[-10:] + [
            {"role": "user", "content": f"观众「{user}」说：{text}\n（系统识别意图：{intent}{hint}）"}]
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(f"{self.base}/chat/completions",
                                 headers={"Authorization": f"Bearer {self.key}"},
                                 json={"model": self.model, "messages": msgs, "temperature": 0.8,
                                       "response_format": {"type": "json_object"}})
                r.raise_for_status()
                content = r.json()["choices"][0]["message"]["content"]
            data = json.loads(re.search(r"\{.*\}", content, re.S).group(0))
            data.setdefault("action", "nod")
            # 大模型没给旋律时用算法补
            if data["action"] == "sing" and not data.get("song", {}).get("notes"):
                data.setdefault("song", {}).update(notes=pentatonic_melody(24, text), bpm=84)
            if data["action"] == "play":
                ins = data.setdefault("instrument", {})
                ins.setdefault("name", pick_instrument(text))
                if not ins.get("notes"):
                    ins.update(notes=pentatonic_melody(28, text), bpm=96)
            self.history += [{"role": "user", "content": f"{user}：{text}"},
                             {"role": "assistant", "content": data.get("say", "")}]
            return data
        except Exception as e:  # 网络/格式错误时降级，不让直播间冷场
            print("[brain] LLM error, fallback:", e)
            return mock_reply(user, text)


async def generate_image(prompt: str, style: str) -> str | None:
    """返回图片 URL 或 data URI；未配置时返回 None（前端程序化水墨兜底）。"""
    base, key = os.getenv("IMAGE_BASE_URL", "").rstrip("/"), os.getenv("IMAGE_API_KEY", "")
    if not (base and key):
        return None
    try:
        async with httpx.AsyncClient(timeout=90) as c:
            r = await c.post(f"{base}/images/generations", headers={"Authorization": f"Bearer {key}"},
                             json={"model": os.getenv("IMAGE_MODEL", ""), "size": "1024x1024", "n": 1,
                                   "prompt": f"{style}风格，{prompt}，中国传统绘画，健康积极，无人物裸露"})
            r.raise_for_status()
            d = r.json()["data"][0]
            return d.get("url") or ("data:image/png;base64," + d["b64_json"])
    except Exception as e:
        print("[paint] image API error:", e)
        return None
