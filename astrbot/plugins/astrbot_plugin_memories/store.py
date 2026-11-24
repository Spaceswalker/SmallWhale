
# 出处：astrbot_plugin_memories（原作者：若剑清，MIT License）
# 本项目在原作者 v0.5.1 的基础上做了修改（记忆注入、日记总结、语言过滤等，详见 THIRD-PARTY.md）。
# 修改者：XiaoJing contributors

"""小回忆的存储：单文件 JSONL + 字符二元组检索，零第三方依赖。

一条回忆长这样：

    {
      "id": "a1b2c3d4e5f6",
      "title": "第一次一起看烟花",
      "body": "那天风很大，他把外套披在我身上",
      "kind": "event",              # event / promise / preference / wound
      "people": ["示例人物"],
      "tags": ["重要"],
      "cake": 3,                    # 蛋糕值：正=甜，负=苦
      "hits": 2,
      "history": [{"delta": 1, "reason": "他说记得那天", "at": 1792...}],
      "created_at": ..., "updated_at": ..., "last_hit_at": ...
    }
"""

from __future__ import annotations

# ==== 部署占位符（改成你自己的，或用环境变量 XJ_MASTER_QQ / XJ_MASTER_NAME / XJ_BOT_QQ）====
import os as _os


class _QQStr(str):
    """和 str / int 都比较得上的 QQ 号，避免上游代码里混用两种类型时判断失效。"""

    def __eq__(self, other):
        return str(self) == str(other)

    def __ne__(self, other):
        return str(self) != str(other)

    def __hash__(self):
        return hash(str(self))


MASTER_QQ = _QQStr(_os.environ.get("XJ_MASTER_QQ", "1000000002"))
BOT_QQ = _QQStr(_os.environ.get("XJ_BOT_QQ", "1000000001"))
MASTER_NAME = _os.environ.get("XJ_MASTER_NAME", "Master")
# ==== 占位符结束 ====


import json
import math
import os
import re
import threading
import time
import uuid

try:  # PET3:semantic —— 本地 bge 向量检索（可选，装不上就回落纯 BM25）
    import vec as _vec_mod  # noqa: F401
except Exception:  # pragma: no cover
    try:
        from . import vec as _vec_mod  # noqa: F401
    except Exception:
        _vec_mod = None


def _vec_enabled() -> bool:
    return bool(_vec_mod is not None and getattr(_vec_mod, "enabled", lambda: False)())

_PUNCT = re.compile(
    r"[\s\u3000,，.。!！?？;；:：、\"'“”‘’()（）\[\]【】<>《》~～…—\-_/\\|*#`]+"
)

CAKE_MIN = -10
CAKE_MAX = 10

# 归属：owner = 某个人的 QQ；"public" = 谁都能想起的公共记忆；"" = 老数据（按 legacy_owner 处理）
PUBLIC = "public"
LEGACY_OWNER = "MASTER_QQ"
# 主用户：可以看到全部回忆（别人跟她聊过的内容，主用户问起可以复述）；
# 反过来，主用户的记忆对其他任何人一律不可见。
MASTER_QQ = LEGACY_OWNER

FIDELITY_CLEAR = "clear"
FIDELITY_VAGUE = "vague"
FIDELITY_FEELING = "feeling"


def strength_of(row: dict) -> float:
    """情绪分量 0~1：优先用显式 strength，其次由 cake 推。"""
    raw = row.get("strength")
    if raw is not None:
        try:
            return max(0.0, min(1.0, float(raw)))
        except Exception:
            pass
    cake = abs(int(row.get("cake") or 0))
    return max(0.15, min(1.0, 0.25 + 0.15 * cake))


def owner_of(row: dict) -> str:
    who = str(row.get("owner") or "").strip()
    return who or LEGACY_OWNER


def visible_to(row: dict, viewer: str) -> bool:
    """她对任何人都知道全部记忆（便于理解上下文），但人格层保证只爱主用户、不认错人。"""
    return True



def normalize(text: str) -> str:
    return _PUNCT.sub("", (text or "").lower())


def grams(text: str) -> set[str]:
    t = normalize(text)
    if not t:
        return set()
    if len(t) == 1:
        return {t}
    return {t[i : i + 2] for i in range(len(t) - 1)}


def _clamp(value: int) -> int:
    return max(CAKE_MIN, min(CAKE_MAX, int(value)))


# PET3:retr —— 检索侧「这几条不值得被想起来」的判定（只影响检索，不删数据）
_FILLER_CHARS = set("嗯哦噢啊呀吧呢嘛的了是我你他她它在吗好行对不没么啦嗯嗯蛤嘿诶哎哦")
_FILLER_PUNCT = "，。！？…、,.!?~ 　·—"


