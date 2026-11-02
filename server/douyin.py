"""抖音开放平台「直播互动」数据推送的接入层（官方、合规的弹幕来源）。

流程（需在 https://developer.open-douyin.com 申请）：
  1. 企业/个体主体注册开发者 → 创建「直播小玩法 / 直播互动」应用 → 通过审核；
  2. 拿到 app_id / app_secret，在后台配置“数据推送回调地址”= https://你的域名/douyin/push；
  3. 开播后调用“启动任务”接口订阅 live_comment(评论) / live_gift(礼物) / live_like(点赞)；
  4. 平台把消息 POST 到回调地址，本模块负责验签 + 统一成 {user, text, kind}。

⚠️ 签名算法与字段名请以开放平台最新文档为准，下面按公开文档的常见格式实现，上线前务必核对。
不要使用逆向抖音网页/APP 的 WebSocket 抓弹幕：违反《抖音用户服务协议》，可能被封号并承担法律风险。
"""
import base64
import hashlib
import json
import os


def verify(headers: dict, body: bytes) -> bool:
    if os.getenv("DOUYIN_SKIP_VERIFY", "0") == "1":
        return True
    secret = os.getenv("DOUYIN_APP_SECRET", "")
    if not secret:
        return False
    keys = ["x-nonce-str", "x-timestamp", "x-roomid", "x-msg-type"]
    params = "&".join(f"{k}={headers.get(k, '')}" for k in sorted(keys))
    raw = params + body.decode("utf-8") + secret
    sign = base64.b64encode(hashlib.md5(raw.encode()).digest()).decode()
    return sign == headers.get("x-signature", "")


def normalize(headers: dict, body: bytes) -> list[dict]:
    kind = headers.get("x-msg-type", "live_comment")
    try:
        items = json.loads(body)
    except Exception:
        return []
    if isinstance(items, dict):
        items = [items]
    out = []
    for it in items:
        user = it.get("nickname") or "观众"
        uid = it.get("sec_openid") or user
        if kind == "live_comment":
            out.append({"kind": "comment", "uid": uid, "user": user, "text": it.get("content", "")})
        elif kind == "live_gift":
            out.append({"kind": "gift", "uid": uid, "user": user,
                        "text": f"送出了{it.get('gift_num', 1)}个礼物", "gift": it.get("sec_gift_id")})
        elif kind == "live_like":
            out.append({"kind": "like", "uid": uid, "user": user, "text": "点了赞"})
    return out
