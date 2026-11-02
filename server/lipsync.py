"""口型同步（写实真人形象用）。

把本句 TTS 语音发给口型同步服务，拿回“这个人物说这句话”的视频，overlay 播放它，
这样写实人物的嘴型和声音是对上的。

推荐自建 MuseTalk（开源，单张 RTX 3090/4090 可实时）或 LatentSync；也可以用商用数字人平台的
“音频驱动”接口。不同服务的接口不一样，这里约定一个最简单的 HTTP 协议，写一个几十行的适配层即可：

  POST {LIPSYNC_URL}
  {"avatar": "<人物ID>", "audio_url": "http://<本机>/audio/12.mp3"}
  → {"video_url": "http://.../12.mp4"}

未配置 LIPSYNC_URL 时返回 None，overlay 改为播放通用的 talk 片段。
"""
import os

import httpx


async def render(audio_path: str) -> str | None:
    url = os.getenv("LIPSYNC_URL", "")
    if not (url and audio_path):
        return None
    base = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8765")
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(url, json={"avatar": os.getenv("LIPSYNC_AVATAR", "jiayao"),
                                        "audio_url": base + audio_path})
            r.raise_for_status()
            return r.json().get("video_url")
    except Exception as e:  # 超时或失败就退回通用说话片段，不影响直播
        print("[lipsync] 失败，改用通用说话片段：", e)
        return None
