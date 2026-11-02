"""自主学习层（SQLite 持久化，重启不丢）。

三件事：
1. 观众长期记忆：昵称、来访次数、聊过的话题、喜好 → 老粉回来时能“认出来”。
2. 节目自优化（多臂老虎机 / Thompson Sampling）：每次表演后统计接下来 60 秒的
   弹幕数、点赞、新进观众，作为该节目的“奖励”，冷场时自动挑最受欢迎的节目，
   同时保留一定探索，避免只会一招。
3. 知识/话术学习：从弹幕里高频出现但答不好的问题、操作员手动修改过的回复中
   提取“候选知识”，**必须由操作员在控制台批准后**才进入知识库并影响回答。
   （防止被观众“教坏”，也符合平台对生成内容可控的要求）
"""
import json
import random
import sqlite3
import time
from collections import Counter

PROGRAMS = ["poem", "sing", "dance", "play_guzheng", "play_pipa", "play_dizi", "paint", "story", "quiz"]


class Memory:
    def __init__(self, path="runtime_memory.db"):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript("""
        create table if not exists viewer(uid text primary key, name text, visits int, first real, last real,
                                          topics text default '[]', gifts int default 0);
        create table if not exists program(name text primary key, wins real, trials real);
        create table if not exists knowledge(id integer primary key, q text, a text, status text, src text, ts real);
        create table if not exists question_log(q text, ts real);
        """)
        for p in PROGRAMS:
            self.db.execute("insert or ignore into program values(?,1,2)", (p,))
        self.db.commit()
        self.window: dict | None = None  # 正在统计效果的节目
        self.counters = Counter()

    # ---------- 观众记忆 ----------
    def see(self, uid: str, name: str) -> dict:
        now = time.time()
        row = self.db.execute("select visits,last,topics,gifts from viewer where uid=?", (uid,)).fetchone()
        if not row:
            self.db.execute("insert into viewer values(?,?,1,?,?,'[]',0)", (uid, name, now, now))
            self.db.commit()
            return {"new": True, "visits": 1, "topics": [], "returning": False}
        visits, last, topics, gifts = row
        returning = now - last > 6 * 3600  # 隔了一段时间再来 = 回访
        self.db.execute("update viewer set name=?, visits=visits+?, last=? where uid=?",
                        (name, 1 if returning else 0, now, uid))
        self.db.commit()
        return {"new": False, "visits": visits + returning, "topics": json.loads(topics),
                "returning": returning, "gifts": gifts}

    def note_topic(self, uid: str, topic: str):
        row = self.db.execute("select topics from viewer where uid=?", (uid,)).fetchone()
        if row:
            t = [x for x in json.loads(row[0]) if x != topic][-7:] + [topic]
            self.db.execute("update viewer set topics=? where uid=?", (json.dumps(t, ensure_ascii=False), uid))
            self.db.commit()

    def note_gift(self, uid: str):
        self.db.execute("update viewer set gifts=gifts+1 where uid=?", (uid,))
        self.db.commit()

    # ---------- 节目自优化 ----------
    def pick_program(self) -> str:
        rows = self.db.execute("select name,wins,trials from program").fetchall()
        return max(rows, key=lambda r: random.betavariate(r[1], max(1e-3, r[2] - r[1] + 1)))[0]

    def start_window(self, program: str):
        self.close_window()
        self.window = {"p": program, "t": time.time()}
        self.counters.clear()

    def count(self, kind: str):
        if self.window:
            self.counters[kind] += 1

    def close_window(self, force=False):
        w = self.window
        if not w or (not force and time.time() - w["t"] < 60):
            return
        score = self.counters["comment"] + 0.2 * self.counters["like"] + 2 * self.counters["enter"]
        win = 1.0 if score >= 5 else score / 5  # 归一化到 0~1
        self.db.execute("update program set wins=wins+?, trials=trials+1 where name=?", (win, w["p"]))
        self.db.commit()
        self.window = None

    def program_stats(self):
        return [{"name": n, "rate": round(w / t, 2), "trials": int(t - 2)}
                for n, w, t in self.db.execute("select name,wins,trials from program order by wins/trials desc")]

    # ---------- 知识学习（人工审核） ----------
    def log_question(self, q: str):
        self.db.execute("insert into question_log values(?,?)", (q, time.time()))
        self.db.commit()

    def propose(self, q: str, a: str, src: str) -> int:
        cur = self.db.execute("insert into knowledge(q,a,status,src,ts) values(?,?,'pending',?,?)", (q, a, src, time.time()))
        self.db.commit()
        return cur.lastrowid

    def review(self, kid: int, ok: bool, a: str | None = None):
        if a:
            self.db.execute("update knowledge set a=? where id=?", (a, kid))
        self.db.execute("update knowledge set status=? where id=?", ("approved" if ok else "rejected", kid))
        self.db.commit()

    def pending_knowledge(self):
        return [dict(zip(("id", "q", "a", "src"), r)) for r in
                self.db.execute("select id,q,a,src from knowledge where status='pending' order by id desc limit 20")]

    def recall(self, text: str, k=3) -> list[str]:
        """极简检索：按字重叠打分。规模大了可换成向量库（bge-m3 + faiss/chroma）。"""
        rows = self.db.execute("select q,a from knowledge where status='approved'").fetchall()
        s = set(text)
        scored = sorted(rows, key=lambda r: -len(s & set(r[0])))
        return [f"问：{q} 答：{a}" for q, a in scored[:k] if len(s & set(q)) >= 2]

    def hot_questions(self, hours=24, top=10):
        since = time.time() - hours * 3600
        qs = [r[0] for r in self.db.execute("select q from question_log where ts>?", (since,))]
        return Counter(qs).most_common(top)
