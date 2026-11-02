"""主播技巧层：把头部主播的“通用”控场方法做成策略（不复制任何具体主播的人设、口头禅、形象或声音）。

覆盖：欢迎新人 / 认出老粉 / 感谢礼物（不诱导）/ 点名互动 / 冷场暖场 / 节目单节奏 /
     互动小游戏（飞花令、猜诗句）/ 带货讲解（可选，强制人工审核）/ 下播收尾。
PK、连麦需要真人在抖音 APP 里操作，这里只提供话术提示给操作员，不做自动化。
"""
import json
import pathlib
import random
import re
import time

# 广告法禁用的绝对化用语 + 医疗功效用语（带货讲解时强制检查）
AD_BANNED = r"最[好佳强低高便]|第一|顶级|极品|国家级|全网最|100%|根治|治愈|药到病除|无副作用|秒杀全网"

PROGRAM_PROMPTS = {
    "poem": "（主动节目）以当下季节或直播间氛围为题，即兴作一首七言绝句。",
    "sing": "（主动节目）即兴唱一段原创小调，歌词温暖积极。",
    "dance": "（主动节目）跳一段古风舞，跳前说一句开场白。",
    "play_guzheng": "（主动节目）弹一段古筝原创小曲。",
    "play_pipa": "（主动节目）弹一段琵琶原创小曲。",
    "play_dizi": "（主动节目）吹一段笛子原创小曲。",
    "paint": "（主动节目）画一幅水墨小品，先说要画什么。",
    "story": "（主动节目）讲一个30秒的中国传统文化小知识或典故，结尾抛一个问题让观众在公屏回答。",
    "quiz": "（主动节目）发起飞花令小游戏：给出一个字，请观众在公屏发含这个字的诗句，你来点评。",
}

ROLLCALL = [
    "{u}，你刚才说得很有意思，能再多讲讲吗？",
    "点名{u}！你最喜欢哪种乐器呀？",
    "{u}今天过得怎么样？在公屏告诉云汐吧～",
]

WELCOME_NEW = ["欢迎{u}第一次来！我是AI虚拟主播云汐，可以点歌、点诗、点画哦～",
               "{u}你好呀～新朋友可以在公屏打“作诗”“画画”“弹古筝”试试！"]
WELCOME_BACK = ["{u}回来啦！上次我们聊到{t}，还记得吗？", "欢迎老朋友{u}，第{n}次来看云汐啦～"]


class Host:
    def __init__(self, mem, idle_after=25, rollcall_every=180):
        self.mem = mem
        self.idle_after, self.rollcall_every = idle_after, rollcall_every
        self.last_activity = time.time()
        self.last_rollcall = time.time()
        self.active_users: dict[str, float] = {}
        self.rundown = self._load("rundown.json", [
            {"name": "开场", "minutes": 3, "programs": ["poem"]},
            {"name": "才艺", "minutes": 20, "programs": ["sing", "dance", "play_guzheng", "play_pipa", "play_dizi"]},
            {"name": "互动", "minutes": 15, "programs": ["quiz", "story", "paint"]},
        ])
        self.products = self._load("products.json", [])
        self.started = time.time()

    @staticmethod
    def _load(name, default):
        p = pathlib.Path(name)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default

    def touch(self, uid=None, name=None):
        self.last_activity = time.time()
        if uid:
            self.active_users[name or uid] = time.time()

    # 第一句话前加上“欢迎/认出老粉”的提示，交给大模型自然地融进回答
    def greet_hint(self, user: str, info: dict) -> str:
        if info["new"]:
            return "；这是新观众第一次发言，回答时顺带欢迎TA"
        if info["returning"]:
            t = info["topics"][-1] if info["topics"] else ""
            return f"；这是第{info['visits']}次来的老观众" + (f"，上次聊过「{t}」，可以自然提一句" if t else "")
        return ""

    def gift_thanks(self, user: str, info: dict) -> str:
        base = random.choice(["谢谢{u}的礼物，心意云汐收到啦～", "感谢{u}！"]).format(u=user)
        return base + "大家量力而行、理性消费，陪伴就是最好的支持哦。"

    def current_segment(self):
        t = (time.time() - self.started) / 60
        for seg in self.rundown:
            if t < seg["minutes"]:
                return seg
            t -= seg["minutes"]
        return self.rundown[-1]

    def next_action(self):
        """返回 None 或 (kind, prompt)。由中控每几秒调用一次。"""
        now = time.time()
        recent = [u for u, ts in self.active_users.items() if now - ts < 120]
        if recent and now - self.last_rollcall > self.rollcall_every:
            self.last_rollcall = now
            return "rollcall", random.choice(ROLLCALL).format(u=random.choice(recent))
        if now - self.last_activity > self.idle_after:
            self.last_activity = now
            seg = self.current_segment()
            # 在当前环节允许的节目里，用学习到的受欢迎程度挑
            for _ in range(5):
                p = self.mem.pick_program()
                if p in seg["programs"]:
                    break
            else:
                p = random.choice(seg["programs"])
            return "program:" + p, PROGRAM_PROMPTS[p]
        return None

    # ---------- 带货讲解（可选） ----------
    def product_prompt(self, idx: int) -> str | None:
        if not 0 <= idx < len(self.products):
            return None
        p = self.products[idx]
        return ("（带货讲解，需人工审核）请客观介绍商品："
                f"{p['name']}，卖点：{'；'.join(p.get('points', []))}，价格以小黄车页面为准。"
                "禁止使用绝对化用语、禁止功效承诺、禁止虚构库存或限时，结尾提醒按需购买。")

    @staticmethod
    def ad_check(text: str) -> str | None:
        m = re.search(AD_BANNED, text)
        return m.group(0) if m else None
