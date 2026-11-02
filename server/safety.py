"""内容安全：输入过滤 + 输出过滤 + 频率限制。

这是第一道“本地”防线，上线时还应叠加：
  1. 大模型厂商自带的内容安全（国内备案模型均有）；
  2. 阿里云/腾讯云/数美等内容审核 API（可在 moderate_remote 里接入）；
  3. 操作员人工复核（REPLY_MODE=review）。
词表只放了示例，运营时请按抖音《社区自律公约》持续补充。
"""
import re
import time
from collections import defaultdict, deque

# 低俗 / 擦边 / 引流 / 诱导打赏 / 未成年 等示例词（可按需扩充，支持正则）
BLOCK_PATTERNS = {
    "低俗擦边": [r"脱", r"胸", r"腿.{0,2}(照|图)", r"性感", r"色色", r"开车", r"福利姬", r"内衣", r"约(炮|吗)", r"裸", r"床"],
    "引流私下交易": [r"加.{0,3}(微信|vx|v信|qq|扣扣)", r"私聊", r"二维码", r"转账", r"线下见"],
    "诱导打赏": [r"刷.{0,2}(礼物|火箭|嘉年华).{0,4}(就|才)", r"不刷.{0,3}不"],
    "辱骂攻击": [r"傻[逼bB比]", r"去死", r"废物", r"滚"],
    "冒充真人": [r"你是真人", r"露脸", r"真人出镜"],
}

# 机器人自己的输出更严格：不能出现诱导打赏、承诺、医疗/投资建议等
OUTPUT_EXTRA = {
    "诱导打赏": [r"(刷|送).{0,3}(礼物|火箭|嘉年华)", r"打赏", r"榜一"],
    "违规承诺": [r"保(证|你)(赚|收益)", r"稳赚", r"包治"],
}

_compiled_in = {k: [re.compile(p, re.I) for p in v] for k, v in BLOCK_PATTERNS.items()}
_compiled_out = {**_compiled_in, **{k: [re.compile(p, re.I) for p in v] for k, v in OUTPUT_EXTRA.items()}}


def _hit(text: str, table) -> str | None:
    t = re.sub(r"[\s\*\-_.。·]", "", text)  # 去掉插空格、插符号的规避写法
    for cat, pats in table.items():
        for p in pats:
            if p.search(t):
                return cat
    return None


def check_input(text: str) -> str | None:
    """返回命中的类别；None 表示通过。"""
    if cat := _hit(text, _compiled_in):
        # “你是真人吗”不屏蔽，而是要求如实回答“我是AI”
        return None if cat == "冒充真人" else cat
    return None


def is_identity_question(text: str) -> bool:
    return bool(re.search(r"(你是|是不是).{0,3}(真人|人类|机器人|AI|ai)", text))


def check_output(text: str) -> str | None:
    return _hit(text, _compiled_out)


SAFE_DEFLECT = {
    "低俗擦边": "这个话题云汐就不接啦～我们聊聊诗词、音乐或者画画好不好？",
    "引流私下交易": "直播间不支持私下联系哦，有问题在公屏留言就好～",
    "诱导打赏": "不用破费呀，陪云汐聊天就很开心啦～",
    "辱骂攻击": "大家友善发言哦，我们一起维护好直播间氛围～",
}


class RateLimiter:
    """同一用户 N 秒内最多 M 条；全局去重相同内容。"""

    def __init__(self, per_user=2, window=20):
        self.per_user, self.window = per_user, window
        self.hist = defaultdict(deque)
        self.recent = deque(maxlen=50)

    def allow(self, uid: str, text: str) -> bool:
        now = time.time()
        q = self.hist[uid]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.per_user or text in self.recent:
            return False
        q.append(now)
        self.recent.append(text)
        return True
