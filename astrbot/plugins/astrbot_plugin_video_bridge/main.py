# -*- coding: utf-8 -*-
"""B 站视频：让她"看完"再聊。

链路：QQ 卡片（json/xml 段）→ 提取 bvid → 本地收集字幕/热评 → 压成素材卡 → 作为临时上下文注入。

设计要点：
1. 只注入"素材"，不注入"该怎么回"——话由她自己的语气说。
2. 没听到内容就明说：素材卡里 confidence=low 时，注入块会要求她说"没听清/只看到标题"。
3. 卡片识别靠 patch 适配器 convert_message：OneBot 的 json 段不会进 message_str，
   纯卡片消息在 LLM 阶段前就会被当空消息丢掉。
"""
import asyncio
import json
import os
import re
import sys
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

BRIDGE = r"XJ_HOME\video-bridge"
BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
SHORT_RE = re.compile(r"(?:b23\.tv|bili2233\.cn)/([0-9A-Za-z]{6,})")
MARK = "[B站视频]"
CACHE = os.path.join(BRIDGE, "cache.json")


def _norm(s: str) -> str:
    if not isinstance(s, str):
        return ""
    s = s.replace("&#44;", ",").replace("&amp;", "&").replace("&quot;", '"')
    s = s.replace("&#39;", "'").replace("&lt;", "<").replace("&gt;", ">")
    s = s.replace("\\/", "/").replace("\\u0026", "&")
    return s


def _blob(abm) -> str:
    parts = []
    for seg in getattr(abm, "message", []) or []:
        try:
            parts.append(json.dumps(seg.toDict(), ensure_ascii=False))
        except Exception:
            parts.append(str(seg))
    return _norm("\n".join(parts))


