# -*- coding: utf-8 -*-

# 出处：astrbot_plugin_memories（原作者：若剑清，MIT License）
# 本项目在原作者 v0.5.1 的基础上做了修改（记忆注入、日记总结、语言过滤等，详见 THIRD-PARTY.md）。
# 修改者：XiaoJing contributors

"""PET3:convstate —— 对话状态层。

目的：把「他答过什么」「她刚才说过什么」「现在对话里有几个人」这类
每轮都必须知道、但现在散落在七八个临时块里的东西，收敛成一个状态块。

全部本地计算，0 token。数据落在 data_dir/conv_state.json。
"""
from __future__ import annotations

import datetime
import json
import os
import random
import re
import time

ANSWER_TTL = 6 * 3600
FACT_MAX = 40

# 会被反复问到的「事」
TOPICS = (
    "头疼", "头痛", "头晕", "体育课", "上课", "下课", "考试", "作业", "比赛",
    "训练", "午饭", "晚饭", "早饭", "吃饭", "睡觉", "熬夜", "感冒", "发烧",
    "牙", "胃", "眼睛", "脖子", "腰", "论文", "项目", "答辩", "面试",
    "兼职", "实习", "某个地点", "宿舍", "教室", "回家", "生日", "洗澡",
)

# 「已经答过 / 已经解决」的说法
_STATUS = re.compile(
    r"(好了|好差不多|差不多好|好很多|好多了|不疼了|不难受了|缓过来了|没事了|"
    r"吃过了|吃完了|已经吃了|下课了|上完课|做完了|写完了|考完了|忙完了|"
    r"到了|到家了|睡醒了|已经睡了|请过假了|不去了|取消了)"
)
_NEG_STATUS = re.compile(r"(没好|还疼|还难受|更疼|还是有点疼|又疼)")

_PRONOUN_RULE = (
    "现在这段对话里只有你和他两个人。他说的「你」就是你；"
    "你称呼他只用「你」，不要用「你们」——「你们」是两个人以上的说法。"
)

# 说法归一：把「头还疼」「吃过了」这类换成台账里的话题词
_ALIASES = (
    ("头还疼", "头疼"), ("头有点疼", "头疼"), ("头痛", "头疼"), ("头不疼", "头疼"),
    ("吃过了", "吃饭"), ("吃完饭", "吃饭"), ("已经吃了", "吃饭"), ("吃过饭", "吃饭"),
    ("下完课", "下课"), ("上完课", "下课"),
    ("睡醒了", "睡觉"), ("醒了", "睡觉"),
)


def _normalize(text: str) -> str:
    out = str(text or "")
    for a, b in _ALIASES:
        out = out.replace(a, b)
    return out


def _now() -> float:
    return time.time()


def _hm(ts: float) -> str:
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%H:%M")
    except Exception:
        return "--:--"


class ConvState:
    """极小的本地状态库：facts + 素材使用记录。"""

    def __init__(self, path: str):
        self.path = path
        self.data = {"facts": [], "materials": []}
        self.load()

    # ---------- 存取 ----------
    def load(self) -> None:
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    self.data["facts"] = list(d.get("facts") or [])
                    self.data["materials"] = list(d.get("materials") or [])
        except Exception:
            pass

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except Exception:
            pass

    # ---------- 事实 ----------
    def note_user_text(self, text: str, now: float | None = None) -> list[dict]:
        """从他的话里抽「已答事实」，写进台账。返回新写入的条目。"""
        now = now or _now()
        text = (text or "").strip()
        if not text:
            return []
        norm = _normalize(text)
        topic = next((t for t in TOPICS if t in norm), "")
        # 状态词在原句里找（归一化会把「吃过了」换成「吃饭」，状态就丢了）
        status = ""
        m = _STATUS.search(text)
        if m:
            status = m.group(1)
        if _NEG_STATUS.search(text):
            status = "还没好"
        if not topic or not status:
            self.prune(now)
            return []
        # 同一话题：新状态覆盖旧状态
        self.data["facts"] = [f for f in self.data["facts"] if f.get("topic") != topic]
        item = {
            "topic": topic,
            "status": status,
            "ts": now,
            "quote": text[:60],
        }
        self.data["facts"].append(item)
        self.prune(now)
        self.save()
        return [item]

    def prune(self, now: float | None = None) -> None:
        now = now or _now()
        keep = [
            f for f in self.data["facts"]
            if now - float(f.get("ts") or 0) <= ANSWER_TTL and f.get("topic")
        ]
        if len(keep) > FACT_MAX:
            keep = keep[-FACT_MAX:]
        self.data["facts"] = keep

    def facts_block(self, now: float | None = None, limit: int = 4) -> str:
        now = now or _now()
        self.prune(now)
        rows = sorted(self.data["facts"], key=lambda f: -float(f.get("ts") or 0))[:limit]
        if not rows:
            return ""
        lines = []
        for f in rows:
            lines.append("    - %s：%s（%s 说的）" % (f["topic"], f["status"], _hm(f["ts"])))
        return "\n".join(lines)

    # ---------- 素材 ----------
    def material_used(self, key: str, now: float, window: float = 86400) -> bool:
        rows = [m for m in self.data["materials"] if now - float(m.get("ts") or 0) <= window]
        return any(m.get("key") == key for m in rows)

    def mark_material(self, key: str, now: float | None = None) -> None:
        now = now or _now()
        self.data["materials"] = [
            m for m in self.data["materials"] if now - float(m.get("ts") or 0) <= 7 * 86400
        ]
        self.data["materials"].append({"key": key, "ts": now})
        if len(self.data["materials"]) > 60:
            self.data["materials"] = self.data["materials"][-60:]
        self.save()


