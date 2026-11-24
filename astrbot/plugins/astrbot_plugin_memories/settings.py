
# 出处：astrbot_plugin_memories（原作者：若剑清，MIT License）
# 本项目在原作者 v0.5.1 的基础上做了修改（记忆注入、日记总结、语言过滤等，详见 THIRD-PARTY.md）。
# 修改者：XiaoJing contributors

"""小回忆的「阈值」设置：记住的风格、频率、遗忘、整合。

设置存在插件自己的 settings.json 里，面板「回忆阈值」页直接改，
不碰 AstrBot 的插件配置，保持独立。
"""

from __future__ import annotations

import json
import os

DEFAULTS: dict = {
    # 记住的风格：restrained 克制 / balanced 平衡 / sensitive 敏感
    "record_style": "balanced",
    # 频率闸门
    "max_records_per_day": 5,
    "min_gap_minutes": 20,
    # 遗忘
    "forgetting_enabled": True,
    "forget_probability": 0.12,
    "forget_after_days": 21,
    "forget_max_cake": 0,
    # 整合
    "consolidate_enabled": True,
    "consolidate_threshold": 0.72,
    "consolidate_max": 3,
    # 注入与表达
    # 纪念日 / 人物 / 主动想起
    "anniversary_enabled": True,
    "anniversary_window_days": 7,
    "people_recall_enabled": True,
    "proactive_recall_enabled": True,
    "proactive_recall_probability": 0.35,
    # 注入
    "inject_limit": 4,
    "min_score": 0.65,
    "say_cake": False,
    "bitter_weight": 0.85,
    # 自动蛋糕（不调模型，按字面情绪给回忆打分）
    "auto_cake_enabled": True,
    "auto_cake_prob": 0.6,
    "auto_cake_daily_cap": 8,
    "auto_cake_min_gap_minutes": 10,
    # 蛋糕衰减：太久没被想起的分量会淡掉
    "cake_decay_enabled": True,
    "cake_decay_days": 30,
    "cake_decay_mode": "zero",
    "cake_protect_from_forget": True,
    # 注入冷却：同一条记忆 N 小时内不重复塞进上下文
    "inject_cooldown_hours": 24,
    # 话题命中用的短冷却（分钟）：聊到相关的事时，不必等满 24 小时
    "inject_topic_cooldown_minutes": 10,
    "wake_up_per_day": 5,
    # 每日总结：每天一段日记，默认永久保留（不进遗忘、不衰减、不整合）
    "daily_summary_enabled": True,
    "daily_summary_max_chars": 150,
    "daily_summary_hour": 0,
    "daily_summary_lookback_days": 3,
    "daily_summary_permanent": True,
    # 语义检索：本地 bge 向量 + BM25 混合；装不上依赖/没模型时自动回落纯 BM25
    "semantic_enabled": True,
    "semantic_min_sim": 0.55,
    "semantic_top_k": 8,
    # 旧对话压缩：主用户侧超过 N 天的原句压成摘要（没蛋糕、没被想起过的才压）
    "digest_compress_enabled": True,
    "digest_compress_days": 10,
    "digest_compress_per_day": 120,
    # 群聊记忆：单独一个库，跟私聊彻底分开
    "group_memory_enabled": True,
    "group_memory_keep": 2000,
}

STYLE_LABEL = {
    "restrained": "克制",
    "balanced": "平衡",
    "sensitive": "敏感",
}

STYLE_HINT = {
    "restrained": "只记真正改变关系的事，能省则省，一个月记两三条也正常。",
    "balanced": "有分量的事就记，别记寒暄。",
    "sensitive": "稍微有点分量的都值得记，宁可多记一点也不漏。",
}

