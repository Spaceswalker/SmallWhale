"""展开 QQ 合并转发（"聊天记录"卡片），让她看得见里面的内容。

为什么不能只靠 on_llm_request 钩子：OneBot v11 里合并转发是一个 forward 段（只有 id），
AstrBot 只把 text/markdown 拼进 message_str，于是「只发一个聊天记录卡片」的消息在
LLM 阶段开始前就被当成"空消息"丢掉了（agent_sub_stages/internal.py 的
has_valid_message / has_media_content 判断都不认 Forward），钩子根本轮不到执行。

所以这里在适配器把消息转成 AstrBotMessage 之后（还没进管线）就把卡片展开、
写进 message_str。钩子保留做兜底（同一消息里既有文字又有卡片的情况）。
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


import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.message import TextPart
from astrbot.core.message.components import Forward

_MAX_CACHE = 32
_CACHE_TTL = 600.0
_MAX_CHARS = 2500
_MAX_FORWARDS = 2
_CACHE: dict[str, tuple[float, str]] = {}

_NOTICE_HEAD = (
    "[转发的聊天记录] 他转发了一段聊天给你（不是你俩刚才这段对话）。"
    "里面提到的人和事你可以顺着聊，但别装作当时在场，也别把里面别人的话当成他说的。"
    "记录里写「他：」的就是他本人的话，别人用昵称。"
    "跟他说话的时候叫「你」，不要用第三人称叫他名字（别说「MASTER_NAME 刚才…」）。\n"
)
_MARK = "[转发的聊天记录]"


def _seg_text(seg) -> str:
    t = ""
    data = {}
    if isinstance(seg, dict):
        t = str(seg.get("type") or "")
        data = seg.get("data") or {}
    else:
        t = str(getattr(seg, "type", "") or "")
        try:
            data = seg.toDict().get("data") or {}
        except Exception:
            data = {}
    if t in ("text", "plain"):
        return str(data.get("text") or "")
    if t == "image":
        return "[图片]"
    if t == "face":
        return "[表情]"
    if t == "at":
        return f"@{data.get('qq') or ''}"
    if t == "record":
        return "[语音]"
    if t == "video":
        return "[视频]"
    if t == "forward":
        return "[转发的聊天记录]"
    return ""


def _node_text(node) -> str:
    if not isinstance(node, dict):
        return ""
    sender = node.get("sender") or {}
    name = (
        sender.get("card")
        or sender.get("nickname")
        or sender.get("name")
        or str(sender.get("user_id") or "")
        or "某人"
    )
    # PET3:hlab —— 他自己在记录里的话标成「他」，别用昵称，否则她会学着叫「MASTER_NAME刚才…」
    if str(sender.get("user_id") or "") == MASTER_QQ:
        name = "他"
    msgs = node.get("message") or node.get("content") or []
    body = msgs if isinstance(msgs, str) else "".join(_seg_text(m) for m in msgs)
    body = " ".join(str(body).split())
    if not body:
        return ""
    return f"{name}：{body}"


def _nodes_of(obj) -> list:
    if not isinstance(obj, dict):
        return []
    for key in ("message", "messages", "nodes"):
        val = obj.get(key)
        if isinstance(val, list) and val:
            return val
    data = obj.get("data")
    if isinstance(data, dict):
        for key in ("message", "messages", "nodes"):
            val = data.get(key)
            if isinstance(val, list) and val:
                return val
    return []


async def _fetch(bot, fid: str) -> str:
    """问 NapCat 要节点；不同版本参数名不一样，两种都试。"""
    if bot is None or not hasattr(bot, "call_action"):
        return ""
    obj = None
    for params in ({"message_id": fid}, {"id": fid}):
        try:
            obj = await bot.call_action("get_forward_msg", **params)
            if obj:
                break
        except Exception as exc:
            logger.debug(f"[forward] get_forward_msg{params} failed: {exc!r}")
    nodes = _nodes_of(obj)
    if not nodes:
        return ""
    lines = [t for t in (_node_text(n) for n in nodes) if t]
    if not lines:
        return ""
    return f"（共 {len(nodes)} 条）\n" + "\n".join(lines)


async def _cached_fetch(bot, fid: str) -> str:
    hit = _CACHE.get(fid)
    if hit and time.time() - hit[0] <= _CACHE_TTL:
        return hit[1]
    text = await _fetch(bot, fid)
    if text:
        if len(_CACHE) >= _MAX_CACHE:
            oldest = min(_CACHE.items(), key=lambda kv: kv[1][0])[0]
            _CACHE.pop(oldest, None)
        _CACHE[fid] = (time.time(), text)
    return text


async def _expand_abm(adapter, abm) -> None:
    """把 abm.message 里的 forward 段展开，追加进 abm.message_str。"""
    comps = list(getattr(abm, "message", []) or [])
    fwds = [c for c in comps if isinstance(c, Forward)]
    if not fwds:
        return
    base = str(getattr(abm, "message_str", "") or "")
    if _MARK in base:
        return
    blocks = []
    for comp in fwds[:_MAX_FORWARDS]:
        fid = str(getattr(comp, "id", "") or "")
        if not fid:
            continue
        text = await _cached_fetch(getattr(adapter, "bot", None), fid)
        if text:
            blocks.append(text)
    if not blocks:
        return
    body = "\n\n---\n\n".join(blocks)[:_MAX_CHARS]
    add = (_NOTICE_HEAD + body) if not base.strip() else ("\n\n" + _NOTICE_HEAD + body)
    abm.message_str = base + add
    logger.info(f"[forward] expanded {len(blocks)} card(s), {len(body)} chars")


@register(
    "astrbot_plugin_forward",
    "MASTER_NAME",
    "展开 QQ 合并转发的聊天记录（聊天记录卡片），让她看得见里面写了什么",
    "1.1.0",
)
class ForwardPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}

    async def initialize(self):
        await super().initialize()
        try:
            self._install_patch()
        except Exception as exc:
            logger.warning(f"[forward] patch failed: {exc!r}")
        logger.info("[forward] ready")

    @staticmethod
    def _install_patch() -> None:
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (
            AiocqhttpAdapter,
        )

        if getattr(AiocqhttpAdapter, "_forward_patched", False):
            return
        orig = AiocqhttpAdapter._convert_handle_message_event

        async def patched(self, event, get_reply=True):
            abm = await orig(self, event, get_reply)
            try:
                await _expand_abm(self, abm)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[forward] expand failed: {exc!r}")
            return abm

        AiocqhttpAdapter._forward_orig = orig
        AiocqhttpAdapter._convert_handle_message_event = patched
        AiocqhttpAdapter._forward_patched = True
        logger.info("[forward] adapter patched")

    async def terminate(self):
        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (
                AiocqhttpAdapter,
            )

            if getattr(AiocqhttpAdapter, "_forward_patched", False):
                AiocqhttpAdapter._convert_handle_message_event = getattr(
                    AiocqhttpAdapter, "_forward_orig", AiocqhttpAdapter._convert_handle_message_event
                )
                AiocqhttpAdapter._forward_patched = False
        except Exception:
            pass

    @filter.on_llm_request(priority=110000)
    async def inject_forward(self, event: AstrMessageEvent, req):
        """兜底：适配器没展开到（例如别的适配器）时，这里再试一次。"""
        try:
            if not bool(self.config.get("enabled", True)):
                return
            if _MARK in str(getattr(event.message_obj, "message_str", "") or ""):
                return
            comps = list(getattr(event.message_obj, "message", []) or [])
            fwds = [c for c in comps if isinstance(c, Forward)]
            if not fwds:
                return
            blocks = []
            for comp in fwds[:_MAX_FORWARDS]:
                fid = str(getattr(comp, "id", "") or "")
                if fid:
                    text = await _cached_fetch(getattr(event, "bot", None), fid)
                    if text:
                        blocks.append(text)
            if not blocks:
                return
            body = "\n\n---\n\n".join(blocks)[:_MAX_CHARS]
            notice = _NOTICE_HEAD + body
            req.extra_user_content_parts.append(TextPart(text=notice).mark_as_temp())
            logger.info(f"[forward] injected via hook, {len(body)} chars")
        except Exception as exc:
            logger.warning(f"[forward] hook error: {exc!r}")