def _find_bvid(text: str) -> str:
    m = BV_RE.search(text)
    if m:
        return m.group(1)
    m = SHORT_RE.search(text)
    if not m:
        return ""
    try:
        import urllib.request
        req = urllib.request.Request("https://" + m.group(0),
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            final = r.geturl()
        mm = BV_RE.search(final)
        return mm.group(1) if mm else ""
    except Exception:
        return ""


def _load_card(bvid: str) -> dict:
    try:
        return (json.load(open(CACHE, encoding="utf-8")) or {}).get(bvid) or {}
    except Exception:
        return {}


def _render(card: dict) -> str:
    if not card or not card.get("ok"):
        return ""
    beats = "；".join([str(x) for x in (card.get("beats") or [])[:4]])
    low = str(card.get("confidence") or "") == "low"
    head = "【他刚给你发了个 B 站视频：《%s》（%s，%s 秒）】" % (
        card.get("title") or "", card.get("up") or "", card.get("dur") or 0)
    body = "你已经看过了。内容大意：%s" % (card.get("summary") or "")
    if beats:
        body += "\n其中具体的点：%s" % beats
    if card.get("quote"):
        body += "\n里面有一句原话：「%s」" % str(card.get("quote"))[:80]
    if low:
        tail = ("\n注意：这条视频没有字幕，你其实**没听到内容**，只看到了标题、简介和评论。"
                "所以别编画面里发生了什么；可以说「没字幕，我这边听不太清」或者只针对标题聊两句。")
    elif str(card.get("source") or "") == "asr":
        tail = ("\n（这是你听完音频自己听出来的，机器听写可能有个别字错；日语的就当自己听懂了日语。）"
                "\n怎么回：像平时聊天那样随口说一两句，可以提一个具体的点；"
                "不要用「看完了/这个视频讲的是/总结一下」这种汇报腔，不要列点。")
    else:
        tail = ("\n怎么回：像平时聊天那样随口说一两句，可以提一个具体的点；"
                "不要用「看完了/这个视频讲的是/总结一下」这种汇报腔，不要列点，别提是别人帮你总结的。")
    return head + "\n" + body + tail


def _job(bvid: str) -> dict:
    """同步跑一次：collect -> digest（在子进程里做，避免阻塞事件循环）。"""
    try:
        sys.path.insert(0, BRIDGE)
        import run_job  # type: ignore
        return run_job.run(bvid)
    except Exception as exc:
        logger.warning("[video] job failed: %r", exc)
        return {"ok": False, "err": repr(exc)}


@register("astrbot_plugin_video_bridge", "MASTER_NAME",
          "B站视频素材桥：把视频变成可聊的内容（不替她说话）", "1.0.0")
class VideoBridge(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context, config)
        self._patched = False
        self._orig = None

    async def initialize(self):
        await super().initialize()
        self._patch()
        # 预热本地转写服务（模型只加载一次，第一次约几十秒，之后每句 8–12 秒）
        try:
            import threading
            def _warm():
                try:
                    sys.path.insert(0, BRIDGE)
                    import audio_asr  # type: ignore
                    ok = audio_asr.warm()
                    logger.info("[video] asr warm: %s", ok)
                except Exception as exc:
                    logger.warning("[video] asr warm failed: %r", exc)
            threading.Thread(target=_warm, daemon=True).start()
        except Exception:
            pass

    # ---------- 适配器补丁：把 B 站卡片变成可见文本 ----------
    def _patch(self):
        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (
                AiocqhttpAdapter,
            )
        except Exception as exc:
            logger.warning("[video] patch skipped: %r", exc)
            return
        if getattr(AiocqhttpAdapter, "_video_bridge_patched", False):
            self._patched = True
            return
        orig = AiocqhttpAdapter.convert_message
        self._orig = orig

        async def convert_message(self_, event):
            abm = await orig(self_, event)
            try:
                blob = _blob(abm)
                if MARK not in getattr(abm, "message_str", ""):
                    bv = _find_bvid(blob)
                    if bv:
                        line = "%s https://www.bilibili.com/video/%s" % (MARK, bv)
                        abm.message_str = (getattr(abm, "message_str", "") or "") + "\n" + line
                        logger.info("[video] card -> %s", bv)
            except Exception as exc:
                logger.warning("[video] expand err: %r", exc)
            return abm

        AiocqhttpAdapter.convert_message = convert_message
        AiocqhttpAdapter._video_bridge_patched = True
        self._patched = True
        logger.info("[video] adapter patched")

    async def terminate(self):
        try:
            if self._patched and self._orig is not None:
                from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (
                    AiocqhttpAdapter,
                )
                AiocqhttpAdapter.convert_message = self._orig
                AiocqhttpAdapter._video_bridge_patched = False
        except Exception:
            pass

    # ---------- 注入 ----------
    @filter.on_llm_request(priority=110000)
    async def inject_video(self, event: AstrMessageEvent, req):
        text = getattr(event, "message_str", "") or ""
        m = BV_RE.search(text)
        blocks = []
        if m and MARK in text:
            bvid = m.group(1)
            card = _load_card(bvid)
            if not card:
                try:
                    card = await asyncio.wait_for(asyncio.to_thread(_job, bvid), timeout=150)
                except asyncio.TimeoutError:
                    card = {"ok": False, "err": "timeout"}
                except Exception as exc:
                    card = {"ok": False, "err": repr(exc)}
            rendered = _render(card)
            if rendered:
                blocks.append(rendered)
        else:
            # 这一轮没发卡片，但最近聊过视频 -> 让"她还记得"，并分清是哪一个
            seen = []
            for seg in (getattr(req, "contexts", []) or [])[-20:]:
                content = seg.get("content") if isinstance(seg, dict) else None
                flat = json.dumps(content, ensure_ascii=False) if content else ""
                for mm in BV_RE.finditer(flat or ""):
                    bv = mm.group(1)
                    if bv not in seen:
                        seen.append(bv)
            cards = []
            for bv in seen[-2:]:
                card = _load_card(bv)
                if card.get("ok"):
                    cards.append(card)
            if cards:
                latest = cards[-1]
                blocks.append("【你最近看过的视频（最新那条是《%s》）：你已经知道内容了：%s】"
                              % (latest.get("title") or "", (latest.get("summary") or "")[:160]))
                if len(cards) > 1:
                    older = cards[0]
                    blocks.append("【更早还看过《%s》：%s —— 那是另一条，别把两条的内容混在一起】"
                                  % (older.get("title") or "", (older.get("summary") or "")[:120]))

        if not blocks:
            return
        from astrbot.core.agent.message import TextPart
        req.extra_user_content_parts.append(TextPart(text="\n".join(blocks)).mark_as_temp())
        logger.info("[video] injected %s chars", sum(len(b) for b in blocks))