# (min, max, 类型)
_RULES = {
    "max_records_per_day": (0, 50, int),
    "min_gap_minutes": (0, 1440, int),
    "forget_probability": (0.0, 1.0, float),
    "forget_after_days": (1, 3650, int),
    "forget_max_cake": (0, 10, int),
    "anniversary_window_days": (0, 90, int),
    "proactive_recall_probability": (0.0, 1.0, float),
    "consolidate_threshold": (0.3, 1.0, float),
    "consolidate_max": (1, 20, int),
    "inject_limit": (1, 5, int),
    "min_score": (0.2, 10.0, float),
    "bitter_weight": (0.2, 1.5, float),
    "auto_cake_prob": (0.0, 1.0, float),
    "auto_cake_daily_cap": (0, 50, int),
    "auto_cake_min_gap_minutes": (0, 1440, int),
    "cake_decay_days": (1, 3650, int),
    "inject_cooldown_hours": (0, 168, int),
    "inject_topic_cooldown_minutes": (0, 1440, int),
    "wake_up_per_day": (0, 50, int),
    "daily_summary_max_chars": (40, 600, int),
    "daily_summary_hour": (0, 12, int),
    "daily_summary_lookback_days": (1, 14, int),
    "semantic_min_sim": (0.3, 0.95, float),
    "semantic_top_k": (1, 20, int),
    "digest_compress_days": (7, 3650, int),
    "digest_compress_per_day": (1, 500, int),
    "group_memory_keep": (200, 20000, int),
}


def _clean(data: dict) -> dict:
    out = dict(DEFAULTS)
    for key, value in (data or {}).items():
        if key in (
            "record_style",
            "forgetting_enabled",
            "consolidate_enabled",
            "say_cake",
            "anniversary_enabled",
            "people_recall_enabled",
            "proactive_recall_enabled",
            "auto_cake_enabled",
            "cake_decay_enabled",
            "cake_protect_from_forget",
            "cake_decay_mode",
            "daily_summary_enabled",
            "daily_summary_permanent",
            "semantic_enabled",
            "digest_compress_enabled",
            "group_memory_enabled",
            "group_memory_enabled",
        ):
            continue
        if key in _RULES:
            out[key] = value
    style = str((data or {}).get("record_style") or DEFAULTS["record_style"]).strip().lower()
    out["record_style"] = style if style in STYLE_LABEL else DEFAULTS["record_style"]
    mode = str((data or {}).get("cake_decay_mode") or DEFAULTS["cake_decay_mode"]).strip().lower()
    out["cake_decay_mode"] = mode if mode in ("zero", "halve_then_zero") else DEFAULTS["cake_decay_mode"]
    for key in (
        "forgetting_enabled",
        "consolidate_enabled",
        "say_cake",
        "anniversary_enabled",
        "people_recall_enabled",
        "proactive_recall_enabled",
        "auto_cake_enabled",
        "cake_decay_enabled",
        "cake_protect_from_forget",
        "daily_summary_enabled",
        "daily_summary_permanent",
        "semantic_enabled",
        "digest_compress_enabled",
    ):
        raw = (data or {}).get(key, DEFAULTS[key])
        if isinstance(raw, str):
            out[key] = raw.strip().lower() in {"1", "true", "yes", "on"}
        else:
            out[key] = bool(raw)
    for key, (low, high, typ) in _RULES.items():
        try:
            val = typ(out.get(key, DEFAULTS[key]))
        except Exception:
            val = DEFAULTS[key]
        out[key] = max(low, min(high, val))
    return out


class Settings:
    def __init__(self, path: str) -> None:
        self.path = path
        self.data: dict = dict(DEFAULTS)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.load()

    def load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                self.data = _clean(json.load(f))
        except FileNotFoundError:
            self.data = dict(DEFAULTS)
            self.save()
        except Exception:
            self.data = dict(DEFAULTS)
        return self.data

    def save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def update(self, payload: dict) -> dict:
        merged = dict(self.data)
        merged.update(payload or {})
        self.data = _clean(merged)
        self.save()
        return self.data

    # 便捷读取
    @property
    def style(self) -> str:
        return str(self.data.get("record_style") or "balanced")

    @property
    def style_hint(self) -> str:
        return STYLE_HINT.get(self.style, STYLE_HINT["balanced"])

    @property
    def max_per_day(self) -> int:
        return int(self.data.get("max_records_per_day") or 0)

    @property
    def min_gap_minutes(self) -> int:
        return int(self.data.get("min_gap_minutes") or 0)