def _memory_text(row: dict) -> str:
    """去掉「我：」「她：」说话人前缀后的正文。"""
    body = str(row.get("body") or "")
    for pfx in ("我：", "她：", "他："):
        if body.startswith(pfx):
            body = body[len(pfx):]
            break
    return body.strip()


def _retrievable(row: dict) -> bool:
    """记忆能不能被检索出来。

    聊天记录逐句归档，库里必然有一堆「没事」「嗯」「哈哈」——它们不是回忆，
    让它们进候选池只会把有限的名额挤掉，所以正文太短、或整句只是语气词
    的一律不参与检索（数据本身保留，手动翻/日记/遗忘都不受影响）。
    """
    text = _memory_text(row)
    if len(text) < 6:
        return False
    core = "".join(ch for ch in text if ch not in _FILLER_PUNCT)
    if core and len(core) <= 12 and len(set(core)) <= 3 and all(ch in _FILLER_CHARS for ch in core):
        return False
    return True



def similarity(a: dict, b: dict) -> float:
    """两条回忆的相似度（标题 + 正文的二元组 Jaccard）。"""
    ta = grams(str(a.get("title") or "") + str(a.get("body") or ""))
    tb = grams(str(b.get("title") or "") + str(b.get("body") or ""))
    if not ta or not tb:
        return 0.0
    union = len(ta | tb)
    return len(ta & tb) / union if union else 0.0


# PET3:syn —— 轻量同义词表（本地，0 token；只做同义扩展加分）
_SYNONYMS = {
    "吃饭": ["吃饭", "吃东西", "用餐", "下馆子", "早饭", "午饭", "晚饭"],
    "吃": ["吃饭", "吃东西", "饿"],
    "饿": ["饿", "吃", "肚子"],
    "生气": ["生气", "不高兴", "介意", "吃醋", "不开心"],
    "不高兴": ["不高兴", "生气", "介意", "不舒服", "委屈"],
    "吃醋": ["吃醋", "介意", "不高兴", "酸"],
    "睡": ["睡", "睡觉", "困", "熬夜", "夜里"],
    "熬夜": ["熬夜", "睡", "几点睡", "太晚"],
    "工作": ["工作", "上班", "加班", "忙"],
    "加班": ["加班", "工作", "上班", "忙"],
    "妹妹": ["妹妹", "姐", "姐姐", "另一个 AI"],
    "姐姐": ["姐姐", "妹妹", "另一个 AI"],
    "喜欢": ["喜欢", "爱", "心动", "想你"],
    "想念": ["想你", "想念", "惦记"],
    "家": ["家", "家里", "回家", "屋子"],
    "考试": ["考试", "复习", "成绩", "考"],
    "身体": ["身体", "不舒服", "生病", "累"],
}

# PET3:prefcool —— 偏好类条目不吃「话题冷却」：他说到吃什么、喜欢什么时，应该每次都想起来
_PREF_TAGS = {"喜好", "爱吃", "口味", "偏好", "喜欢", "习惯", "food"}
_PREF_WORDS = ("最爱吃", "爱吃", "喜欢吃", "最喜欢", "常点", "常吃", "口味")


def _is_pref(row) -> bool:
    try:
        if int(row.get("cake") or 0) >= 2:
            return True
    except Exception:
        pass
    tags = {str(t).strip() for t in (row.get("tags") or [])}
    if tags & _PREF_TAGS:
        return True
    body = str(row.get("body") or "")
    return any(w in body for w in _PREF_WORDS)


class MemoryStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._rows: list[dict] = []
        self._appends = 0  # PET3:append —— 追加了几行没压实
        self._dirty_hits: set = set()  # touch 过但还没落盘的 id
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.load()

    # ---------------- 持久化 ----------------

    def load(self) -> None:
        rows: list[dict] = []
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(obj, dict) and obj.get("id"):
                        rows.append(obj)
        except FileNotFoundError:
            pass
        except Exception:
            pass
        with self._lock:
            self._rows = rows
            self._replay_hits_locked()
            self._migrate_locked()

    def _replay_hits_locked(self) -> None:
        """回放 *.hits 侧写日志（崩溃前没折叠的 hits/last_hit_at），然后折叠回去。"""
        journal = self.path + ".hits"
        try:
            with open(journal, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    mid = str(obj.get("id") or "")
                    for row in self._rows:
                        if row.get("id") == mid:
                            row["hits"] = max(int(row.get("hits") or 0), int(obj.get("hits") or 0))
                            row["last_hit_at"] = max(
                                float(row.get("last_hit_at") or 0),
                                float(obj.get("last_hit_at") or 0),
                            )
                            break
        except FileNotFoundError:
            return
        except Exception:
            return
        try:
            self._save_locked()
        except Exception:
            pass

    def _migrate_locked(self) -> None:
        """老数据补字段：没有 owner 的默认归主用户，避免专属记忆漏给别人。"""
        changed = False
        now = time.time()
        for row in self._rows:
            if not str(row.get("owner") or "").strip():
                row["owner"] = LEGACY_OWNER
                changed = True
            if row.get("strength") is None:
                row["strength"] = round(strength_of(row), 3)
                changed = True
            if not row.get("fidelity"):
                row["fidelity"] = FIDELITY_CLEAR
                changed = True
            row.setdefault("origin", "")
            row.setdefault("body3p", "")
            row.setdefault("emotion", "")
            row.setdefault("pinned", False)
            row.setdefault("updated_at", now)
        if changed:
            try:
                self._save_locked()
            except Exception:
                pass

    def _save_locked(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for row in self._rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(tmp, self.path)
        self._appends = 0
        self._dirty_hits.clear()
        # 全量重写后侧写日志就没意义了
        try:
            os.remove(self.path + ".hits")
        except FileNotFoundError:
            pass
        except Exception:
            pass

    def _append_locked(self, row: dict) -> None:
        """PET3:append —— 只追加一行，不再重写整个文件。"""
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._appends += 1

    def save(self) -> None:
        """PET3:cd-persist —— 把内存里的变化（touch 记下的 hits/last_hit_at）落盘。

        原来每条消息全量重写整个 jsonl；现在改成往 `*.hits` 侧写日志追加几行，
        加载时回放。侧写日志超过 512KB 才折叠回主文件，避免写放大。"""
        with self._lock:
            if not self._dirty_hits:
                return
            by_id = {str(r.get("id") or ""): r for r in self._rows}
            try:
                with open(self.path + ".hits", "a", encoding="utf-8") as f:
                    for mid in sorted(self._dirty_hits):
                        r = by_id.get(mid)
                        if not r:
                            continue
                        f.write(
                            json.dumps(
                                {
                                    "id": mid,
                                    "hits": int(r.get("hits") or 0),
                                    "last_hit_at": float(r.get("last_hit_at") or 0),
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
            except Exception:
                pass
            self._dirty_hits.clear()
            try:
                if os.path.getsize(self.path + ".hits") > 512 * 1024:
                    self._save_locked()
            except Exception:
                pass

    # ---------------- 读写 ----------------

    def add(
        self,
        title: str,
        body: str,
        kind: str = "event",
        people: list[str] | None = None,
        tags: list[str] | None = None,
        cake: int = 0,
        dedupe: bool = True,
        happened_at: float | None = None,
        quote: str = "",
        people_qq: list[str] | None = None,
        owner: str = "",
        emotion: str = "",
        strength: float | None = None,
        fidelity: str = FIDELITY_CLEAR,
        origin: str = "",
        body3p: str = "",
        pinned: bool = False,
    ) -> dict:
        now = time.time()
        title = (title or "").strip()[:120]
        body = (body or "").strip()
        owner = str(owner or "").strip()
        with self._lock:
            if dedupe:
                for row in self._rows:
                    if title and row.get("title") == title:
                        row["body"] = body or row.get("body", "")
                        row["kind"] = kind or row.get("kind", "event")
                        row["people"] = people or row.get("people") or []
                        row["tags"] = tags or row.get("tags") or []
                        row["cake"] = _clamp(int(row.get("cake") or 0) + int(cake or 0))
                        row["updated_at"] = now
                        if owner:
                            row["owner"] = owner
                        if emotion:
                            row["emotion"] = str(emotion)[:24]
                        if strength is not None:
                            try:
                                row["strength"] = max(0.0, min(1.0, float(strength)))
                            except Exception:
                                pass
                        if pinned:
                            row["pinned"] = True
                        self._save_locked()
                        return dict(row)
            # PET3:unique-id —— 曾经有回填脚本按内容生成 id，撞出过三条共用 dig6aeb13ef 的行；
            # 这里兜一下，撞了就重抽，别再让两条不同的记忆同 id。
            _has = {str(r.get("id") or "") for r in self._rows}
            _new_id = uuid.uuid4().hex[:12]
            while _new_id in _has:
                _new_id = uuid.uuid4().hex[:12]
            row = {
                "id": _new_id,
                "title": title,
                "body": body,
                "kind": (kind or "event").strip()[:24],
                "people": [str(p).strip() for p in (people or []) if str(p).strip()],
                "people_qq": [str(q).strip() for q in (people_qq or []) if str(q).strip()],
                "tags": [str(t).strip() for t in (tags or []) if str(t).strip()],
                "cake": _clamp(cake),
                "happened_at": float(happened_at or 0),  # 0 = 没填，按创建时间算
                "quote": (quote or "").strip()[:300],
                "owner": owner,
                "emotion": str(emotion or "").strip()[:24],
                "strength": max(0.0, min(1.0, float(strength))) if strength is not None else None,
                "fidelity": (fidelity or FIDELITY_CLEAR).strip()[:16],
                "origin": (origin or "").strip()[:120],
                "body3p": (body3p or "").strip()[:2000],
                "pinned": bool(pinned),
                "hits": 0,
                "history": ([{"delta": int(cake), "reason": "初次记住", "at": now}] if cake else []),
                "created_at": now,
                "updated_at": now,
                "last_hit_at": 0,
            }
            self._rows.append(row)
            # PET3:append —— 新行只追加；每攒 600 行或文件超大时压实一次
            try:
                if self._appends >= 600 or (
                    os.path.getsize(self.path) > 64 * 1024 * 1024
                ):
                    self._save_locked()
                else:
                    self._append_locked(row)
            except Exception:
                try:
                    self._save_locked()
                except Exception:
                    pass
            return dict(row)

    def update(self, memory_id: str, **fields: object) -> dict | None:
        allowed = {
            "title",
            "body",
            "body3p",
            "kind",
            "people",
            "people_qq",
            "tags",
            "cake",
            "happened_at",
            "quote",
            "owner",
            "emotion",
            "strength",
            "fidelity",
            "pinned",
        }
        with self._lock:
            for row in self._rows:
                if row.get("id") == memory_id:
                    for key, value in fields.items():
                        if key in allowed and value is not None:
                            if key == "cake":
                                value = _clamp(int(value))
                            row[key] = value
                    row["updated_at"] = time.time()
                    self._save_locked()
                    return dict(row)
        return None

    def add_cake(self, memory_id: str, delta: int, reason: str = "") -> dict | None:
        """加/减蛋糕：正面 +，负面 -，同时记一笔流水。"""
        with self._lock:
            for row in self._rows:
                if row.get("id") == memory_id:
                    before = int(row.get("cake") or 0)
                    after = _clamp(before + int(delta))
                    row["cake"] = after
                    row["updated_at"] = time.time()
                    history = list(row.get("history") or [])
                    history.append(
                        {
                            "delta": int(delta),
                            "reason": (reason or "").strip()[:120],
                            "at": time.time(),
                        }
                    )
                    row["history"] = history[-20:]
                    self._save_locked()
                    return dict(row)
        return None

    def trim_to(self, keep: int) -> int:
        """PET3:grouptrim —— 只保留最近 keep 条（群聊库用：不做遗忘、不进向量索引）。"""
        keep = max(1, int(keep))
        with self._lock:
            if len(self._rows) <= keep:
                return 0
            drop = len(self._rows) - keep
            self._rows = self._rows[-keep:]
            self._save_locked()
            return drop

    def delete(self, memory_id: str, force: bool = True) -> bool:
        """删除一条。force=False 时带蛋糕（有分量）的不许删——遗忘只走这条路。"""
        with self._lock:
            before = len(self._rows)
            kept = []
            for row in self._rows:
                if row.get("id") == memory_id:
                    if not force and int(row.get("cake") or 0) != 0:
                        return False
                    continue
                kept.append(row)
            self._rows = kept
            changed = len(self._rows) != before
            if changed:
                self._save_locked()
            return changed

    def all(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._rows]

    def get(self, memory_id: str) -> dict | None:
        for row in self.all():
            if row.get("id") == memory_id:
                return row
        return None

    def stats(self) -> dict:
        rows = self.all()
        sweet = sum(1 for r in rows if int(r.get("cake") or 0) > 0)
        bitter = sum(1 for r in rows if int(r.get("cake") or 0) < 0)
        return {
            "total": len(rows),
            "sweet": sweet,
            "bitter": bitter,
            "neutral": len(rows) - sweet - bitter,
            "pinned": sum(1 for r in rows if r.get("pinned")),
            "path": self.path,
        }

    # ---------------- 检索 ----------------

    def search(
        self,
        query: str,
        limit: int = 3,
        min_score: float = 1.0,
        bitter_weight: float = 0.85,
        viewer: str | None = None,
        cooldown_hours: float = 0.0,
        semantic: bool = True,
        semantic_min_sim: float = 0.55,
        semantic_top_k: int = 8,
    ) -> list[tuple[float, dict]]:
        """PET3:bm25f —— 二元组覆盖率 + BM25F + 同义词扩展（全本地，0 token）

        A) 只拿正文够长的记忆参与检索：聊天记录里大量「没事」「嗯」这类
           填充句不是记忆，命中它们只会把名额挤掉。
        B) 必须命中「稀有词」才算相关：只出现在很少几条记忆里的词（考试、
           某个人、某个地点）才代表真的沾边；只命中「没事」「今天」这种满库都是
           的词，分数再高也不收。
        """
        q = grams(query)
        if not q:
            return []
        # 整句都是「在吗 / 没事 / 嗯 / 好」这类语气词：不检索，别硬凑
        _qn_text = normalize(query)
        if _qn_text and len(_qn_text) <= 8 and all(ch in _FILLER_CHARS for ch in _qn_text):
            return []
        qn = math.sqrt(len(q)) or 1.0
        now = time.time()
        # df / 平均长度按「全部可见记忆」算，避免被检索过滤改变词频口径；
        # 真正参与打分的只有 _retrievable 的那些。
        rows = [r for r in self.all() if visible_to(r, viewer)]
        if not rows:
            return []

        FW = {"title": 3.0, "people": 2.0, "tags": 1.5, "body": 1.0}
        docs = []
        for row in rows:
            t = str(row.get("title") or "")
            b = str(row.get("body") or "")
            p = " ".join(str(x) for x in (row.get("people") or []))
            g = " ".join(str(x) for x in (row.get("tags") or []))
            docs.append({
                "title": grams(t),
                "people": grams(p),
                "tags": grams(g),
                "body": grams(b),
                "raw": t + " " + b + " " + p + " " + g,
            })

        N = max(1, len(docs))
        df: dict[str, int] = {}
        lens: list[float] = []
        for d in docs:
            u = set()
            L = 0.0
            for k, w in FW.items():
                toks = d[k]
                L += w * len(toks)
                u |= toks
            lens.append(L or 1.0)
            for t in u:
                df[t] = df.get(t, 0) + 1
        avg = sum(lens) / N
        # B) 稀有词门槛：只出现在 ≤max(5, 1% 库容) 条记忆里的二元组才算「真沾边」
        rare_df = max(5, int(N * 0.01))
        # PET3:nameterm —— people / title 里出现过的词，天然算「有信息量」。
        # 不然「某个名字」这种越常提的名字 df 越高，反而被判成"太常见"→ 检索直接返回空。
        _name_terms: set[str] = set()
        for i, row in enumerate(rows):
            if not _retrievable(row):
                continue
            _hit = q & (docs[i]["people"] | docs[i]["title"])
            if _hit:
                _name_terms |= _hit
        # 很短的话（≤4 字）如果每个词都是满库高频词（「没事」这种），
        # 整句就没什么信息量，干脆不检索，别硬凑。
        if len(_qn_text) <= 4:
            _qg = grams(_qn_text)
            if _qg and all(df.get(t, 0) > rare_df for t in _qg) and not _name_terms:
                return []
        k1, b = 1.2, 0.75

        exp_terms = set()
        for key, alts in _SYNONYMS.items():
            if key in query:
                for a in alts:
                    for t in grams(a):
                        if t not in q:
                            exp_terms.add(t)

        def _idf(t: str) -> float:
            d = df.get(t, 0)
            return math.log(1.0 + (N - d + 0.5) / (d + 0.5))

        prelim = []
        bm_max = 0.0
        for i, row in enumerate(rows):
            if not _retrievable(row):
                continue
            # PET3:fresh —— 刚归档（10 分钟内）的话不参与检索：
            # 它已经在最近几轮上下文里了，再捞回来只会重复刚说过的话、白占名额。
            _born = float(row.get("created_at") or row.get("happened_at") or 0)
            if _born and (now - _born) < 600:
                continue
            d = docs[i]
            ng = d["title"] | d["body"] | d["people"] | d["tags"]
            common = len(q & ng)
            if not common and not (exp_terms & ng):
                continue
            hit_terms = (q | exp_terms) & ng
            # 整句原样出现过（且这句不是纯语气词，前面已经拦掉了）也算真沾边
            phrase_hit = bool(query.strip()) and len(query.strip()) >= 2 and query.strip() in d["raw"]
            if not phrase_hit and not any(df.get(t, 0) <= rare_df or t in _name_terms for t in hit_terms):
                continue
            base = common / qn
            bm = 0.0
            for t in q:
                tf = 0.0
                for k, w in FW.items():
                    if t in d[k]:
                        tf += w
                if tf <= 0:
                    continue
                bm += _idf(t) * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * lens[i] / avg))
            for t in exp_terms:
                if t in q:
                    continue
                tf = 0.0
                for k, w in FW.items():
                    if t in d[k]:
                        tf += w
                if tf <= 0:
                    continue
                bm += 0.45 * _idf(t) * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * lens[i] / avg))
            if bm > bm_max:
                bm_max = bm
            phrase = 1.0 if phrase_hit else 0.0
            syn = min(3, len(exp_terms & ng)) * 0.3
            prelim.append((row, base, bm, phrase, syn))

        if not prelim and not (semantic and _vec_enabled()):
            return []

        scored: list[tuple[float, dict]] = []
        norm = bm_max or 1.0
        for row, base, bm, phrase, syn in prelim:
            cake = int(row.get("cake") or 0)
            weight = 1.0 + 0.6 * math.log1p(abs(cake))
            if cake < 0:
                weight *= max(0.2, min(1.5, float(bitter_weight)))
            weight *= 0.6 + 0.9 * strength_of(row)
            last = float(row.get("last_hit_at") or row.get("updated_at") or 0)
            days = max(0.0, (now - last) / 86400.0)
            recency = 1.0 + 0.3 * math.exp(-days / 30.0)
            core = base + 1.1 * (bm / norm) + 0.6 * phrase + syn
            score = core * weight * recency
            if grams(str(row.get("title") or "")) & q:
                score += 1.0
            if score >= min_score:
                scored.append((score, dict(row)))
        # PET3:semantic —— 向量召回补漏：BM25 一个字都撞不上但意思相近的（
        # 「你知道我说的是谁吗」→「某个地点某个人」）从这里捞回来。
        if semantic and _vec_enabled() and _vec_mod is not None:
            try:
                for vid, sim in _vec_mod.query(query, k=semantic_top_k, min_sim=semantic_min_sim):
                    if any(item[1].get("id") == vid for item in scored):
                        continue
                    row = next((r for r in rows if str(r.get("id")) == vid), None)
                    if row is None:
                        continue
                    if not _retrievable(row):
                        continue
                    _born = float(row.get("created_at") or row.get("happened_at") or 0)
                    if _born and (now - _born) < 600:
                        continue
                    cake = int(row.get("cake") or 0)
                    weight = 1.0 + 0.6 * math.log1p(abs(cake))
                    if cake < 0:
                        weight *= max(0.2, min(1.5, float(bitter_weight)))
                    weight *= 0.6 + 0.9 * strength_of(row)
                    last = float(row.get("last_hit_at") or row.get("updated_at") or 0)
                    days = max(0.0, (now - last) / 86400.0)
                    recency = 1.0 + 0.3 * math.exp(-days / 30.0)
                    score = (0.55 + 0.45 * sim) * weight * recency
                    if score >= min_score:
                        scored.append((score, dict(row)))
            except Exception:
                pass
        scored.sort(key=lambda item: (-item[0], -int(item[1].get("cake") or 0)))
        # 冷却：刚被塞进上下文的回忆，N 小时内让给别的（除非实在没别的）
        if cooldown_hours and cooldown_hours > 0:
            cool = cooldown_hours * 3600.0
            fresh = [
                item
                for item in scored
                if _is_pref(item[1]) or now - float(item[1].get("last_hit_at") or 0) >= cool
            ]
            scored = fresh
        return scored[: max(1, limit)]

    # ---------------- 维护用 ----------------

    def count_since(self, ts: float) -> int:
        """从某个时间点之后新建了几条。"""
        return sum(1 for r in self.all() if float(r.get("created_at") or 0) >= ts)

    def last_created_at(self) -> float:
        rows = self.all()
        if not rows:
            return 0.0
        return max(float(r.get("created_at") or 0) for r in rows)

    def forget_candidates(
        self, max_cake: int, older_than_days: float, protect_cake: bool = True
    ) -> list[dict]:
        """长期没被想起、且情绪分量不重的回忆，才有资格被忘掉。

        protect_cake=True 时，只要 cake != 0（不管甜还是苦）就一律不参与遗忘。
        """
        now = time.time()
        out = []
        for row in self.all():
            # PET3:pin —— 日记等标记为永久保留的记忆，不参与遗忘
            if row.get("pinned"):
                continue
            # PET3:digestkeep —— 摘要/日记是压缩后的长期记忆，不参与遗忘
            origin = str(row.get("origin") or "")
            kind = str(row.get("kind") or "")
            if origin.startswith("digest") or kind in ("对话摘要", "日记"):
                continue
            cake = int(row.get("cake") or 0)
            if protect_cake and cake != 0:
                continue
            if abs(cake) > max_cake:
                continue
            last = float(
                row.get("last_hit_at") or row.get("updated_at") or row.get("created_at") or 0
            )
            if (now - last) / 86400.0 >= older_than_days:
                out.append(row)
        return out

    def decay_cake(self, older_than_days: float, mode: str = "zero") -> int:
        """太久没被想起来的蛋糕会淡掉：idle ≥ 天数就归零（或先减半再归零）。

        返回被改动的条数。不动 updated_at，免得把遗忘倒计时也一起重置。
        """
        now = time.time()
        days = max(1.0, float(older_than_days))
        changed = 0
        with self._lock:
            for row in self._rows:
                # PET3:pin —— 永久保留的记忆，蛋糕不衰减
                if row.get("pinned"):
                    continue
                cake = int(row.get("cake") or 0)
                if cake == 0:
                    continue
                last = max(
                    float(row.get("last_hit_at") or 0),
                    float(row.get("updated_at") or 0),
                    float(row.get("created_at") or 0),
                )
                idle = (now - last) / 86400.0
                new_cake = cake
                reason = ""
                if idle >= days:
                    new_cake, reason = 0, "太久没想起，分量淡了"
                elif mode == "halve_then_zero" and idle >= days * 0.7 and abs(cake) >= 2:
                    new_cake = int(cake / 2) if cake > 0 else -int(abs(cake) / 2)
                    reason = "有一阵没想起，分量淡了一半"
                if new_cake != cake:
                    row["cake"] = _clamp(new_cake)
                    history = list(row.get("history") or [])
                    history.append({"delta": new_cake - cake, "reason": reason, "at": now})
                    row["history"] = history[-20:]
                    changed += 1
            if changed:
                self._save_locked()
        return changed

    def merge(
        self,
        keep_id: str,
        drop_ids: list[str],
        body: str | None = None,
        cake: int | None = None,
        title: str | None = None,
    ) -> dict | None:
        """把几条回忆并成一条：留 keep，删 drop，可选替换标题/正文/蛋糕。"""
        with self._lock:
            keep = next((r for r in self._rows if r.get("id") == keep_id), None)
            if keep is None:
                return None
            # PET3:pin —— 永久保留的记忆不被当成合并的收纳对象
            if keep.get("pinned"):
                return None
            drop = {
                i
                for i in drop_ids
                if i and i != keep_id and not any(
                    r.get("id") == i and r.get("pinned") for r in self._rows
                )
            }
            if title:
                keep["title"] = title
            if body:
                keep["body"] = body
            if cake is not None:
                keep["cake"] = _clamp(int(cake))
            keep["updated_at"] = time.time()
            if drop:
                self._rows = [r for r in self._rows if r.get("id") not in drop]
            self._save_locked()
            return dict(keep)

    # ---------------- 时间 / 人物 ----------------

    @staticmethod
    def happened_at(row: dict) -> float:
        return float(row.get("happened_at") or row.get("created_at") or 0)

    def anniversaries(self, window_days: int = 7, today: "date | None" = None, viewer: str | None = None) -> list[dict]:
        """把「月-日」落在今天前后 window_days 天内的回忆挑出来，按临近排序。"""
        from datetime import date as _date

        today = today or _date.today()
        import time as _time

        out = []
        for row in self.all():
            if not visible_to(row, viewer):
                continue
            explicit = float(row.get("happened_at") or 0)
            ts = explicit or self.happened_at(row)
            if not ts:
                continue
            # 90 天内刚记的不算纪念日（哪怕 happened_at 是显式写的）——
            # 归档时 happened_at 一律填"现在"，若只拦没填日期的那种，
            # 今天新记的每一条都会在当天命中"纪念日"，绕过检索和冷却被反复注入。
            if (_time.time() - ts) < 90 * 86400:
                continue
            try:
                when = _date.fromtimestamp(ts)
            except Exception:
                continue
            delta = self._md_delta(when, today)
            if abs(delta) > window_days:
                continue
            years = today.year - when.year
            if delta > 0:
                years += 1
            out.append(
                {
                    "row": row,
                    "delta": delta,
                    "years": max(0, years),
                    "date": when.strftime("%m-%d"),
                    "year": when.year,
                }
            )
        out.sort(key=lambda x: (abs(x["delta"]), -abs(int(x["row"].get("cake") or 0))))
        return out

    @staticmethod
    def _md_delta(when, today) -> int:
        """距离今年这个「月-日」还有几天；0 = 今天，负数 = 今年已过。"""
        from datetime import date as _date

        try:
            this_year = when.replace(year=today.year)
        except ValueError:  # 2 月 29
            this_year = when.replace(year=today.year, day=28)
        delta = (this_year - today).days
        if delta < -180:
            try:
                nxt = when.replace(year=today.year + 1)
            except ValueError:
                nxt = when.replace(year=today.year + 1, day=28)
            delta = (nxt - today).days
        return delta

    def by_people(
        self,
        names: list[str],
        limit: int = 3,
        viewer: str | None = None,
        cooldown_hours: float = 0.0,
    ) -> list[dict]:
        """按人名找回记忆，甜的优先。"""
        wanted = {str(n).strip() for n in names if str(n).strip()}
        if not wanted:
            return []
        rows = []
        for row in self.all():
            if not visible_to(row, viewer):
                continue
            people = {str(p).strip() for p in (row.get("people") or [])}
            if people & wanted:
                rows.append(row)
        if cooldown_hours and cooldown_hours > 0:
            cool = cooldown_hours * 3600.0
            now = time.time()
            fresh = [r for r in rows if _is_pref(r) or now - float(r.get("last_hit_at") or 0) >= cool]
            rows = fresh
        rows.sort(key=lambda r: (-int(r.get("cake") or 0), -self.happened_at(r)))
        return rows[: max(1, limit)]

    def by_qq(
        self,
        qq: str,
        limit: int = 50,
        viewer: str | None = None,
        cooldown_hours: float = 0.0,
        only_substantive: bool = False,
        fresh_seconds: float = 600.0,
    ) -> list[dict]:
        """按 QQ 号找回这个人的回忆。

        only_substantive=True 时走「值得想起来」的过滤（正文太短/纯语气词的不要），
        注入用的就是这个模式；面板里看这个人的全部回忆时保持 False。
        fresh_seconds>0 时跳过刚归档不久的话（PET3:freshslot），避免把对方刚说的
        这一句又当成「回忆」塞回去。
        """
        qq = str(qq or "").strip()
        if not qq:
            return []
        want = qq if viewer is None else str(viewer).strip()
        rows = [
            r
            for r in self.all()
            if visible_to(r, want) and qq in {str(x) for x in (r.get("people_qq") or [])}
        ]
        if only_substantive:
            rows = [r for r in rows if _retrievable(r)]
        now = time.time()
        if fresh_seconds and fresh_seconds > 0:
            rows = [r for r in rows if now - self.happened_at(r) >= fresh_seconds]
        if cooldown_hours and cooldown_hours > 0:
            cool = cooldown_hours * 3600.0
            now = time.time()
            fresh = [r for r in rows if now - float(r.get("last_hit_at") or 0) >= cool]
            rows = fresh
        rows.sort(key=lambda r: (-int(r.get("cake") or 0), -self.happened_at(r)))
        return rows[: max(1, limit)]

    def by_owner(self, viewer: str | None, limit: int = 50) -> list[dict]:
        """这个人的全部专属回忆（公共的不算进来）。"""
        if not viewer:
            return []
        rows = [r for r in self.all() if owner_of(r) == str(viewer).strip()]
        rows.sort(key=lambda r: (-int(r.get("cake") or 0), -self.happened_at(r)))
        return rows[: max(1, limit)]

    def pick_random(
        self,
        exclude_ids: set[str] | None = None,
        min_abs_cake: int = 0,
        viewer: str | None = None,
    ) -> dict | None:
        """按蛋糕分量加权随机挑一条，用于「忽然想起」。"""
        import random as _random

        exclude = exclude_ids or set()
        pool = [
            r
            for r in self.all()
            if r.get("id") not in exclude
            and abs(int(r.get("cake") or 0)) >= min_abs_cake
            and visible_to(r, viewer)
        ]
        if not pool:
            return None
        weights = [
            (1.0 + 0.8 * abs(int(r.get("cake") or 0)))
            * (6.0 if int(r.get("hits") or 0) == 0 else 1.0)
            for r in pool
        ]
        return _random.choices(pool, weights=weights, k=1)[0]

    def touch(self, memory_id: str) -> None:
        with self._lock:
            for row in self._rows:
                if row.get("id") == memory_id:
                    row["hits"] = int(row.get("hits") or 0) + 1
                    row["last_hit_at"] = time.time()
                    self._dirty_hits.add(str(memory_id))
                    return

    def wake_unseen(self, count: int) -> int:
        """PET3:wake —— 每天挑几条从没被想起过的回忆「想起一下」：
        把 last_hit_at 更新到现在，检索时它们的近期权重会更高，也顺带推迟蛋糕衰减。
        """
        count = max(0, int(count or 0))
        if count <= 0:
            return 0
        pool = [
            r
            for r in self._rows
            if not r.get("pinned") and int(r.get("hits") or 0) == 0
        ]
        if not pool:
            return 0
        _random.shuffle(pool)
        now = time.time()
        with self._lock:
            for row in pool[:count]:
                row["last_hit_at"] = now
                row["history"] = list(row.get("history") or [])[-20:]
            self._save_locked()
        return min(count, len(pool))