# ---------------- 素材池 ----------------

def _date_material(now: float) -> tuple[str, str]:
    """日期类素材：星期、距离周末、月初月末。返回 (key, 文案)。"""
    d = datetime.datetime.fromtimestamp(now)
    wd = "一二三四五六日"[d.weekday()]
    key = "date:%s" % d.strftime("%Y-%m-%d")
    if d.weekday() == 0:
        text = "今天是周一，一周刚开头"
    elif d.weekday() == 4:
        text = "今天周五，明天就周末了"
    elif d.weekday() >= 5:
        text = "今天周%s，是周末" % wd
    elif d.month in (3, 4, 5):
        text = "今天周%s，四月的天开始回暖了" % wd if d.month == 4 else "今天周%s，春天了" % wd
    else:
        text = "今天是周%s" % wd
    return key, text


def build_material_pool(store, viewer: str, data_dir: str, now: float, rng: random.Random | None = None) -> list[dict]:
    """收集「她自己知道的事」，供找话题用。每项 {key, text}。"""
    rng = rng or random
    out: list[dict] = []

    # 1) 昨天/今天的日记（周报不算日记）
    try:
        rows = [
            r for r in (store.all() if store else [])
            if str(r.get("kind")) == "日记"
            and str(r.get("origin") or "") != "weekly_report"
        ]
        rows.sort(key=lambda r: -float(r.get("created_at") or 0))
        if rows:
            body = str(rows[0].get("body") or "").strip()
            if body:
                out.append({"key": "diary:%s" % rows[0].get("id"), "text": "你昨天记下的一段日记：" + body[:160]})
    except Exception:
        pass

    # 2) 最近看过的 B 站视频
    try:
        p = os.path.join(data_dir, "..", "plugin_data", "astrbot_plugin_video_bridge", "bili_cache.json")
        p = os.path.normpath(p)
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                cache = json.load(f)
            items = sorted(
                [(k, v) for k, v in cache.items() if isinstance(v, dict) and v.get("title")],
                key=lambda kv: -float(kv[1].get("ts") or 0),
            )[:3]
            for k, v in items:
                out.append({"key": "bili:%s" % k, "text": "你之前看过一个视频《%s》，是 %s 做的" % (
                    str(v.get("title"))[:40], str(v.get("up") or "某个 UP 主")[:20])})
    except Exception:
        pass

    # 3) 她自己的、有蛋糕的旧话（不是他说的话）
    try:
        if store:
            rows = [
                r for r in store.all()
                if str(r.get("body") or "").startswith("她：")
                and abs(int(r.get("cake") or 0)) >= 2
                and str(r.get("origin") or "") != "proactive"
            ]
            rows.sort(key=lambda r: -float(r.get("created_at") or 0))
            for r in rows[:3]:
                body = str(r.get("body") or "")[2:].strip()
                if len(body) >= 12:
                    out.append({"key": "mine:%s" % r.get("id"), "text": "你以前说过一句：" + body[:80]})
    except Exception:
        pass

    # 4) 日期
    key, text = _date_material(now)
    out.append({"key": key, "text": text})

    rng.shuffle(out)
    return out


def pick_material(state: ConvState, pool: list[dict], now: float) -> dict | None:
    """挑一条 24h 内没用过的素材。"""
    for m in pool:
        if not state.material_used(m["key"], now):
            return m
    return None


# ---------------- 组装状态块 ----------------

def build_state_block(
    state: ConvState,
    now: float,
    his_lines: list[str] | None = None,
    her_lines: list[str] | None = None,
    material: dict | None = None,
    extra: str = "",
) -> str:
    parts = ["【这段对话的现状（每轮都按这个来，不要凭印象）】", "- " + _PRONOUN_RULE]
    fb = state.facts_block(now)
    if fb:
        parts.append("- 他最近已经说清 / 答过的事（别再问同一件，也别当成没说过）：")
        parts.append(fb)
    if her_lines:
        parts.append("- 你刚才对他说过的（原话，别重复、别装作没说过）：")
        for line in her_lines[:3]:
            parts.append("  「%s」" % line[:60])
    if his_lines:
        parts.append("- 他刚才说过的话（他说「刚才那个」「前面说的」就是指这些）：")
        for hm, line in his_lines[:4]:
            parts.append("  %s 「%s」" % (hm, line[:60]))
    if material:
        parts.append("- 你此刻可以随口提的一件属于你自己的事（不是他说的，别编细节）：%s" % material.get("text"))
    if extra:
        parts.append(extra)
    return "\n".join(parts)
