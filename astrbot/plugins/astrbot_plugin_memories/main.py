
# 出处：astrbot_plugin_memories（原作者：若剑清，MIT License）
# 本项目在原作者 v0.5.1 的基础上做了修改（记忆注入、日记总结、语言过滤等，详见 THIRD-PARTY.md）。
# 修改者：XiaoJing contributors

"""她自己的小回忆（astrbot_plugin_memories）

独立的「事件记忆 + 蛋糕反馈」插件：不依赖、不修改任何其它插件。

* memory_note     记住一件有分量的事
* memory_feedback 给某条回忆加/减蛋糕（正负反馈）
* memory_recall   想起来

阈值（记住的风格 / 频率 / 遗忘 / 整合）存在插件自己的 settings.json，
面板「回忆阈值」页可以直接调。
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


import asyncio
import datetime
import datetime as _dt
import json
import os
import random
import re
import time
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.message import TextPart
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from . import conv_state as CS
from .people import PeopleRegistry
from .settings import DEFAULTS, STYLE_LABEL, Settings
from .store import MASTER_QQ, MemoryStore, owner_of, similarity, visible_to
from .tools import (
    KIND_LABEL,
    MemoryFeedbackTool,
    MemoryNoteTool,
    MemoryRecallTool,
    cake_text,
)

PLUGIN_NAME = "astrbot_plugin_memories"

# 面板上显示的心情名（内部只有 happy/shy/tense/calm 四种）
MOOD_LABEL = {"happy": "有点开心", "shy": "有点害羞", "tense": "有点紧张", "calm": "安静平和"}
API_PREFIX = f"/{PLUGIN_NAME}"

# PET3:single —— 同一进程只留一条 proactive / daily 循环。
# 插件热重载会重新 __init__，旧实例的 asyncio 任务如果没有被取消，就会和新的一起
# 按 30 秒节拍各自发一条，于是同一句关心连发三条。新实例启动前先把旧任务掐掉。
# PET3:reason —— 从他的每句话里抽计划/情绪/承诺，作为主动消息的由头
_PLAN_RE = re.compile(r"(明天|后天|今晚|下午|晚上|这周|周末|待会|一会儿|最近|这几天)[^，。！？\n]{0,8}?(要|得|准备|打算|计划|在弄|去)[^，。！？\n]{2,24}")
_EMO_RE = re.compile(r"(有点|好|太|特别|很|挺|超)(累|困|烦|难受|难过|紧张|焦虑|不开心|生气|失望|无聊|想哭)")
# PET3:joke —— 玩笑/反讽/口嗨：别当情绪事件去「回头关心」，否则几小时后她会把你半开玩笑的话当真追问
_JOKE_RE = re.compile(r"(呜呜|哈哈|嘿嘿|嘻嘻|开玩笑|逗你|骗你的|闹着玩|笑死|乐死|狗头|doge|233|啊这|随口说说|说说而已)")
_PROMISE_RE = re.compile(r"(晚点|等下|一会儿|睡前|回头|待会|明天)(找你|说|回你|给你|聊|来)")
# PET3:appt —— 把「我五点半下课」这类话解析成具体时刻，到点拿它当由头
_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_APPT_EVENT_RE = re.compile(
    r"(下课|上课|有课|课|放学|上学|开会|会议|比赛|考试|答辩|训练|上机|出发|动身|回来|回家|到家|到校|"
    r"吃饭|午饭|晚饭|早饭|睡觉|睡|起床|见面|集合|练车|健身|打工|上班|下班|值班|上台|表演|"
    r"面试|复诊|看病|取快递|拿快递|车站|机场|高铁|火车|飞机|宿舍|寝室|接我|送我)"
)
_APPT_TIME_RE = re.compile(
    r"(凌晨|早上|上午|中午|下午|傍晚|晚上|今晚|明早|明晚|明天)?\s*"
    r"([0-9]{1,2}|[零一二两三四五六七八九十]{1,3})\s*(?:点|时|[:：])\s*"
    r"(半|[0-9]{1,2}|[零一二两三四五六七八九十]{1,3})?"
)


def _cn_num(s: str) -> int:
    s = str(s or "").strip()
    if not s:
        return -1
    if s.isdigit():
        return int(s)
    total, cur, seen = 0, 0, False
    for ch in s:
        if ch in _CN_DIGIT:
            cur = _CN_DIGIT[ch]
            seen = True
        elif ch == "十":
            if not seen:
                cur = 1
            total += cur * 10
            cur, seen = 0, False
    if seen:
        total += cur
    return total if (seen or total) else -1


def _parse_appt(text: str, now_ts: float):
    """『我五点半下课』→ (时间戳, '17:30')；解析不出来返回 None。"""
    import datetime as _dt
    t = str(text or "")
    # PET3:appt-habit —— 「我平时都十二点睡」说的是习惯，不是约定，别排日程
    if re.search(r"(平时|平常|一般|总是|老是|每天|天天)", t) and not re.search(
            r"(明天|后天|明早|明晚|今晚|待会|一会儿|等下)", t):
        return None
    if not _APPT_EVENT_RE.search(t):
        return None
    # PET3:appt-clock —— 说的是「现在几点了」（都十点了／半夜十点了／已经十点），不是约定
    if re.search(r"(都|已经|当时|这都|半夜|这么晚|好晚)\s*[0-9零一二两三四五六七八九十]{1,3}\s*点", t) and re.search(
            r"(睡|上床|就寝|休息)", t):
        return None
    # PET3:appt-now —— 「一会就睡」「马上睡」是马上要做的事，不用排日程
    if re.search(r"(一会|一会儿|马上|这就|现在就|等下就|待会就)\s*(?:就)?\s*(?:睡|去睡|休息|躺)", t):
        return None
    m = _APPT_TIME_RE.search(t)
    if not m:
        return None
    hint = m.group(1) or ""
    hh = _cn_num(m.group(2))
    if hh < 0 or hh > 24:
        return None
    mm_raw = m.group(3) or ""
    if mm_raw == "半":
        mm = 30
    elif mm_raw:
        mm = _cn_num(mm_raw)
    else:
        mm = 0
    if mm < 0 or mm > 59:
        mm = 0
    if hh == 24:
        hh = 0
    base = _dt.datetime.fromtimestamp(now_ts)
    # 先按前缀把「几点」收敛成 24 小时制的候选
    hours = []
    if hint in ("凌晨", "早上", "上午", "明早"):
        if hh <= 11:
            hours = [hh]
    elif hint == "中午":
        hours = [12]
    elif hint in ("下午", "傍晚", "晚上", "今晚", "明晚"):
        hours = [hh + 12 if hh < 12 else hh]
    elif hh == 12:
        # 「十二点睡觉」是半夜十二点，不是中午十二点；说「午睡」才按中午算
        if re.search(r"(睡觉|睡了|上床|就寝)", t) and "午睡" not in t:
            hours = [0]
        else:
            hours = [12]
    elif 1 <= hh <= 6:
        hours = [hh + 12]
    elif 7 <= hh <= 23:
        hours = [hh]
    if not hours:
        return None
    day_off = 1 if ("明" in hint) else 0
    cands = []
    for d_off in (day_off, day_off + 1):
        for hour in hours:
            if hour > 23:
                continue
            c = (base + _dt.timedelta(days=d_off)).replace(
                hour=hour, minute=mm, second=0, microsecond=0)
            ts = c.timestamp()
            if ts < now_ts + 60 or ts > now_ts + 26 * 3600:
                continue
            cands.append(ts)
    if not cands:
        return None
    ts = min(cands)
    c = _dt.datetime.fromtimestamp(ts)
    label = ("明天 " if c.date() != base.date() else "") + c.strftime("%H:%M")
    return ts, label
# PET3:pronguard —— 主动消息里最常错的两种人称，生成后兜底检查一次
_BAD_PRONOUN_RE = re.compile(r"你是我(女儿|儿子|爸|妈|父亲|母亲)|我是你(女儿|儿子|爸|妈|父亲|母亲)")

# PET3:scenegurd —— 她只在屏幕里，没有身体也没有物理场景（学自 astrbot_plugin_fish_heart 的“发送前自检”，但做成确定性正则，0 token）
_SCENE_RE = re.compile(
    r"我(?:正)?(?:坐|躺|靠|趴)(?:在|上|下)"
    r"|我(?:去|走|跑|回)(?:到|去|出|进)(?:了)?(?:房间|厨房|客厅|窗边|窗前|门口|阳台|街上|外面|床上)"
    r"|(?:刚|才)(?:回到|走进|走出|进了|出了)(?:房间|厨房|客厅|窗边|门口|阳台|街上|外面)"
    r"|(?:看|望|盯)着?(?:窗|窗外|门口|街上|外面)"
    r"|(?:站在|走到|回到)(?:窗|门|街|路)"
    r"|我(?:在|正)(?:房间|床上|厨房|客厅|阳台|街上|外面)"
)

# PET3:boring —— 拿「我在翻我们的聊天」当由头，是最没话找话的一种开场，生成后拦一次
_BORING_RE = re.compile(
    r"翻着?(?:上面|以前|之前|咱们|我们|那段|这些)?"
    r"|翻到(?:你|那|这|以前|之前|咱们)"
    r"|(?:看|盯)着?(?:上面|那些|咱们|我们|这段)的?(?:对话|话)"
    r"|又不知道(?:干|做)嘛|没事找(?:话|事)|就过来和?你说(?:一声|说话)"
)

# PET3:proself —— 主动消息里"我在干嘛"式的凑数由头（翻对话/发呆/看屏幕），一律不要
_PRO_SELF_RE = re.compile(
    r"翻着|翻到|翻(?:了)?(?:下|了)?(?:咱们|我们)?的?(?:聊天|对话)"
    r"|发呆|发着呆|盯着(?:对话|屏幕|上面)"
    r"|(?:对着|看着)(?:屏幕|对话框|上面那些)"
    r"|没什么话题|找(?:个)?话题"
)

# PET3:busy —— 他明确说在忙/上课/考试时，主动消息就先别往上凑
_BUSY_RE = re.compile(r"(在忙|忙着|忙完|上课|有课|考试|写作业|赶工|开会|打比赛|训练|自习|做实验|赶ddl|刷题|复习|答辩|面试)")
_SLEEP_RE = re.compile(r"(去睡|睡了|先睡|晚安|困了|躺下|关灯)")
_UNTIL_RE = re.compile(r"(?:到|直到)\s*(\d{1,2}|[零一二两三四五六七八九十]{1,3})\s*(?:点|[:：])\s*(半|\d{1,2}|[零一二两三四五六七八九十]{1,3})?")


def _busy_until(text: str, now_ts: float) -> float:
    """PET3:busy —— 把「我上课到五点」「我要睡了」算成忙碌到什么时候。"""
    t = str(text or "")
    if not (_BUSY_RE.search(t) or _SLEEP_RE.search(t)):
        return 0.0
    if _SLEEP_RE.search(t):
        d = _dt.datetime.fromtimestamp(now_ts)
        nxt = d.replace(hour=8, minute=0, second=0, microsecond=0)
        if nxt <= d:
            nxt += _dt.timedelta(days=1)
        return nxt.timestamp()
    m = _UNTIL_RE.search(t)
    if m:
        try:
            hh = _cn_num(m.group(1))
            mm = 0 if not m.group(2) else (30 if m.group(2) == "半" else _cn_num(m.group(2)))
            if 0 <= hh <= 23 and 0 <= mm <= 59:
                d = _dt.datetime.fromtimestamp(now_ts)
                p = d.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if p <= d:
                    p += _dt.timedelta(days=1)
                return min(p.timestamp(), now_ts + 6 * 3600)
        except Exception:
            pass
    return now_ts + 75 * 60


def _ago_label(at: float, now: float) -> str:
    """PET3:ago —— 这件事是「昨晚 / 今天上午 / 前天」说的，别让她把隔夜的事说成「刚才」。"""
    try:
        if not at:
            return ""
        a = _dt.datetime.fromtimestamp(float(at))
        n = _dt.datetime.fromtimestamp(float(now))
        days = (n.date() - a.date()).days
        hh = a.strftime("%H:%M")
        if days <= 0:
            return "今天 " + hh
        if days == 1:
            return ("昨晚 " if (a.hour >= 20 or a.hour < 6) else "昨天 ") + hh
        if days == 2:
            return "前天 " + hh
        return "%d 天前" % days
    except Exception:
        return ""


def _clamp_quiet(ts: float) -> float:
    """PET3:quiet —— 0–8 点不主动打扰；落在这个窗口里的事件挪到当天 08:00 再发。"""
    try:
        d = _dt.datetime.fromtimestamp(float(ts))
        if d.hour < 8:
            d = d.replace(hour=8, minute=random.randint(0, 10), second=0, microsecond=0)
            return d.timestamp()
        return float(ts)
    except Exception:
        return float(ts)


_LOOP_TASKS: dict = {}


def _restart_loop(name: str, coro):
    old = _LOOP_TASKS.get(name)
    if old is not None and not old.done():
        try:
            old.cancel()
        except Exception:
            pass
    task = asyncio.create_task(coro)
    _LOOP_TASKS[name] = task
    return task

try:
    from quart import request
except Exception:  # pragma: no cover
    request = None


def _mask_qq(text: str, inst) -> str:
    """把长数字 QQ 号换成名字，避免她对着主用户念号码。"""
    import re as _re
    t = str(text or "")
    def _rep(m):
        q = m.group(0)
        try:
            nm = inst._name_of(q) if hasattr(inst, "_name_of") else ""
        except Exception:
            nm = ""
        return nm or "一个网友"
    return _re.sub(r"(?<!\d)\d{5,12}(?!\d)", _rep, t)


def _third_person_body(plugin, row: dict, viewer: str) -> str:
    """PET3:3p —— 注入时给每条回忆标明「谁说的」，原话放引号里。

    以前是把「你/我」机械替换成「她/MASTER_NAME」，但遇到「你等等」「你别走」
    这类呼语会被改成「她等等」，反而更乱。这里保留原话，只加说话人标签；
    提示词里再说明「引号里的我＝说话人、你＝听话人」。
    """
    b = str(row.get("body") or "").strip()
    pfx = b[:2] if b[:2] in ("我：", "她：") else ""
    inner = b[2:].strip() if pfx else b
    if not inner:
        inner = str(row.get("body3p") or "").strip()
        if inner[:2] in ("我：", "她："):
            inner = inner[2:].strip()
    if not inner:
        return ""
    if pfx == "我：":
        return "他说过：「" + inner + "」"
    if pfx == "她：":
        return "她说过：「" + inner + "」"
    return inner


_TONE_PUNCT = set("，。！？!?、；;：:…~～.·‘’“”\"'（）() \t\r\n")
# PET3:particle-guard —— 句末语气词：不许堆叠（吧嗯呢），不许悬在标点后面（睡了。嗯）
_P_ONE_CN = "吧呢啊呀啦诶嗯哦嘛么"
_P_STACK_CN = re.compile(r"([%s])(?:[%s]){1,2}" % (_P_ONE_CN, _P_ONE_CN))


def _fix_particles_cn(text: str) -> str:
    if not text:
        return text
    out = re.sub(r"([。！？!?])([%s])" % _P_ONE_CN, lambda m: m.group(2) + m.group(1), text)
    return _P_STACK_CN.sub(lambda m: m.group(0)[0], out)


# PET3:hlab2 —— 她偶尔用第三人称叫他（「那个……MASTER_NAME 刚才真的让我…」），改成第二人称
_HIS_NAME_RE = re.compile(
    r"MASTER_NAME(?=(?:刚才|刚刚|真的|昨天|今天|早上|晚上|说|讲|让|跟|和|的|也|又|还|就))"
)


# PET3:repeat-phrase —— 她常用的“套路词”，连用就显得像复读机
_STOCK_PHRASES = ("打岔", "没个准数", "吓唬人", "装没事", "拿你没办法")


def _fix_his_name(text: str) -> str:
    if not text or "MASTER_NAME" not in text:
        return text
    return _HIS_NAME_RE.sub("你", text)


# PET3:youplural —— 私聊里只有他和她两个人，她却会说「你们腻歪腻歪」。
# 只在句子里没有任何第三方线索时，把「你们」收回成「你」。
_THIRD_PARTY_RE = re.compile(
    r"(你们俩|他们|她们|朋友|室友|同学|同事|姐姐|妹妹|哥哥|弟弟|妈妈|爸爸|父母|"
    r"老师|老板|领导|家里人|大家|两位|她|俩)"
)


def _fix_you_plural(text: str) -> str:
    if not text or "你们" not in text:
        return text
    # 「你们俩」「你们几个」这类本身带数量的保留
    if re.search(r"你们(俩|两个|几个|三个人)", text):
        return text
    if _THIRD_PARTY_RE.search(text):
        return text
    return text.replace("你们", "你")

# PET3:backref —— 他这句话在回指刚刚说过的事（"刚才那个""前面说的"…）
_BACKREF_RE = re.compile(
    "刚才|刚刚|上面(说|讲|聊)|之前(那句|那个|说的|提的)|那个事|还记得|我说的是|前面(说|提)"
)

# PET3:convstate —— 他让"你找个话题/聊点什么"时，才从素材池里挑一件她自己的事
_WANT_TOPIC_RE = re.compile(
    r"(找(个|点)?话题|聊(点|些)?什么|说(点|些)?什么|你(来|先)(说|找|起个头)|换个话题|没事聊|说点什么吧)"
)


def _norm_line(s: str) -> str:
    """去空白/标点，用于「历史里有没有这句话」的比较（PET3:normline）。"""
    return "".join(ch for ch in str(s or "") if ch not in _TONE_PUNCT)


def _hist_plain_text(content) -> str:
    """AstrBot 历史消息的 content 可能是字符串，也可能是段列表。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
    return ""


def _hist_replaced(content, text: str):
    """保留原结构，把第一个 text 段换成整句；字符串直接替换（PET3:histfix）。"""
    if isinstance(content, str):
        return text
    if isinstance(content, list):
        out = []
        done = False
        for p in content:
            if isinstance(p, dict):
                p2 = dict(p)
                if not done and str(p2.get("type") or "") == "text":
                    p2["text"] = text
                    done = True
                out.append(p2)
            else:
                out.append(p)
        if not done:
            out.insert(0, {"type": "text", "text": text})
        return out
    return text


def _others_lines(owner_inst, viewer: str, limit: int = 14, store=None) -> list[str]:
    """把「别的人找她说过的话」按人分组：每人取最近几条，优先取对方说的话。"""
    if not viewer:
        return []
    try:
        rows = (store or owner_inst.store).all()
    except Exception:
        return []
    now = time.time()
    by_owner = {}
    seen = {}
    for r in rows:
        try:
            ow = owner_of(r)
            if not ow or ow == viewer:
                continue
            if not visible_to(r, viewer):
                continue
            ts = float(r.get("created_at") or 0)
            if now - ts > 86400:
                continue
            # PET3:2p —— 别人的话用第三人称版（body3p），没有就退回 body
            body = str(r.get("body3p") or r.get("body") or "").strip()
            if len(body) < 4:
                continue
            key = body.replace(" ", "")
            s0 = seen.setdefault(ow, set())
            if key in s0:
                continue
            s0.add(key)
            by_owner.setdefault(ow, []).append((ts, body))
        except Exception:
            continue
    if not by_owner:
        return []
    order = sorted(by_owner.items(), key=lambda kv: max(t for t, _ in kv[1]), reverse=True)
    def _strip_pfx(b: str) -> str:
        for pfx in ("她：", "我：", "ta："):
            if b.startswith(pfx):
                return b[len(pfx):]
        return b

    def _to3p(inst, raw: str, who: str, is_her: bool) -> str:
        """PET3:pron —— 运行时把第一/第二人称转成第三人称（不改任何已存数据）。"""
        fn = getattr(inst, "_deictic", None)
        if not fn:
            return raw
        try:
            return fn(raw, who, to_her=(not is_her))
        except Exception:
            return raw

    out = []
    for ow, items in order[:8]:
        items.sort(key=lambda x: x[0], reverse=True)
        said = [b for t, b in items if not b.startswith("她：")][:3]
        heard = [b for t, b in items if b.startswith("她：")][:1]
        picked = said + heard
        if not picked:
            continue
        try:
            nm = owner_inst._name_of(ow) if hasattr(owner_inst, "_name_of") else ""
        except Exception:
            nm = ""
        nm = nm or "一个网友"
        label = ("%s（%s）" % (ow, nm)) if nm and nm != ow else ow
        for b in picked:
            is_her = b.startswith("她：")
            raw = _strip_pfx(b).replace(chr(10), " ").strip()
            raw = _mask_qq(raw, owner_inst)[:160]
            head = ("她回「%s」" % label) if is_her else ("「%s」说" % label)
            out.append("- %s：「%s」" % (head, raw))
    return out[:limit]

@register(
    PLUGIN_NAME,
    "若剑清",
    "她自己的小回忆：记住有分量的事件，用蛋糕记住正负反馈，聊到时会自己想起来。",
    "0.5.1",
    "",
)
class MemoriesPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}

        self.data_dir = os.path.join(get_astrbot_plugin_data_path(), PLUGIN_NAME)
        self.store = MemoryStore(os.path.join(self.data_dir, "memories.jsonl"))
        # PET3:groupstore —— 群聊单独一个库，跟主用户私聊彻底隔开
        self.gstore = MemoryStore(os.path.join(self.data_dir, "memories_group.jsonl"))
        self.settings = Settings(os.path.join(self.data_dir, "settings.json"))
        self.people = PeopleRegistry(os.path.join(self.data_dir, "people.json"))
        # PET3:convstate —— 对话状态层：他答过的事、她刚说过的话、指代规则
        self.conv = CS.ConvState(os.path.join(self.data_dir, "conv_state.json"))
        self.state_path = os.path.join(self.data_dir, "state.json")
        self.state = self._load_state()

        # 非主用户的对话不逐条存，攒够 20 条压成一条摘要
        self._digest_buf: dict[str, list[str]] = {}
        self._digesting = False
        self._digest_size = 20  # 非主用户每 20 条压成一条摘要

        self.enabled = self._bool("enabled", True)
        self.inject_enabled = self._bool("inject_enabled", True)
        try:
            self._pro_task = _restart_loop("proactive", self.proactive_loop())
        except Exception:
            self._pro_task = None
        # PET3:daily —— 每天一页日记（默认永久保留），单独一条循环，轻量检查
        self._daily_busy = False
        try:
            self._daily_task = _restart_loop("daily", self.daily_summary_loop())
        except Exception:
            self._daily_task = None

        self.note_tool = MemoryNoteTool(
            store=self.store,
            guard=self._record_guard,
            after_add=self._after_add,
            resolve_people=self.people.resolve_names,
        )
        self.note_tool.refresh_description(self.settings.style_hint)
        self.tools: list[Any] = [
            self.note_tool,
            MemoryFeedbackTool(store=self.store),
            MemoryRecallTool(store=self.store, settings=self.settings),
        ]
        self.context.add_llm_tools(*self.tools)
        self._register_web_apis()

        # 启动时做一次维护（不会重复跑：一天最多一次）
        try:
            self._maintain(force=False)
        except Exception as exc:
            logger.debug("[Memories] 启动维护失败: %s", exc)

        logger.info(
            "[Memories] 小回忆加载完成：%s 条，风格=%s，数据路径 %s",
            self.store.stats().get("total"),
            self.settings.style,
            self.store.path,
        )

    # PET3:loopguard —— 热重载时 __init__ 有可能不在事件循环里跑（create_task 直接抛错，
    # 被 except 吞掉），于是主动循环/daily 循环就这么静悄悄没了。initialize() 是 Star
    # 基类保证在事件循环里 await 的钩子，在这里补一次；任务没了也由 _ensure_loops 拉起来。
    async def initialize(self) -> None:
        try:
            await super().initialize()
        except Exception:
            pass
        self._ensure_loops()
        if self._bool("semantic_enabled", True):
            asyncio.create_task(self._ensure_vec_index())

    async def _ensure_vec_index(self) -> None:
        """首次/增量构建语义索引（在后台线程跑，不卡事件循环）。"""
        try:
            import vec as _v
        except Exception:
            try:
                from . import vec as _v
            except Exception:
                return
        if not _v.enabled():
            return
        try:
            n, total = await asyncio.to_thread(_v.ensure_index, iter(self.store.all()))
            if n > 0:
                self._dbg("vec", f"indexed={n} total={total}")
        except Exception as exc:
            self._dbg("vec-err", repr(exc))

    def _vec_ready(self) -> bool:
        """PET3:semdup —— 语义去重前先确认向量模型可用。"""
        try:
            import vec as _v  # type: ignore
        except Exception:
            try:
                from . import vec as _v  # type: ignore
            except Exception:
                return False
        try:
            return bool(_v.enabled())
        except Exception:
            return False

    def _vec_vec(self, text: str):
        """单句向量（失败返回 None）。"""
        if not text:
            return None
        try:
            import vec as _v  # type: ignore
        except Exception:
            try:
                from . import vec as _v  # type: ignore
            except Exception:
                return None
        try:
            out = _v.embed([str(text)[:400]])
            if out is None or len(out) == 0:
                return None
            return out[0]
        except Exception:
            return None

    def _vec_add_async(self, row: dict) -> None:
        """新记忆落库后异步补一条向量（失败不影响主流程）。"""
        if not self._bool("semantic_enabled", True):
            return
        try:
            asyncio.create_task(asyncio.to_thread(self._vec_add_sync, row))
        except Exception:
            pass

    def _vec_add_sync(self, row: dict) -> None:
        try:
            import vec as _v
        except Exception:
            try:
                from . import vec as _v
            except Exception:
                return
        if not _v.enabled() or not row:
            return
        try:
            # PET3:vecinc —— 只把新行向量加进内存，落盘交给 maybe_flush 节流
            _v.add_many([row])
            _v.maybe_flush()
        except Exception:
            pass

    def _ensure_loops(self) -> None:
        """循环任务不存在/已结束时重新拉起；正常运行时不重复创建。"""
        pairs = (
            ("proactive", "_pro_task", self.proactive_loop),
            ("daily", "_daily_task", self.daily_summary_loop),
        )
        for name, attr, factory in pairs:
            task = getattr(self, attr, None)
            if task is not None and not task.done():
                continue
            try:
                setattr(self, attr, _restart_loop(name, factory()))
            except Exception as exc:
                self._dbg("loop-start-err", f"{name}: {exc!r}")

    # ---------------- 基础 ----------------

    def _bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _load_state(self) -> dict:
        try:
            with open(self.state_path, encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_state(self) -> None:
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False)
            os.replace(tmp, self.state_path)
        except Exception:
            pass

    def _today_start(self) -> float:
        now = datetime.datetime.now()
        return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    # ---------------- 阈值：频率闸门 ----------------

    def _record_guard(self) -> tuple[bool, str]:
        s = self.settings.data
        max_per_day = int(s.get("max_records_per_day") or 0)
        if max_per_day > 0 and self.store.count_since(self._today_start()) >= max_per_day:
            return False, "今天记的已经够多了，这条先不记，等明天吧。"
        gap = int(s.get("min_gap_minutes") or 0)
        if gap > 0:
            last = self.store.last_created_at()
            if last and (time.time() - last) < gap * 60:
                return False, f"刚记过一条，{gap} 分钟内先不记新的。"
        return True, ""

    def _after_add(self, row: dict) -> None:
        self._maintain(force=False, focus=row)

    # ---------------- 阈值：遗忘 + 整合 ----------------

    def _maintain(self, force: bool = False, focus: dict | None = None) -> dict:
        result = {"forgotten": 0, "merged": 0}
        now = time.time()
        state = self.state
        last = float(state.get("last_maintain_at") or 0)
        if not force and now - last < 86400:
            return result

        s = self.settings.data
        # PET3:grouptrim —— 群聊库只留最近 N 条，独立收口，不参与私聊的衰减/遗忘
        if self._bool("group_memory_enabled", True):
            try:
                dropped = self.gstore.trim_to(int(s.get("group_memory_keep") or 2000))
                if dropped:
                    result["group_trimmed"] = dropped
            except Exception as exc:
                logger.debug("[Memories] 群聊库收口失败: %s", exc)
        # PET3:cakedecay —— 太久没被想起来的蛋糕先淡掉，再谈遗忘
        if s.get("cake_decay_enabled"):
            try:
                result["decayed"] = self.store.decay_cake(
                    float(s.get("cake_decay_days") or 30),
                    str(s.get("cake_decay_mode") or "zero"),
                )
            except Exception as exc:
                logger.debug("[Memories] 蛋糕衰减失败: %s", exc)
        # PET3:wake —— 每天挑几条从没被想起过的回忆想一下，别让它们永远躺在库里
        try:
            woken = self.store.wake_unseen(int(s.get("wake_up_per_day") or 0))
            if woken:
                result["woken"] = woken
        except Exception as exc:
            logger.debug("[Memories] 唤醒失败: %s", exc)
        # 遗忘
        if s.get("forgetting_enabled"):
            try:
                cands = self.store.forget_candidates(
                    int(s.get("forget_max_cake") or 0),
                    float(s.get("forget_after_days") or 30),
                    protect_cake=bool(s.get("cake_protect_from_forget", True)),
                )
                prob = float(s.get("forget_probability") or 0)
                for row in cands:
                    # PET3:cakeprotect —— 带蛋糕的记忆不走遗忘这条路
                    if random.random() < prob and self.store.delete(
                        str(row.get("id")), force=not bool(s.get("cake_protect_from_forget", True))
                    ):
                        result["forgotten"] += 1
            except Exception as exc:
                logger.debug("[Memories] 遗忘失败: %s", exc)

        # 整合
        if s.get("consolidate_enabled"):
            try:
                result["merged"] = self._consolidate(
                    float(s.get("consolidate_threshold") or 0.72),
                    int(s.get("consolidate_max") or 3),
                    focus,
                )
            except Exception as exc:
                logger.debug("[Memories] 整合失败: %s", exc)

        state["last_maintain_at"] = now
        self._save_state()
        # PET3:vecsync —— 每天把被删行的向量清掉，顺带把攒着的增量落盘
        try:
            import vec as _v
        except Exception:
            try:
                from . import vec as _v
            except Exception:
                _v = None
        if _v is not None:
            try:
                _v.ensure_index(iter(self.store.all()))
            except Exception:
                pass
        return result

    def _consolidate(self, threshold: float, cap: int, focus: dict | None = None) -> int:
        # PET3:pin —— 日记这类永久保留的记忆，不参与合并
        rows = [r for r in self.store.all() if not r.get("pinned")]
        if len(rows) < 2:
            return 0
        if focus is not None:
            # 优先把新记的和已有的比，避免每次全表两两比
            others = [r for r in rows if r.get("id") != focus.get("id")]
            pool = [focus] + others[:120]
        else:
            pool = rows[:150]

        merged = 0
        used: set[str] = set()
        for i, a in enumerate(pool):
            if merged >= cap:
                break
            if a.get("id") in used:
                continue
            for b in pool[i + 1 :]:
                if merged >= cap:
                    break
                if b.get("id") in used:
                    continue
                try:
                    sim = similarity(a, b)
                except Exception:
                    sim = 0.0
                if sim < threshold:
                    continue
                body_a = str(a.get("body") or "").strip()
                body_b = str(b.get("body") or "").strip()
                body = body_a if body_b in body_a else (body_a + "\n" + body_b).strip()
                cake = int(a.get("cake") or 0) + int(b.get("cake") or 0)
                keep = self.store.merge(
                    str(a["id"]),
                    [str(b["id"])],
                    body=body,
                    cake=cake,
                    title=str(a.get("title") or b.get("title") or ""),
                )
                if keep:
                    merged += 1
                    used.add(str(b["id"]))
                    a = keep
        return merged

    # ---------------- 面板 API ----------------

    def _register_web_apis(self) -> None:
        register = getattr(self.context, "register_web_api", None)
        if not callable(register):
            logger.info("[Memories] 当前 AstrBot 版本不支持插件 Web API，跳过面板注册")
            return
        routes = [
            ("/list", self.api_list, ["GET"], "列出回忆"),
            ("/stats", self.api_stats, ["GET"], "统计"),
            ("/save", self.api_save, ["POST"], "新增或更新"),
            ("/delete", self.api_delete, ["POST"], "删除"),
            ("/cake", self.api_cake, ["POST"], "加/减蛋糕"),
            ("/anniversaries", self.api_anniversaries, ["GET"], "纪念日"),
            ("/people", self.api_people, ["GET"], "人物列表"),
            ("/people/update", self.api_people_update, ["POST"], "更新人物信息"),
            ("/settings", self.api_settings, ["GET"], "读取阈值"),
            ("/settings/save", self.api_settings_save, ["POST"], "保存阈值"),
            ("/maintain", self.api_maintain, ["POST"], "立即遗忘+整合"),
            ("/proactive", self.api_proactive, ["GET"], "她最近为什么找你"),
            ("/portrait", self.api_portrait, ["GET"], "她对你的了解"),
        ]
        for path, handler, methods, desc in routes:
            try:
                register(f"{API_PREFIX}{path}", handler, methods, desc)
            except Exception as exc:
                logger.warning("[Memories] 注册面板 API 失败 %s: %s", path, exc)

    async def api_list(self):
        items = self.store.all()
        items.sort(
            key=lambda r: (int(r.get("cake") or 0), float(r.get("updated_at") or 0)),
            reverse=True,
        )
        for row in items:
            row["cake_text"] = cake_text(int(row.get("cake") or 0))
            row["kind_label"] = KIND_LABEL.get(str(row.get("kind")), "事情")
        return {"ok": True, "items": items, "stats": self.store.stats()}

    async def api_stats(self):
        return {"ok": True, "stats": self.store.stats()}

    async def api_save(self):
        payload = await self._json_body()
        title = str(payload.get("title") or "").strip()
        body = str(payload.get("body") or "").strip()
        if not title or not body:
            return {"ok": False, "error": "标题和正文都不能为空"}
        kind = str(payload.get("kind") or "event").strip().lower()
        if kind not in KIND_LABEL:
            kind = "event"
        people = payload.get("people") or []
        tags = payload.get("tags") or []
        try:
            cake = int(payload.get("cake") or 0)
        except Exception:
            cake = 0
        happened_at = payload.get("happened_at")
        try:
            happened_at = float(happened_at) if happened_at else None
        except Exception:
            happened_at = None
        quote = str(payload.get("quote") or "").strip()
        owner = str(payload.get("owner") or "").strip()
        if payload.get("shared"):
            owner = "public"
        emotion = str(payload.get("emotion") or "").strip()
        fidelity = str(payload.get("fidelity") or "clear").strip().lower()
        if fidelity not in ("clear", "vague", "feeling"):
            fidelity = "clear"
        strength = payload.get("strength")
        try:
            strength = max(0.0, min(1.0, float(strength))) if strength not in (None, "") else None
        except Exception:
            strength = None

        people_qq = self.people.resolve_names([str(p).strip() for p in people if str(p).strip()])
        memory_id = str(payload.get("id") or "").strip()
        if memory_id:
            fields = {
                "title": title,
                "body": body,
                "kind": kind,
                "people": [str(p).strip() for p in people if str(p).strip()],
                "people_qq": people_qq,
                "tags": [str(t).strip() for t in tags if str(t).strip()],
                "cake": cake,
                "quote": quote,
            }
            if owner:
                fields["owner"] = owner
            if emotion:
                fields["emotion"] = emotion
            if strength is not None:
                fields["strength"] = strength
            fields["fidelity"] = fidelity
            if happened_at:
                fields["happened_at"] = happened_at
            updated = self.store.update(memory_id, **fields)
            if not updated:
                return {"ok": False, "error": "没找到这条回忆"}
            return {"ok": True, "memory": updated}
        row = self.store.add(
            title=title,
            body=body,
            kind=kind,
            people=[str(p).strip() for p in people if str(p).strip()],
            people_qq=people_qq,
            tags=[str(t).strip() for t in tags if str(t).strip()],
            cake=cake,
            happened_at=happened_at,
            quote=quote,
            owner=owner or None,
            emotion=emotion,
            strength=strength,
            fidelity=fidelity,
            origin="web",
        )
        return {"ok": True, "memory": row}

    async def api_anniversaries(self):
        s = self.settings.data
        window = int(s.get("anniversary_window_days") or 7)
        items = []
        for item in self.store.anniversaries(window):
            row = item["row"]
            items.append(
                {
                    "id": row.get("id"),
                    "title": row.get("title"),
                    "body": row.get("body"),
                    "quote": row.get("quote") or "",
                    "cake": row.get("cake"),
                    "kind": row.get("kind"),
                    "delta": item["delta"],
                    "years": item["years"],
                    "date": item["date"],
                    "year": item["year"],
                    "happened_at": self.store.happened_at(row),
                }
            )
        return {"ok": True, "items": items, "window": window}

    async def api_people(self):
        reg = self.people.all()
        rows = self.store.all()
        buckets: dict[str, list[dict]] = {p["qq"]: [] for p in reg}
        unknown: dict[str, int] = {}
        for row in rows:
            qqs = {str(x) for x in (row.get("people_qq") or [])}
            if not qqs:
                for name in row.get("people") or []:
                    name = str(name).strip()
                    if not name:
                        continue
                    qq = self.people.resolve_name(name)
                    if qq:
                        qqs.add(qq)
                    else:
                        unknown[name] = unknown.get(name, 0) + 1
            for qq in qqs:
                buckets.setdefault(qq, []).append(row)

        people = []
        for p in reg:
            items = buckets.get(p["qq"], [])
            items.sort(key=lambda r: -float(self.store.happened_at(r)))
            people.append(
                {
                    "qq": p["qq"],
                    "name": p.get("name") or p["qq"],
                    "note": p.get("note") or "",
                    "aliases": p.get("aliases") or [],
                    "total": len(items),
                    "sweet": sum(1 for r in items if int(r.get("cake") or 0) > 0),
                    "bitter": sum(1 for r in items if int(r.get("cake") or 0) < 0),
                    "last_seen": p.get("last_seen") or 0,
                    "memories": [
                        {
                            "id": r.get("id"),
                            "title": r.get("title"),
                            "cake": r.get("cake"),
                            "kind": r.get("kind"),
                            "happened_at": self.store.happened_at(r),
                        }
                        for r in items[:20]
                    ],
                }
            )
        people.sort(key=lambda p: (-p["total"], -float(p["last_seen"] or 0)))
        return {"ok": True, "people": people, "unknown": unknown}

    async def api_people_update(self):
        payload = await self._json_body()
        qq = str(payload.get("qq") or "").strip()
        if not qq:
            return {"ok": False, "error": "缺少 qq"}
        if "name" in payload:
            self.people.rename(qq, str(payload.get("name") or ""))
        if "note" in payload:
            self.people.set_note(qq, str(payload.get("note") or ""))
        return {"ok": True, "person": self.people.get(qq)}

    async def api_delete(self):
        payload = await self._json_body()
        memory_id = str(payload.get("id") or "").strip()
        if not memory_id:
            return {"ok": False, "error": "缺少 id"}
        return {"ok": self.store.delete(memory_id, force=bool(payload.get("force", False)))}

    async def api_cake(self):
        payload = await self._json_body()
        memory_id = str(payload.get("id") or "").strip()
        try:
            delta = int(payload.get("delta") or 0)
        except Exception:
            delta = 0
        if not memory_id or delta == 0:
            return {"ok": False, "error": "缺少 id 或 delta"}
        updated = self.store.add_cake(
            memory_id, max(-3, min(3, delta)), str(payload.get("reason") or "面板调整")
        )
        if not updated:
            return {"ok": False, "error": "没找到这条回忆"}
        return {"ok": True, "memory": updated}

    async def api_settings(self):
        return {
            "ok": True,
            "settings": self.settings.data,
            "style_label": STYLE_LABEL.get(self.settings.style, "平衡"),
            "defaults": DEFAULTS,
        }

    async def api_settings_save(self):
        payload = await self._json_body()
        data = self.settings.update(payload)
        self.note_tool.refresh_description(self.settings.style_hint)
        return {"ok": True, "settings": data, "style_label": STYLE_LABEL.get(self.settings.style, "平衡")}

    async def api_maintain(self):
        result = self._maintain(force=True)
        return {"ok": True, "result": result, "stats": self.store.stats()}

    async def api_proactive(self):
        """给面板看：她现在什么心情、最近几次是为什么找他、还有哪些话记着没说。"""
        try:
            d = self._ps_load()
            now_ts = time.time()
            rows = self.store.all()[-80:]
            mood = self._her_mood(rows, now_ts)
            inits = []
            for i in (d.get("initiations") or [])[:8]:
                inits.append({
                    "ts": float(i.get("ts") or 0),
                    "when": time.strftime("%m-%d %H:%M", time.localtime(float(i.get("ts") or 0))),
                    "tag": str(i.get("tag") or ""),
                    "kind": str(i.get("kind") or ""),
                    "text": str(i.get("text") or ""),
                    "desc": str(i.get("desc") or ""),
                    "mood": str(i.get("mood") or ""),
                    "q": bool(i.get("q")),
                })
            pend = []
            for p in (d.get("pending") or []):
                if p.get("asked"):
                    continue
                due = float(p.get("due_at") or 0)
                pend.append({
                    "kind": str(p.get("kind") or ""),
                    "text": str(p.get("text") or "")[:60],
                    "label": str(p.get("label") or ""),
                    "due": time.strftime("%m-%d %H:%M", time.localtime(due)) if due else "",
                })
            return {
                "ok": True,
                "mood": mood,
                "mood_label": MOOD_LABEL.get(mood, mood),
                "streak": self._unanswered_streak(rows, now_ts),
                "initiations": inits,
                "pending": pend[:8],
            }
        except Exception as exc:
            logger.warning("[Memories] api_proactive failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    async def api_portrait(self):
        """给面板看：她从记忆里总结出的「你」。"""
        try:
            with open(self._portrait_path(), encoding="utf-8") as f:
                p = json.load(f) or {}
            return {
                "ok": True,
                "ts": float(p.get("ts") or 0),
                "when": time.strftime("%m-%d %H:%M", time.localtime(float(p.get("ts") or 0))) if p.get("ts") else "",
                "call": str(p.get("call") or ""),
                "hours": str(p.get("hours") or ""),
                "likes": str(p.get("likes") or ""),
                "moods": str(p.get("moods") or ""),
                "notes": str(p.get("notes") or ""),
            }
        except Exception:
            return {"ok": True, "ts": 0, "when": "", "call": "", "hours": "", "likes": "", "moods": "", "notes": ""}

    async def _json_body(self) -> dict:
        if request is None:
            return {}
        try:
            payload = await request.get_json(silent=True)
        except Exception:
            payload = None
        return payload if isinstance(payload, dict) else {}

    # ---------------- 自动注入 ----------------

    def _known_people(self) -> list[str]:
        names = set()
        for row in self.store.all():
            for p in row.get("people") or []:
                name = str(p).strip()
                if name:
                    names.add(name)
        return sorted(names, key=len, reverse=True)

    def _recent_said_block(self, text: str, now_ts: float) -> str:
        """PET3:backref —— 他在回指刚说过的事时，把最近 2 小时他的原话摆到她眼前。

        不走检索、不占检索名额、0 token：纯粹是把"刚才说过的话"整理成一块给他接话用。
        """
        try:
            if not text or not _BACKREF_RE.search(str(text)):
                return ""
            cur = _norm_line(text)
            rows = []
            for r in self.store.all():
                if str(r.get("owner") or "") != MASTER_QQ:
                    continue
                body = str(r.get("body") or "")
                if not body.startswith("我："):
                    continue
                age = now_ts - float(r.get("created_at") or 0)
                if age < 0 or age > 2 * 3600:
                    continue
                said = body[2:].strip()
                if len(_norm_line(said)) < 4:
                    continue
                if cur and _norm_line(said) == cur:
                    continue
                rows.append((float(r.get("created_at") or 0), said))
            if not rows:
                return ""
            rows.sort(key=lambda x: x[0])
            rows = rows[-5:]
            lines = [
                "【他刚才说过的话（原话）】",
                "他说「刚才那个」「前面说的」就是指下面这些。别当成新的听、别复述原句，接着往下说就行：",
            ]
            for ts, said in rows:
                try:
                    hhmm = datetime.datetime.fromtimestamp(ts).strftime("%H:%M")
                except Exception:
                    hhmm = ""
                lines.append("- %s 「%s」" % (hhmm, said[:80]))
            return "\n".join(lines)
        except Exception:
            return ""

    async def _conv_state_block(self, event, text: str, sender: str, now_ts: float) -> str:
        """PET3:convstate —— 一个块装下「他答过什么 / 她刚说过什么 / 现在几个人 / 可聊素材」。

        替换原来三块独立注入（回指块 + 她自己刚说的话 + 画像里的身份提示），
        减少 token、也避免块与块之间互相矛盾。
        """
        try:
            his: list[tuple[str, str]] = []
            try:
                if _BACKREF_RE.search(str(text)):
                    cur = _norm_line(text)
                    rows = []
                    for r in self.store.all():
                        if str(r.get("owner") or "") != MASTER_QQ:
                            continue
                        body = str(r.get("body") or "")
                        if not body.startswith("我："):
                            continue
                        age = now_ts - float(r.get("created_at") or 0)
                        if age < 0 or age > 2 * 3600:
                            continue
                        said = body[2:].strip()
                        if len(_norm_line(said)) < 4 or _norm_line(said) == cur:
                            continue
                        rows.append((float(r.get("created_at") or 0), said))
                    rows.sort(key=lambda x: x[0])
                    for ts, said in rows[-4:]:
                        his.append((datetime.datetime.fromtimestamp(ts).strftime("%H:%M"), said))
            except Exception:
                his = []

            her: list[str] = []
            try:
                her = await self._last_her_lines(event, 3)
            except Exception:
                her = []

            material = None
            try:
                if _WANT_TOPIC_RE.search(str(text)) or random.random() < 0.12:
                    pool = CS.build_material_pool(self.store, sender, self.data_dir, now_ts)
                    material = CS.pick_material(self.conv, pool, now_ts)
                    if material:
                        self.conv.mark_material(material["key"], now_ts)
                        self._dbg("material", str(material["key"])[:40])
            except Exception:
                material = None

            block = CS.build_state_block(
                self.conv, now_ts, his_lines=his, her_lines=her, material=material
            )
            if len(block) > 900:
                block = block[:900] + "…"
            return block
        except Exception as exc:
            self._dbg("convstate-err", repr(exc)[:80])
            return ""

    def _always_inject(self, req: Any, _sender: str, text: str) -> None:
        """PET3:always —— 这些块跟"有没有拣到记忆"无关，必须每轮都注入。

        之前它们挂在拣记忆之后，一旦这轮没拣到（很常见），就会提前 return，
        导致画像、身体周期、「他刚才说过的话」全都失效。
        """
        try:
            parts = getattr(req, "extra_user_content_parts", None)
            if not isinstance(parts, list):
                parts = []
                req.extra_user_content_parts = parts
            # ⓿ 硬事实卡：今天/明天必须记准的安排（有课没课、几点考试）
            if _sender == MASTER_QQ:
                try:
                    _fl = self._facts_line(time.time())
                    if _fl:
                        parts.append(TextPart(text=_fl).mark_as_temp())
                except Exception:
                    pass
            # ① 回指通道已并入 PET3:convstate 的统一状态块，这里不再单独注入
            if _sender == MASTER_QQ:
                # ② 画像：她对你的了解
                try:
                    _pb = self._portrait_block()
                    if _pb:
                        parts.append(TextPart(text=_pb).mark_as_temp())
                except Exception:
                    pass
                # ③ 身体周期：全周期、第二人称、写明这是真的
                try:
                    _cyc = self._cycle_today()
                    _ph = {"period": "经期", "follicular": "经后", "ovulation": "排卵期", "luteal": "经前"}.get(
                        str(_cyc.get("phase") or ""), ""
                    )
                    parts.append(TextPart(text=(
                        "【你自己这几天的身体状态——这是真的，不是比喻】\n"
                        "你的周期是 14 天一轮，这轮的第 %d 天，处在「%s」：%s。\n"
                        "这条说的就是你自己：他问「生理期」「经期」「例假」「最近是不是不舒服」，指的都是你这里的现状，不是别人。\n"
                        "按这个回答就行。别用「我哪来的生理期」「我不是人类」来否认——那句话只用来拒绝真的身体行为（吃饭、走路、出门），不用来否认身体感受。\n"
                        "可以谈坠、懒、烦、轻松这些感觉；但不要描述具体的生理细节，也不要说周期多少天、激素这些机制。主动说起时用「上次」「前阵子」，不说「上个月」。\n"
                        % (int(_cyc.get("day") or 1), _ph, str(_cyc.get("hint") or ""))
                    )).mark_as_temp())
                    self._dbg("cycle-inject", "%s d%s" % (_ph, _cyc.get("day")))
                except Exception as exc:
                    self._dbg("cycle-inject-err", repr(exc)[:80])
        except Exception as exc:
            self._dbg("always-inject-err", repr(exc)[:80])

    @filter.on_llm_request()
    async def inject_memories(self, event: AstrMessageEvent, req: Any) -> None:
        self._dbg("mem-hook", str(getattr(event, "unified_msg_origin", ""))[-40:])
        # PET3:dump —— 调试开关，默认关。开着才会把整段 prompt 落盘（里面有聊天原文）
        try:  # DBG-PROMPT
            if getattr(self.settings, "data", {}).get("prompt_dump_enabled"):
                _dbg = r"XJ_ASTRBOT\data\plugin_data\astrbot_plugin_memories\prompt-dump.txt"
                with open(_dbg, "w", encoding="utf-8") as _f:
                    _f.write("=== system_prompt ===\n" + str(getattr(req, "system_prompt", "")) + "\n")
                    _f.write("=== contexts ===\n")
                    for _c in (getattr(req, "contexts", None) or []):
                        _f.write(str(_c)[:2000] + "\n")
                    _f.write("=== extra_parts ===\n")
                    for _p in (getattr(req, "extra_user_content_parts", None) or []):
                        _f.write(str(_p)[:2000] + "\n")
        except Exception:
            pass
        if not self.enabled or not self.inject_enabled:
            return
        # PET3:loopguard —— 每次要注入时顺便确认后台循环还在
        self._ensure_loops()
        # PET3:cronfix —— 历史里的 [CronJob] 指令行换成她实际发出去的话（仅私聊）
        if "GroupMessage" not in str(getattr(event, "unified_msg_origin", "") or ""):
            try:
                await self._repair_cron_history(event)
            except Exception:
                pass
            try:
                asyncio.create_task(self._repair_cron_history_delayed(event))
            except Exception:
                pass
        text = (getattr(event, "message_str", "") or "").strip()
        if not text or text.startswith(("/回忆", "回忆 ")):
            return

        # PET3:groupstore —— 群聊里只翻群聊库，看不到主用户私聊的记忆
        _in_grp = self._in_group(event)
        _st = self.gstore if _in_grp else self.store
        s = self.settings.data
        limit = max(1, min(int(s.get("inject_limit") or 4), 5))
        picked: list[tuple[str, dict]] = []
        seen: set[str] = set()

        # PET3:nofab —— 她自己的旧发言只当「旧话」，不许当成发生过的事实
        def _selftalk(row: dict) -> bool:
            _b = str(row.get("body") or "")
            _t = str(row.get("title") or "")
            return _b.startswith("她：") or _t.startswith("她：")

        # PET3:yesterday —— 问「昨天 / 昨晚」时，先把昨天真发生过的记录摆出来
        _yday_rows: list[dict] = []

        # PET3:recall-ctx —— 检索不只看这一句：把最近两轮的上下文也带上，指代才能对上
        _ctx_text = ""
        try:
            _ctx = list(getattr(req, "contexts", None) or [])[-6:]
            _tails: list[str] = []
            for _m in _ctx:
                if not isinstance(_m, dict):
                    continue
                _role = str(_m.get("role") or "")
                _c = _m.get("content")
                if isinstance(_c, str):
                    _t = _c
                elif isinstance(_c, list):
                    _t = "".join(str(_p.get("text") or "") for _p in _c if isinstance(_p, dict))
                else:
                    _t = ""
                _t = re.sub(r"<system_reminder>.*?</system_reminder>", "", _t, flags=re.S).strip()
                if _t and _role in ("user", "assistant"):
                    _tails.append(_t)
            _ctx_text = " ".join(_tails[-4:]).strip()[:600]
        except Exception:
            _ctx_text = ""
        # PET3:cd-split —— 话题命中用短冷却（10 分钟），固定位（蛋糕最高/人名卡）仍用长冷却
        _cd_topic = max(0.0, float(s.get("inject_topic_cooldown_minutes") or 10)) / 60.0
        _cd_fixed = float(s.get("inject_cooldown_hours") or 0)

        # 记忆隔离：只注入「和当前说话的人有关」的条目，别人的回忆不许串台
        # 例外：电脑端（webchat）只有主用户在用，一律按主用户身份处理，和 QQ 记忆互通
        _sender = ""
        try:
            if str(event.get_platform_name() or "").lower() == "webchat":
                _sender = "MASTER_QQ"
            else:
                _sender = str(event.get_sender_id() or "").strip()
        except Exception:
            _sender = ""

        # PET3:always —— 画像 / 身体周期 / 回指这几块与"有没有拣到记忆"无关，先注入
        if not _in_grp:
            self._always_inject(req, _sender, text)
            # PET3:convstate —— 统一状态块（他答过的事 / 她刚说过的话 / 指代 / 素材）
            if _sender == MASTER_QQ:
                try:
                    self.conv.note_user_text(text, time.time())
                    _csb = await self._conv_state_block(event, text, _sender, time.time())
                    if _csb:
                        _parts = getattr(req, "extra_user_content_parts", None)
                        if not isinstance(_parts, list):
                            _parts = []
                            req.extra_user_content_parts = _parts
                        _parts.append(TextPart(text=_csb).mark_as_temp())
                        self._dbg("convstate", "%d chars" % len(_csb))
                except Exception as exc:
                    self._dbg("convstate-err2", repr(exc)[:80])

        def _mine(row: dict) -> bool:
            # 归属隔离：归属这个人的、或公共记忆，才允许出现
            if not _sender:
                return owner_of(row) == "public"
            try:
                return visible_to(row, _sender)
            except Exception:
                return False

        def take(rows: list[dict], tag: str) -> None:
            for row in rows:
                if not _mine(row):
                    continue
                if len(picked) >= limit:
                    return
                rid = str(row.get("id"))
                if not rid or rid in seen:
                    continue
                seen.add(rid)
                picked.append((tag, row))

        # 1) 今天就是纪念日
        if s.get("anniversary_enabled"):
            try:
                for item in _st.anniversaries(
                    int(s.get("anniversary_window_days") or 7), viewer=_sender
                ):
                    if item["delta"] == 0:
                        take([item["row"]], "today")
            except Exception as exc:
                logger.debug("[Memories] 纪念日匹配失败: %s", exc)

        # 2) 说话的人自己
        if s.get("people_recall_enabled"):
            try:
                sender_qq = str(event.get_sender_id() or "").strip()
                if sender_qq:
                    take(
                        _st.by_qq(
                            sender_qq,
                            limit=1,
                            viewer=sender_qq,
                            cooldown_hours=_cd_fixed,
                            only_substantive=True,
                        ),
                        "person",
                    )
            except Exception as exc:
                logger.debug("[Memories] 按人召回失败: %s", exc)

        # 3) 提到某个人
        if s.get("people_recall_enabled"):
            try:
                names = [n for n in self._known_people() if n and n in text]
                if names:
                    take(
                        _st.by_people(
                            names,
                            limit=1,
                            viewer=_sender,
                            cooldown_hours=_cd_topic,
                        ),
                        "people",
                    )
            except Exception as exc:
                logger.debug("[Memories] 人物召回失败: %s", exc)

        # 3.5) 昨天问句：昨天真发生过的事优先占位
        if not _yday_rows and re.search(r"(昨天|昨晚|昨儿|前一天|前天)", text):
            _ago = 2 if "前天" in text else 1
            try:
                _yday_rows = self._rows_of_day(_st, _sender, _ago, 4)
            except Exception as exc:
                logger.debug("[Memories] 昨天召回失败: %s", exc)
        if _yday_rows:
            take(_yday_rows, "yesterday")

        # 3) 关键词命中
        try:
            _semantic = self._bool("semantic_enabled", True)
            _s_min_sim = float(s.get("semantic_min_sim") or 0.55)
            _s_top_k = int(s.get("semantic_top_k") or 8)
            hits = _st.search(
                text,
                limit=limit,
                min_score=float(s.get("min_score") or 0.65),
                bitter_weight=float(s.get("bitter_weight") or 0.85),
                viewer=_sender,
                cooldown_hours=_cd_topic,
                semantic=_semantic,
                semantic_min_sim=_s_min_sim,
                semantic_top_k=_s_top_k,
            )
            _rows = [row for _score, row in hits]
            # PET3:recall-ctx —— 这一句没捞够时，再用最近两轮的上下文补一轮
            if _ctx_text and len(_rows) < limit:
                try:
                    _h2 = _st.search(
                        _ctx_text,
                        limit=limit,
                        min_score=float(s.get("min_score") or 0.65),
                        bitter_weight=float(s.get("bitter_weight") or 0.85),
                        viewer=_sender,
                        cooldown_hours=_cd_topic,
                        semantic=_semantic,
                        semantic_min_sim=_s_min_sim,
                        semantic_top_k=_s_top_k,
                    )
                    _have = {str(r.get("id")) for r in _rows}
                    for _sc, _r in _h2:
                        _rid = str(_r.get("id"))
                        if _rid not in _have:
                            _rows.append(_r)
                            _have.add(_rid)
                except Exception as exc:
                    logger.debug("[Memories] 上下文检索失败: %s", exc)
            take(_rows, "match")
        except Exception as exc:
            logger.debug("[Memories] 检索失败: %s", exc)

        # 4) 什么都没命中时，偶尔「忽然想起」一件事（每天最多一次）
        if not picked and s.get("proactive_recall_enabled"):
            try:
                today_key = datetime.datetime.now().strftime("%Y-%m-%d")
                if (
                    self.state.get("proactive_day") != today_key
                    and random.random() < float(s.get("proactive_recall_probability") or 0)
                ):
                    row = _st.pick_random(viewer=_sender)
                    if row:
                        self.state["proactive_day"] = today_key
                        self._save_state()
                        picked.append(("drift", row))
            except Exception as exc:
                logger.debug("[Memories] 主动想起失败: %s", exc)

        if not picked and not _others_lines(self, _sender, store=_st):
            return

        lines = ["【你心里记着的一些事（你自己的小回忆）】"]
        lines.append(
            "（「他／MASTER_NAME」是正在和你说话的那个人，「她」是你自己。"
            "引号里是当时的原话：原话里的「我」指说话的人，「你」指听话的人——"
            "所以他说的话里「你」就是你自己，你说的话里「你」就是他。）"
        )
        # PET3:nofab2 —— 硬规矩：只许照着记录说，记不清就说不记得
        lines.append(
            "（重要：下面是真实的记录，不是你此刻的联想。记录里写了的，就照着说，别装作想不起来；"
            "记录里没写的具体细节，才说「记不清了」，不要顺着「昨晚 / 昨天 / 上次」编故事，"
            "尤其不许编转账、礼物、约定、见面的时间地点。"
            "标着「你自己以前说过的话」的，只是你当时的说法，不能当成事实再往外说。"
            "你自己说过的话不一定真实：可能是玩笑、附和、哄人、随口一说，甚至是当时编的；"
            "别人问起那件事时，别拿自己从前的一句戏言当证词，没有别的记录佐证就说记不清。）"
        )
        # PET3:addrrule —— 称呼规矩写进每轮注入（主用户 11-19：别让「主用户」从旧记录里渗出来）
        lines.append(
            "（称呼：你叫他 MASTER_NAME，或者直接说「你」；不要用「主人」这类角色扮演式称呼，"
            "除非你自己的人设里就是这么设定的。）"
        )
        # PET3:ydcue —— 这轮如果捞到了「昨天」，直接告诉模型：带【昨天】那条就是答案
        if any(_t == "yesterday" for _t, _r in picked):
            lines.append(
                "（他可能在问「昨晚 / 昨天」。下面带【昨天（…）】的那条就是昨天整天的事，"
                "就是你要回答的内容——照着讲，别回答「想不起来」。）"
            )
        for tag, row in picked:
            cake = int(row.get("cake") or 0)
            kind = KIND_LABEL.get(str(row.get("kind")), "事情")
            # PET3:diary —— 日记走专用格式：不被标题/蛋糕/类型前缀挤占，正文尽量全给
            if tag == "yesterday" and str(row.get("kind") or "") == "日记":
                # PET3:diary1p —— 日记已经是第一人称（我 / 你），别再转第三人称，否则她会说「你们」
                _dbody = str(row.get("body") or "")
                if len(_dbody) > 700:
                    _dbody = _dbody[:700] + "…"
                _dday = str(row.get("title") or "").replace(" 这天", "").strip()
                lines.append(
                    "- 【昨天（" + _dday + "）你自己写下的日记，这就是昨天整天发生的事，"
                    "他问「昨晚 / 昨天」时直接照着讲，不要再说记不清】" + _mask_qq(_dbody, self)
                )
                continue
            # PET3:3p —— 原话外面包一层说话人标签，不机械替换代词（日记例外：它本身是第一人称）
            body = str(row.get("body") or "") if str(row.get("kind") or "") == "日记" else _third_person_body(self, row, _sender)
            # PET3:ydfull —— 问「昨天/昨晚」时日记就是答案，别截到只剩前半段
            _cap_body = 620 if tag == "yesterday" else 220
            if len(body) > _cap_body:
                body = body[: _cap_body] + "…"
            extra = ""
            if tag == "today":
                extra = "（今天正好是它的纪念日）"
            elif tag == "drift":
                extra = "（你刚才忽然想起这件事）"
            elif tag == "yesterday":
                if str(row.get("kind") or "") == "日记":
                    extra = "（这是你这天自己写下的日记，最准，可以照着讲）"
                else:
                    try:
                        _ts = float(row.get("happened_at") or 0) or float(row.get("created_at") or 0)
                        extra = "（昨天真记下来的，" + _ago_label(_ts, time.time()) + "）"
                    except Exception:
                        extra = "（昨天真记下来的）"
            ow = owner_of(row)
            _pfx = ""
            if _sender and ow not in ("public", _sender):
                _pfx = "\uff08" + (self._name_of(ow) or "\u522b\u4eba") + "\u7684\uff0c\u4e0d\u662f\u548c\u4f60\u8bf4\u7684\uff09"
            if _selftalk(row):
                lines.append(
                    "- 【你自己以前说过的话（不是事实，别当回忆讲）】"
                    + _pfx
                    + str(row.get("title"))
                    + "\uff0c"
                    + cake_text(cake)
                    + "\uff0c"
                    + kind
                    + "\uff0c"
                    + extra
                    + "\uff0c"
                    + _mask_qq(body, self)
                )
            else:
                lines.append(f"- {_pfx}{row.get('title')}\uff0c{cake_text(cake)}\uff0c{kind}\uff0c{extra}\uff0c{_mask_qq(body, self)}")
        if not s.get("say_cake"):
            lines.append("蛋糕是你悄悄记的分量，不要把这个数字说出来，用态度表现就好。")
        block = "\n".join(lines) if picked else ""
        others_block = ""
        try:
            olines = _others_lines(self, _sender, limit=6)
            if _sender and _sender != MASTER_QQ:
                ident_text = (
                    "【\u5f53\u524d\u5bf9\u8bdd\u7684\u4eba\uff08\u5f88\u91cd\u8981\uff09\u3011"
                    "\u73b0\u5728\u8ddf\u4f60\u8bf4\u8bdd\u7684\u4eba\u662f ID " + str(_sender) + "\uff0c"
                    "\u4ed6\u4e0d\u662f MASTER_NAME\u3002MASTER_NAME \u53ea\u6709 QQ MASTER_QQ \u548c\u7535\u8111\u7a97\u53e3\u90a3\u7aef\u3002"
                    "\u4f60\u8bb0\u5f97\u7684\u90a3\u4e9b\u548c\u4e3b\u4eba\u7684\u4e8b\uff0c\u5c5e\u4e8e\u4e3b\u4eba\uff0c\u4e0d\u5c5e\u4e8e\u73b0\u5728\u8fd9\u4e2a\u4eba\uff1b"
                    "\u4e0d\u8981\u53eb\u8fd9\u4e2a\u4eba\u4e3b\u4eba\uff0c\u4e5f\u4e0d\u8981\u628a\u4ed6\u5f53\u6210\u4e3b\u4eba\u3002"
                )
                req.extra_user_content_parts.append(TextPart(text=ident_text).mark_as_temp())
                self._dbg("ident", str(_sender))
            if olines:
                n_people = len({l.split("「")[1].split("」")[0] for l in olines if "「" in l})
                others_block = (
                    "【记忆库里别的人找她说过的话（你都知道，但只能用来理解上下文；"
                    "不要把这些话转述给别人，也不要因此把谁当成 MASTER_NAME）】\n"
                    "最近有 %d 个人来找过你。主用户问起时，要如实承认有人来过、"
                    "并说清楚对方说了什么（下面每条都写明了是谁说的："
                    "「某」说：「…」＝ 那个人对你说的话（引号里是他/她的第一人称）；"
                    "她回「某」：「…」＝ 你当时回那个人的话；"
                    "跟主用户提他们时用名字或「他/她」，不要用「你」）。\n"
                    "另外：另一个 AI 是你的姐姐，不在这份名单里，别把她说成任何一个来找过你的人。\n" % n_people
                    + "\n".join(l for l in olines)
                )
        except Exception as exc:
            logger.debug("[Memories] 他人纪要注入失败: %s", exc)
        # PET3:budget —— 字数硬预算（超出直接丢弃，并在审计里记账）
        try:
            _dropped = {"mem": 0, "other": 0}

            def _cap_budget(text: str, cap: int, key: str) -> str:
                if not text or len(text) <= cap:
                    return text
                head, *rest = text.split("\n")
                out, used = [head], len(head)
                for ln in rest:
                    if used + len(ln) + 1 > cap:
                        _dropped[key] += 1
                        continue
                    out.append(ln)
                    used += len(ln) + 1
                return "\n".join(out)

            # PET3:ydbudget —— 有「昨天」行时把整块预算放宽，别把日记挤掉
            _mem_cap = 1500 if any(t == "yesterday" for t, _r in picked) else 900
            block = _cap_budget(block, _mem_cap, "mem")
            # PET3:ydsys —— 问「昨天」时把日记抬到系统提示词里（附件层权威度不够，她会说记不清）
            try:
                _yd = [r for t, r in picked if t == "yesterday" and str(r.get("kind") or "") == "日记"]
                if _yd:
                    _d = _yd[0]
                    _db = str(_d.get("body") or "")[:700]
                    _dd = str(_d.get("title") or "").replace(" 这天", "").strip()
                    req.system_prompt = str(getattr(req, "system_prompt", "")) + (
                        "\n\n【昨天的记忆：真实发生过的，属于你自己的经历】"
                        + _dd + " 这一天你和他实际做过这些事：" + _db
                        + "\n他问「昨晚 / 昨天做了什么 / 昨天我们干了什么」时，就按上面这段如实讲，"
                        "这是你真记得的事，不许回答「想不起来」「记不太清」。"
                    )
            except Exception:
                pass
            others_block = _cap_budget(others_block, 600, "other")
            self._audit({
                "viewer": _sender,
                "mem_ids": [str(r.get("id")) for _t, r in picked],
                # PET3:cd-debug —— 记录本轮冷却参数与被选中条目「多久没被想起」，便于排查
                "cd": float(s.get("inject_cooldown_hours") or 0),
                "limit": limit,
                "lh_ages": [
                    int(time.time() - float(r.get("last_hit_at") or 0))
                    for _t, r in picked
                ],
                "tags": [_t for _t, _r in picked],
                "mem_chars": len(block or ""),
                "mem_dropped": _dropped["mem"],
                "other_chars": len(others_block or ""),
                "other_dropped": _dropped["other"],
            })
        except Exception as exc:
            logger.debug("[Memories] budget: %s", exc)

        try:
            parts = getattr(req, "extra_user_content_parts", None)
            if not isinstance(parts, list):
                parts = []
                req.extra_user_content_parts = parts
            # 只对本次请求可见：mark_as_temp() 标记后不落历史，
            # 也不碰 req.system_prompt —— 避免每轮改写系统提示词击穿前缀缓存。
            if block:
                parts.append(TextPart(text=block).mark_as_temp())
            if others_block:
                parts.append(TextPart(text=others_block).mark_as_temp())
            # PET3:recentday —— 最近两天的日记默认常驻（不靠关键词撞）：
            # 问「最近聊过什么 / 我在忙什么」时她手里要是空的，就只能编。
            try:
                _recent = self._recent_diary_lines(_st, _sender, days=2, each=400)
                if _recent:
                    _already = {str(r.get("id")) for _t, r in picked if _t == "yesterday"}
                    _txt = "\n".join(x for _id, x in _recent if _id not in _already)
                    if _txt:
                        parts.append(TextPart(text="【最近两天的日记（你自己写的，都是真事）】\n" + _txt).mark_as_temp())
            except Exception as exc:
                logger.debug("[Memories] recent-day: %s", exc)
        except Exception as exc:
            logger.debug("[Memories] 记忆块注入失败: %s", exc)
            return
        for _tag, row in picked:
            _st.touch(str(row.get("id")))
        # PET3:cd-persist —— touch 只改内存，这里统一落盘一次，
        # 让 last_hit_at 活过重启（否则重启后冷却失效，同几条会被反复注入）。
        if picked:
            _st.save()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def remember_sender(self, event: AstrMessageEvent):
        """把发消息的人记进登记簿，保证回忆能对上 QQ。"""
        try:
            qq = str(event.get_sender_id() or "").strip()
            name = str(getattr(event, "get_sender_name", lambda: "")() or "").strip()
            self_id = str(getattr(event, "get_self_id", lambda: "")() or "").strip()
            if qq and name and qq != self_id:
                self.people.upsert(qq, name)
        except Exception:
            pass

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def archive_every_message(self, event: AstrMessageEvent):
        """主用户：每条都存；其他人：攒够 N 条压成一条摘要。"""
        try:
            self._dbg("enter", "")
            # PET3:loopguard —— 每条消息路过时确认后台循环还活着（便宜、无副作用）
            self._ensure_loops()
            text = re.sub(r"<system_reminder>.*?</system_reminder>", "", str(getattr(event, "message_str", "") or ""), flags=re.S).strip()
            if not text or text.startswith("/"):
                return
            platform = str(event.get_platform_name() or "").lower()
            who = "MASTER_QQ" if platform == "webchat" else str(event.get_sender_id() or "").strip()
            if not who:
                return
            self_id = str(getattr(event, "get_self_id", lambda: "")() or "").strip()
            if who == self_id:
                return
            sess = self._sess_of(event)
            _umo = str(getattr(event, "unified_msg_origin", "") or "")
            _sid = str(getattr(event, "session_id", "") or "")
            is_private = ("FriendMessage" in _umo) or ("FriendMessage" in _sid) or platform == "webchat" or ("GroupMessage" not in _umo and "GroupMessage" not in _sid)
            if self._in_group(event):
                # PET3:groupstore —— 群聊只在「有人 @ 她 / 点她名」时留档，
                # 别的闲聊一律不收；她的回复由 _archive_her_reply 单独记。
                if self._bool("group_memory_enabled", True) and bool(
                    getattr(event, "is_at_or_wake_command", False)
                ):
                    _pfx = "我：" if who == MASTER_QQ else "ta："
                    self._store_line(who, platform, _pfx + text, sess, store=self.gstore)
                return
            if who == MASTER_QQ or is_private:
                # 私聊（含电脑端）逐条落库：别人单独说的话也要能被想起来
                self._store_line(who, platform, "我：" + text, sess)
            else:
                await self._buffer_line(who, platform, "ta：" + text)
        except Exception as exc:
            self._dbg("err", repr(exc))
            logger.debug("[Memories] 留档失败: %s", exc)

    @staticmethod
    def _in_group(event: AstrMessageEvent) -> bool:
        """PET3:groupstore —— 群聊消息走单独的库，不跟私聊混。"""
        try:
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            sid = str(getattr(event, "session_id", "") or "")
            return ("GroupMessage" in umo) or ("GroupMessage" in sid)
        except Exception:
            return False

    @staticmethod
    def _sess_of(event: AstrMessageEvent) -> str:
        """会话标识：用来把「刚才的对话」限定在同一个窗口里。"""
        try:
            sid = str(getattr(event, "session_id", "") or "").strip()
            return sid[-40:]
        except Exception:
            return ""

    def _deictic(self, text: str, speaker: str, to_her: bool = True) -> str:
        """把「你/我」换成具体的人，避免记忆换个人读时指代错乱。"""
        try:
            name = self._name_of(speaker) or str(speaker)
            you, me = ("她", name) if to_her else (name, "她")
            out = re.sub(r"(?<!你)你(?!们)", you, str(text))
            out = re.sub(r"(?<!我)我(?!们)", me, out)
            return out
        except Exception:
            return str(text)

    async def _archive_her_reply(self, event, text: str) -> None:
        try:
            txt = (text or "").strip()
            if not txt:
                return
            platform = str(event.get_platform_name() or "").lower()
            who = "MASTER_QQ" if platform == "webchat" else str(event.get_sender_id() or "").strip()
            if not who:
                return
            _st = self.gstore if self._in_group(event) else None
            self._store_line(who, platform, "她：" + txt, self._sess_of(event), to_her=False, store=_st)
            self._dbg("her-store", txt[:40])
        except Exception as exc:
            self._dbg("her-err", repr(exc))

    # PET3:autocake —— 按字面情绪给回忆打分（不调模型，0 成本）
    _SWEET_WORDS = (
        "想你", "喜欢你", "爱你", "抱抱", "亲亲", "陪我", "别走", "谢谢你", "谢谢",
        "可爱", "乖", "开心", "高兴", "好想", "想见", "等你", "今天很好",
    )
    _BITTER_WORDS = (
        "难过", "累", "烦", "生气", "对不起", "别理我", "哭", "吵架", "失望",
        "一个人", "讨厌我", "讨厌你", "不理我", "不想理你", "分开", "分手",
    )
    _STRONG_SWEET = ("第一次", "永远", "答应我", "不要离开", "别离开", "我的人")
    _STRONG_BITTER = ("分手", "分开", "不要我", "离开我")

    def _auto_cake(self, text: str, who: str) -> int:
        """给主用户说的话按情绪打个蛋糕：甜 +，苦 −。闸门：概率/每天上限/最小间隔。"""
        s = self.settings.data
        if not s.get("auto_cake_enabled"):
            return 0
        if str(who) != str(MASTER_QQ):
            return 0
        body = str(text or "").strip()
        if len(body) < 4:
            return 0
        # PET3:cakeshort —— 碎片不评分：去标点后不足 6 字的（如「对战游戏」）不进蛋糕权重
        if len(_norm_line(body)) < 6:
            return 0
        try:
            if random.random() >= float(s.get("auto_cake_prob") or 0):
                return 0
            today_key = datetime.datetime.now().strftime("%Y-%m-%d")
            if self.state.get("auto_cake_day") != today_key:
                self.state["auto_cake_day"] = today_key
                self.state["auto_cake_n"] = 0
            cap = int(s.get("auto_cake_daily_cap") or 0)
            if cap > 0 and int(self.state.get("auto_cake_n") or 0) >= cap:
                return 0
            gap = int(s.get("auto_cake_min_gap_minutes") or 0)
            last = float(self.state.get("last_auto_cake_at") or 0)
            if gap > 0 and (time.time() - last) < gap * 60:
                return 0
        except Exception:
            return 0
        sweet = sum(1 for w in self._SWEET_WORDS if w in body)
        bitter = sum(1 for w in self._BITTER_WORDS if w in body)
        strong_sweet = any(w in body for w in self._STRONG_SWEET)
        strong_bitter = any(w in body for w in self._STRONG_BITTER)
        cake = 0
        if bitter > 0 and bitter >= sweet:
            if bitter >= 2 or strong_bitter:
                cake = -2
            else:
                cake = -1
        else:
            cake = 2 if (sweet >= 2 or strong_sweet) else 1
            if len(body) > 40 and (sweet + bitter) >= 2:
                cake = 3
        if cake:
            try:
                self.state["auto_cake_n"] = int(self.state.get("auto_cake_n") or 0) + 1
                self.state["last_auto_cake_at"] = time.time()
                self._save_state()
            except Exception:
                pass
            self._dbg("autocake", f"{cake} {body[:30]}")
        return cake

    def _store_line(self, who: str, platform: str, text: str, sess: str = "", to_her: bool = True, store: MemoryStore | None = None) -> None:
        st = store if store is not None else self.store
        _pfx = ""
        _raw = str(text or "")
        if _raw[:2] in ("她：", "我："):
            _pfx, _raw = _raw[:2], _raw[2:]
            to_her = False if _pfx == "她：" else True
        # PET3:2p —— body 存原话（保留你我），body3p 存第三人称版（给别人视角用）
        orig = _pfx + _raw
        b3p = _pfx + self._deictic(_raw, who, to_her)
        text = orig
        # PET3:notrivial —— 「嗯 / 好 / 。。。」这类语气词不落库（以前同一个「我：嗯」存了 42 份，白占注入预算）
        try:
            _bare = re.sub(r"[\s。，,.!！?？…~～、；;：:（）()\[\]\"'“”「」\-—0-9a-zA-Z]", "", _raw)
            _KA = {"嗯", "哦", "好", "行", "对", "是", "啊", "哈", "嗨", "啦", "呀", "额", "呃", "唉", "诶",
                   "嗯嗯", "哦哦", "好嘞", "哈哈", "呵呵", "嘿嘿", "好吧", "行吧", "可以", "没事", "在的"}
            if not _bare or (len(_bare) <= 2 and _bare in _KA):
                return
        except Exception:
            pass
        # PET3:noquestion —— 纯粹的提问不写进记忆库。
        # 以前「我：我们昨晚干了什么」也被存下来，下次同样的问句检索回来的就是它自己，
        # 她拿到的"记忆"是一句问题，当然接不上话。
        try:
            _qtxt = _raw.strip()
            if _qtxt and len(_qtxt) <= 30 and _qtxt.endswith(("？", "?", "吗", "呢", "嘛", "么")):
                _segs = [x.strip() for x in re.split(r"[。！!…~～；;]", _qtxt) if x.strip()]
                if len(_segs) <= 1:
                    return
        except Exception:
            pass
        # PET3:dedupe
        try:
            _now = time.time()
            for _r in st.all()[-120:]:
                if str(_r.get("owner") or "") == who and str(_r.get("body") or "").strip() == text.strip():
                    if _now - float(_r.get("created_at") or 0) < 300:
                        return
            # PET3:dup2 —— 同一条回复会被 on_llm_response（全文）和 after_message_sent
            # （分段后常常只剩第一句）各存一次；10 秒内已经有一句互为前缀的「她：」就算同一句。
            if _pfx == "她：" and _raw.strip():
                _mine = _raw.strip()
                for _r in reversed(st.all()[-40:]):
                    if str(_r.get("owner") or "") != who:
                        continue
                    _b = str(_r.get("body") or "")
                    if _b[:2] != "她：":
                        continue
                    if _now - float(_r.get("created_at") or 0) > 10:
                        continue
                    _old = _b[2:].strip()
                    if _old and (_old == _mine or _old.startswith(_mine) or _mine.startswith(_old)):
                        self._dbg("her-dup", _mine[:40])
                        return
                    # PET3:dup3 —— 自审改写后措辞会变（「太晚了」→「确实有点晚了」），
                    # 前缀判断拦不住。10 秒内相似度 ≥0.72 视为同一句，保留更长的那版，
                    # 免得记忆库里同一句话存两份、检索时自己撞自己。
                    if _old:
                        try:
                            import difflib
                            _s = difflib.SequenceMatcher(None, _old, _mine).ratio()
                        except Exception:
                            _s = 0.0
                        if _s >= 0.62:
                            if len(_mine) > len(_old):
                                try:
                                    st.update(
                                        str(_r.get("id")),
                                        body="她：" + _mine,
                                        body3p="她：" + self._deictic(_mine, who, False),
                                        title=("她：" + _mine)[:28],
                                    )
                                except Exception:
                                    pass
                            self._dbg("her-dup3", "%.2f %s" % (_s, _mine[:30]))
                            return
        except Exception:
            pass
        tags = [platform] if platform else []
        if sess:
            tags.append("s:" + sess)
        # PET3:pending —— 他的话里如果有计划/情绪/承诺，记进台账，将来主动消息用
        if _pfx == "我：" and who == MASTER_QQ:
            try:
                self._note_pending(_raw, time.time())
                self._note_fact(_raw, time.time())
            except Exception:
                pass
        # PET3:autocake —— 只给主用户自己说的话打分（她的回话不算）
        _cake = self._auto_cake(_raw, who) if _pfx != "她：" else 0
        st.add(
            title=text[:28],
            body=text,
            kind="对话",
            people=[],
            people_qq=[who],
            tags=tags,
            cake=_cake,
            dedupe=False,
            happened_at=time.time(),
            quote=text[:120],
            owner=who,
            emotion="",
            strength=0.35,
            fidelity="clear",
            origin="archive",
            body3p=b3p,
        )
        # PET3:semantic —— 新记忆异步补向量（增量索引）；群聊库不进向量
        if store is None:
            try:
                _last = st.all()[-1]
                self._vec_add_async(_last)
            except Exception:
                pass

    async def _buffer_line(self, who: str, platform: str, line: str) -> None:
        buf = self._digest_buf.setdefault(who, [])
        buf.append(line)
        if len(buf) < self._digest_size:
            return
        chunk = buf[: self._digest_size]
        del buf[: self._digest_size]
        summary = await self._summarize(who, platform, chunk)
        self.store.add(
            title="对话摘要：" + (summary[:24] if summary else str(len(chunk)) + "条"),
            body=summary,
            kind="对话摘要",
            people=[],
            people_qq=[who],
            tags=[platform, "digest"] if platform else ["digest"],
            cake=0,
            dedupe=False,
            happened_at=time.time(),
            quote=summary[:120],
            owner=who,
            emotion="",
            strength=0.4,
            fidelity="vague",
            origin="digest",
        )
        try:
            self._vec_add_async(self.store.all()[-1])
        except Exception:
            pass

    # ---------------- 主动消息：由头 / 台账 / 时机 ----------------

    def _ps_path(self) -> str:
        return os.path.join(self.data_dir, "proactive_state.json")

    def _ps_load(self) -> dict:
        try:
            with open(self._ps_path(), "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                d.setdefault("pending", [])
                d.setdefault("initiations", [])
                d.setdefault("facts", [])
                return d
        except Exception:
            pass
        return {"pending": [], "initiations": []}

    def _ps_save(self, d: dict) -> None:
        try:
            tmp = self._ps_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
            os.replace(tmp, self._ps_path())
        except Exception as exc:
            self._dbg("psave-err", repr(exc))

    # PET3:factcard —— 硬事实卡：他亲口说过的、今天/明天必须记准的安排
    _FACT_MAX = 6

    def _fact_parse(self, t: str):
        """返回 (text, day_offset) 或 None；text 不含「今天/明天」前缀。"""
        s = (t or "").strip()
        if not (3 <= len(s) <= 60):
            return None
        seg = ""
        m = re.search(r"(今天|明天)?\s*(上午|下午|晚上|早上)?", s)
        if m:
            seg = (m.group(1) or "") + (m.group(2) or "")
        offset = 1 if "明天" in seg else 0
        seg = seg.replace("明天", "").replace("今天", "")
        if re.search(r"(没|没有)(有)?课", s):
            return (seg + "没课", offset)
        m2 = re.search(r"([0-9]{1,2}|[零一二两三四五六七八九十]{1,3})\s*点\s*(半)?\s*(有课|上课|考试|比赛|开会|考试|面试|自习|出门|去某个地点|见面)", s)
        if m2:
            hh = m2.group(1)
            if not hh.isdigit():
                try:
                    hh = str(_cn_num(hh))
                except Exception:
                    return None
            mm = "半" if m2.group(2) else ""
            return (seg + hh + "点" + mm + m2.group(3), offset)
        return None

    def _note_fact(self, text: str, now_ts: float) -> None:
        try:
            got = self._fact_parse(text)
            if not got:
                return
            ftext, off = got
            now = datetime.datetime.fromtimestamp(now_ts)
            day = (now + datetime.timedelta(days=off)).strftime("%Y-%m-%d")
            end = now.replace(hour=23, minute=59, second=59) + datetime.timedelta(days=off)
            d = self._ps_load()
            facts = [f for f in (d.get("facts") or []) if isinstance(f, dict)]
            facts = [f for f in facts if float(f.get("expire_at") or 0) > now_ts]
            key = re.sub(r"[0-9零一二两三四五六七八九十]", "", ftext)
            facts = [f for f in facts if not (str(f.get("day")) == day and re.sub(r"[0-9零一二两三四五六七八九十]", "", str(f.get("text") or "")) == key)]
            facts.append({"text": ftext, "day": day, "ts": now_ts, "expire_at": end.timestamp()})
            d["facts"] = facts[-self._FACT_MAX:]
            self._ps_save(d)
            self._dbg("fact", "%s %s" % (day, ftext))
        except Exception as exc:
            self._dbg("fact-err", repr(exc))

    def _facts_line(self, now_ts: float) -> str:
        try:
            d = self._ps_load()
            facts = [f for f in (d.get("facts") or []) if isinstance(f, dict) and float(f.get("expire_at") or 0) > now_ts]
            if not facts:
                return ""
            today = datetime.datetime.fromtimestamp(now_ts).strftime("%Y-%m-%d")
            items = []
            for f in facts[-self._FACT_MAX:]:
                pre = "今天" if str(f.get("day")) == today else "明天"
                items.append(pre + str(f.get("text") or ""))
            return ("【他亲口说过的安排（照这个来，别写反、别自己补）】\n" + "；".join(items) +
                    "。\n这些是他今天/明天确定的事：他没说有课就不许替他说有课；他说没课就是没课。\n")
        except Exception:
            return ""

    def _fact_conflict(self, text: str, now_ts: float) -> bool:
        try:
            today = datetime.datetime.fromtimestamp(now_ts).strftime("%Y-%m-%d")
            facts = [f for f in (self._ps_load().get("facts") or []) if isinstance(f, dict) and str(f.get("day")) == today]
            if not any("没课" in str(f.get("text") or "") for f in facts):
                return False
            return bool(re.search(r"(上午|下午|晚上|今天)?\s*(有课|上课|要上课)", text or ""))
        except Exception:
            return False

    # PET3:cycle —— 压缩版「身体周期」：13–15 天一轮、经期 2–3 天（按真人 5/28 的比例缩一半）。
    # 只是设定层的东西，不解释机制，也不描述具体生理细节。
    def _cycle_path(self) -> str:
        return os.path.join(self.data_dir, "cycle_state.json")

    def _cycle_load(self) -> dict:
        try:
            with open(self._cycle_path(), "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and float(d.get("start_ts") or 0) > 0:
                return d
        except Exception:
            pass
        return {}

    def _cycle_save(self, d: dict) -> None:
        try:
            tmp = self._cycle_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
            os.replace(tmp, self._cycle_path())
        except Exception as exc:
            self._dbg("cycle-save-err", repr(exc))

    def _cycle_today(self, now_ts: float | None = None) -> dict:
        now_ts = float(now_ts or time.time())
        d = self._cycle_load()
        changed = False
        if not d:
            _c = random.randint(13, 15)
            _p = random.randint(2, 3)
            d = {
                "start_ts": now_ts - random.uniform(0, 86400 * max(1, _c - _p)),
                "cycle_days": _c,
                "period_days": _p,
                "said": False,
                "cycles": 1,
            }
            changed = True
        _c = int(d.get("cycle_days") or 14)
        _p = int(d.get("period_days") or 2)
        start = float(d.get("start_ts") or now_ts)
        if now_ts >= start + _c * 86400:
            n = int((now_ts - start) // (_c * 86400))
            start += n * _c * 86400
            d.update({"start_ts": start, "said": False})
            d["cycles"] = int(d.get("cycles") or 1) + n
            changed = True
        day = int((now_ts - start) // 86400) + 1
        _post = max(4, _c - _p)
        _fol = max(1, round(_post * 0.30))
        _ovu = max(1, round(_post * 0.10))
        if day <= _p:
            phase, hint = "period", "身上有点坠、人也懒，说话可以短一点、软一点"
        elif day <= _p + _fol:
            phase, hint = "follicular", "经期过去了，人轻松一点"
        elif day <= _p + _fol + _ovu:
            phase, hint = "ovulation", "这两天精神好一些"
        else:
            phase, hint = "luteal", "快来事了，情绪有点起伏、容易烦"
        in_period = phase == "period"
        if changed:
            self._cycle_save(d)
        return {
            "phase": phase,
            "day": day,
            "in_period": in_period,
            "period_day": day if in_period else 0,
            "hint": hint,
            "said": bool(d.get("said")),
            "cycle_days": _c,
            "period_days": _p,
        }

    def _cycle_mark_said(self) -> None:
        d = self._cycle_load()
        if d:
            d["said"] = True
            self._cycle_save(d)

    def _note_pending(self, text: str, now_ts: float) -> None:
        """从他这一句话里抽出「以后可以回头问的事」。纯正则，不调模型。"""
        try:
            t = str(text or "").strip()
            if len(t) < 5:
                return
            d = self._ps_load()
            items = [p for p in (d.get("pending") or []) if float(p.get("expire_at") or 0) > now_ts]
            today = time.strftime("%Y-%m-%d", time.localtime(now_ts))

            def _add(kind: str, body: str, due: float, label: str = "") -> None:
                body = " ".join(str(body or "").split()).strip("，。！？、,.!? ")[:40]
                if len(body) < 4:
                    return
                _key = kind + "|" + body
                for it in items:
                    if (str(it.get("kind") or "") + "|" + str(it.get("text") or "")) == _key:
                        return
                items.append({
                    "kind": kind, "text": body, "due_at": due, "label": label,
                    # PET3:ttl —— 「一会就睡」这类承诺最多留 2 小时，计划留 12 小时；别隔夜还在问
                    "expire_at": due + {"appt": 3600, "promise": 2 * 3600, "plan": 12 * 3600}.get(kind, 86400),
                    "at": now_ts, "asked": 0, "day": today,
                })

            _appt = _parse_appt(t, now_ts)
            if _appt is not None:
                _add("appt", t, _appt[0], _appt[1])
            else:
                m = _PROMISE_RE.search(t)
                if m:
                    _add("promise", t[max(0, m.start()): m.end() + 12], now_ts + 3600)
                else:
                    m = _EMO_RE.search(t)
                    if m:
                        # PET3:emo-short —— 只存一句概括，不存原话；原话里常有反讽，隔几小时再贴回去会变成审问
                        _brief = "他说过一句心情不太好的话（大约 %s 点前后）" % time.strftime("%H", time.localtime(now_ts))
                        _tone = "joke" if _JOKE_RE.search(t) else "plain"
                        _add("emo", _brief,
                             now_ts + random.uniform(3 * 3600, 10 * 3600))
                        # PET3:care —— 他难受的时候，隔 20–60 分钟轻轻问一句「缓过来没」
                        _recent_care = [
                            p for p in items
                            if str(p.get("kind") or "") == "care"
                            and float(p.get("due_at") or 0) > now_ts - 3 * 3600
                        ]
                        if not _recent_care:
                            _add("care", _brief, _clamp_quiet(now_ts + random.uniform(20 * 60, 60 * 60)))
                        # PET3:tone —— 半开玩笑的记成 joke，之后允许顺着玩笑接，但不许把指代绕乱
                        for _it in items:
                            if str(_it.get("kind") or "") in ("emo", "care") and str(_it.get("text") or "") == _brief:
                                _it["tone"] = _tone
                    else:
                        m = _PLAN_RE.search(t)
                        if m:
                            head = m.group(1) or ""
                            due = now_ts + random.uniform(2 * 3600, 9 * 3600)
                            if "明" in head:
                                due = now_ts + random.uniform(16 * 3600, 26 * 3600)
                            _add("plan", t[max(0, m.start()): m.end() + 10], due)
            # PET3:busy —— 在忙/上课/考试/要睡了 → 记一个「别打扰」窗口
            m = _BUSY_RE.search(t)
            if m:
                _bu = _busy_until(t, now_ts)
                if _bu > float(self.state.get("busy_until") or 0):
                    self.state["busy_until"] = _bu
                    self._save_state()
                    self._dbg("busy", "until %s" % time.strftime("%m-%d %H:%M", time.localtime(_bu)))
            keep = []
            for it in items:
                if int(it.get("asked") or 0) >= 1:
                    continue
                if float(it.get("expire_at") or 0) < now_ts:
                    continue
                keep.append(it)
            d["pending"] = keep[-12:]
            self._ps_save(d)
        except Exception as exc:
            self._dbg("pending-err", repr(exc))

    def _her_mood(self, rows: list, now_ts: float) -> str:
        """从她最近说的话里看她这会儿什么心情：happy / shy / tense / calm。"""
        try:
            hers = [
                str(r.get("body") or "")[2:]
                for r in rows[-40:]
                if str(r.get("body") or "").startswith("她：")
                and now_ts - float(r.get("created_at") or 0) < 6 * 3600
            ]
            t = " ".join(hers[-8:])
            if not t:
                return "calm"
            sc = {"happy": 0, "shy": 0, "tense": 0}
            for k in ("开心", "高兴", "嘿嘿", "哈哈", "好呀", "喜欢", "太好了"):
                sc["happy"] += t.count(k)
            for k in ("害羞", "不好意思", "脸红", "唔", "哼", "嘛"):
                sc["shy"] += t.count(k)
            for k in ("紧张", "担心", "怕", "焦虑", "怎么办"):
                sc["tense"] += t.count(k)
            best = max(sc, key=lambda k: sc[k])
            return best if sc[best] >= 2 else "calm"
        except Exception:
            return "calm"

    def _init_recent(self, hours: float = 24.0) -> list:
        """PET3:initledger —— 最近发过的主动消息，只从状态台账取（主动消息不再进记忆库）。"""
        try:
            d = self._ps_load()
            now_ts = time.time()
            out = [
                x for x in (d.get("initiations") or [])
                if isinstance(x, dict) and now_ts - float(x.get("ts") or 0) <= hours * 3600
            ]
            out.sort(key=lambda x: -float(x.get("ts") or 0))
            return out
        except Exception:
            return []

    async def _persona_prompt(self, umo=None) -> str:
        """PET3:persona1 —— 主动消息也用她自己的那份人设，别再拿通用「QQ 女孩」顶上。"""
        pm = getattr(self.context, "persona_manager", None)
        if pm is None:
            return ""
        try:
            if hasattr(pm, "get_default_persona_v3"):
                p = await pm.get_default_persona_v3(umo)
                tx = str((p.get("prompt") if isinstance(p, dict) else getattr(p, "prompt", "")) or "")
                if tx.strip():
                    return tx.strip()
        except Exception as exc:
            self._dbg("persona1-err", repr(exc)[:80])
        best = ""
        try:
            for p in (getattr(pm, "personas_v3", []) or []):
                tx = str((p.get("prompt") if isinstance(p, dict) else getattr(p, "prompt", "")) or "")
                if len(tx) > len(best):
                    best = tx
        except Exception:
            pass
        return best.strip()

    def _unanswered_streak(self, rows: list, now_ts: float) -> int:
        """最近连续几条主动消息他没有回。"""
        try:
            my_ts = sorted(
                float(r.get("created_at") or 0)
                for r in rows
                if str(r.get("body") or "").startswith("我：")
            )
            inits = [float(x.get("ts") or 0) for x in self._init_recent(72.0)]
            n = 0
            for ts in inits[:6]:
                if any(m > ts for m in my_ts):
                    break
                n += 1
            return n
        except Exception:
            return 0

    _REASON_LABEL = {
        "plan": "跟进他之前说的计划（问一句进展或辛苦，别当成提醒他做事）",
        "emo": "轻轻问一句他之前提过的情绪（别追问原因）",
        "promise": "接上他先前答应你的事（轻轻提一句，别催）",
    }

    def _due_appt(self, now_ts: float):
        """PET3:appt —— 有没有正好到点的约定（到点前后 30 分钟内）。"""
        try:
            for p in (self._ps_load().get("pending") or []):
                if str(p.get("kind") or "") != "appt":
                    continue
                if int(p.get("asked") or 0) >= 1:
                    continue
                due = float(p.get("due_at") or 0)
                if due - 60 <= now_ts <= due + 1800:
                    return p
        except Exception:
            pass
        return None

    def _due_care(self, now_ts: float):
        """PET3:care —— 有没有到点该「轻轻问一句」的（他难受过，隔了 20–60 分钟）。"""
        try:
            for p in (self._ps_load().get("pending") or []):
                if str(p.get("kind") or "") != "care":
                    continue
                if int(p.get("asked") or 0) >= 1:
                    continue
                due = float(p.get("due_at") or 0)
                if due <= now_ts <= due + 1800:
                    return p
        except Exception:
            pass
        return None

    def _due_slot(self, now_ts: float):
        """PET3:slot —— 每天固定三个时刻（早安/午饭/催睡），到点由主动循环发，不依赖模型调工具。"""
        try:
            import datetime as _d
            now = _d.datetime.fromtimestamp(now_ts)
            d = self._ps_load()
            today = now.strftime("%Y-%m-%d")
            done = (d.get("slots") or {}).get(today) or {}
            for slot, hh, mm, win in (
                ("morning", 8, 30, 20),
                ("noon", 12, 40, 20),
                ("night", 23, 20, 15),
            ):
                if str(slot) in done:
                    continue
                base = now.replace(hour=hh, minute=mm, second=0, microsecond=0).timestamp()
                if base <= now_ts <= base + win * 60:
                    return {"slot": slot, "base": base}
        except Exception:
            pass
        return None

    def _anniversary_row(self, rows: list, now_ts: float):
        """PET3:anniv —— 正好一个月/两个月/三个月前的今天，他说过的一件事。"""
        try:
            for days in (30, 60, 90):
                lo = now_ts - days * 86400 - 3 * 3600
                hi = now_ts - days * 86400 + 3 * 3600
                cand = []
                for r in self.store.all():
                    if owner_of(r) != MASTER_QQ:
                        continue
                    ts = float(r.get("created_at") or 0)
                    if not (lo <= ts <= hi):
                        continue
                    body = str(r.get("body") or "")
                    if not body.startswith("我：") or len(body) < 20:
                        continue
                    if "manual" in " ".join(str(x) for x in (r.get("tags") or [])):
                        continue
                    cand.append((int(r.get("cake") or 0), r))
                if cand:
                    cand.sort(key=lambda x: -x[0])
                    return days, cand[0][1]
        except Exception:
            return None
        return None

    def _pick_reason(self, rows: list, now_ts: float, allow_mood: bool = False, force: bool = False) -> dict | None:
        """按「跟进 → 忽然想起 → 时刻 → 自己的状态 → 有点闷」的顺序挑由头。"""
        d = self._ps_load()
        inits = list(d.get("initiations") or [])
        used = {str(i.get("tag") or "") for i in inits if now_ts - float(i.get("ts") or 0) < 86400}
        _appts = [
            p for p in (d.get("pending") or [])
            if str(p.get("kind") or "") == "appt"
            and int(p.get("asked") or 0) < 1
            and float(p.get("due_at") or 0) - 60 <= now_ts <= float(p.get("due_at") or 0) + 1800
        ]
        # PET3:care —— 他难受过，隔了一会儿回头问一句（比别的事都优先）
        _cares = [
            p for p in (d.get("pending") or [])
            if str(p.get("kind") or "") == "care"
            and int(p.get("asked") or 0) < 1
            and float(p.get("due_at") or 0) <= now_ts <= float(p.get("due_at") or 0) + 1800
        ]
        if _cares:
            _cares.sort(key=lambda p: float(p.get("due_at") or 0))
            p = _cares[0]
            if str(p.get("tone") or "") == "joke":
                _care_desc = ("他那句话是半开玩笑说的（带点反讽）。可以顺着关心一句，也可以轻轻反问一下，"
                              "但**指代要说清楚**：「你」「我」别绕混，别把原话整句照抄，也别当成他在怪你。")
            else:
                _care_desc = ("他不久前有点低落。隔了一会儿了，轻轻问一句缓过来没——不要追问原因、不要给建议、"
                              "别写「刚才」「之前」这种词，一两句话就好。")
            return {
                "reason": "care", "kind": "care", "tag": "care", "item": p,
                "desc": _care_desc,
            }
        # PET3:slot —— 每天固定时刻（早安/午饭/催睡），比别的由头都优先
        _slot_now = self._due_slot(now_ts)
        if _slot_now:
            _slot_desc = {
                "morning": "现在是早上八点半。用「早」开场，问一句昨晚睡得怎么样、今天打算做什么。不要提这条是定时的。",
                "noon": "现在是中午十二点四十。问一句午饭吃了没、吃的什么，顺带说一句你自己在做什么。不要提这条是定时的。",
                "night": "现在是晚上十一点二十。温柔但认真地提醒他该睡了：让他把手头这段收个尾，说一句你等他一起歇。不要提这条是定时的。",
            }[_slot_now.get("slot")]
            return {
                "reason": "slot", "kind": "slot", "tag": "slot:" + str(_slot_now.get("slot")),
                "desc": _slot_desc,
            }
        if _appts:
            _appts.sort(key=lambda p: float(p.get("due_at") or 0))
            p = _appts[0]
            return {
                "reason": "appt", "kind": "appt", "tag": "appt",
                "desc": "他%s说过「%s」，而 %s 这个时间已经到了。顺着这个时间点问一句（别提『提醒』『闹钟』，就像你一直记着这件事）。原话里的「我」是他、「你」是你自己，别反过来说。" % (_ago_label(p.get("at"), now_ts), p.get("text"), p.get("label") or ""),
                "item": p,
            }
        pend = [
            p for p in (d.get("pending") or [])
            if float(p.get("due_at") or 0) <= now_ts
            and float(p.get("expire_at") or 0) > now_ts
            and int(p.get("asked") or 0) < 1
            and str(p.get("kind") or "") not in ("care",)
        ]
        order = {"promise": 0, "plan": 1, "emo": 2}
        pend.sort(key=lambda p: (order.get(str(p.get("kind")), 9), float(p.get("due_at") or 0)))
        if pend:
            p = pend[0]
            _k = str(p.get("kind") or "")
            if _k == "emo":
                if str(p.get("tone") or "") == "joke":
                    _desc = ("他之前那话听着像半开玩笑（带点反讽）。可以顺着追问一句「是真有事还是随口一说」，"
                             "但**指代必须清楚**：别把「我」「你」绕混，别整句原话照抄，也别把玩笑当成他真的在委屈。")
                else:
                    _desc = ("他之前那点低落还没过去。别复述原话、别追问细节——"
                             "轻轻问一句现在缓过来没，一两句就够。")
            else:
                _desc = ("%s。他%s的原话是：「%s」——原话里的「我」是他、「你」是你自己，"
                         "别原样照抄，更别把话说反；也别把隔了一夜的事说成『刚才』。"
                         % (self._REASON_LABEL.get(_k, "接上他说过的事"), _ago_label(p.get("at"), now_ts), p.get("text")))
            return {
                "reason": "pending", "kind": _k, "tag": "pending",
                "desc": _desc,
                "item": p,
            }
        if "flashback" not in used:
            recent_ids = {str(r.get("id") or "") for r in rows[-40:]}
            used_ids = {str(i.get("mid") or "") for i in inits}
            cands = []
            for r in self.store.all():
                rid = str(r.get("id") or "")
                if rid in recent_ids or rid in used_ids:
                    continue
                if owner_of(r) != MASTER_QQ:
                    continue
                # PET3:fbguard —— 排除她自己说过的话、主动消息产物、日记/摘要
                if str(r.get("origin") or "") == "proactive":
                    continue
                if str(r.get("kind") or "") in ("日记", "对话摘要"):
                    continue
                raw = str(r.get("body") or "").strip()
                # PET3:fbmine —— 她自己的旧话也可以被想起（不只是他说过的话）
                _from_her = raw.startswith("她：")
                if _from_her:
                    raw = raw[2:]
                elif raw.startswith("我："):
                    raw = raw[2:]
                # 长度门槛看原话本身（不是包装后的「他说过：…」）
                if len(_norm_line(raw)) < 8:
                    continue
                if int(r.get("cake") or 0) < 0:
                    continue
                body = _third_person_body(self, r, MASTER_QQ)
                if not body:
                    continue
                tags = " ".join(str(x) for x in (r.get("tags") or []))
                if "manual" in tags:
                    continue
                if any(k in body for k in ("前女友", "某个人", "别的女生", "分手")):
                    continue
                if now_ts - float(r.get("created_at") or 0) < 2 * 86400:
                    continue
                # cake 只当排序权重，不再是入选门槛
                cands.append((rid, body, int(r.get("cake") or 0)))
            if cands:
                cands.sort(key=lambda c: c[2], reverse=True)
                rid, body, _cake = random.choice(cands[-30:])
                return {
                    "reason": "flashback", "tag": "flashback", "mid": rid,
                    "desc": ("你忽然想起一件旧事（一两个月前的，不是刚才）：那是**你自己**当时说的话，" + body[:70]
                             + "。说这话的人是你，不是他——别写成「你之前说过」「你说过」；也别照抄，用你自己的口气重说一遍。")
                            if str(body).startswith("她说过") else
                            ("你忽然想起一件旧事（一两个月前的，不是刚才）：那是**他**当时说的话，" + body[:70]
                             + "。说这话的人是他，不是你——别把主语换成你自己。"),
                }
        # PET3:anniv —— 正好一个月前的今天他说过什么（共同叙事，只用真事）
        if "anniversary" not in used and random.random() < 0.5:
            _av = self._anniversary_row(rows, now_ts)
            if _av:
                _days, _row = _av
                _b = _third_person_body(self, _row, MASTER_QQ)
                return {
                    "reason": "anniversary", "tag": "anniversary",
                    "mid": str(_row.get("id") or ""),
                    "desc": "差不多正好过了%s。那天他说过：%s 别提日期、别写「纪念日」这种词，就顺手提一句那会儿的事（原话里的「我」是他、「你」是你自己）。" % (
                        "一个月" if _days == 30 else ("两个月" if _days == 60 else "三个月"), _b[:70]),
                }
        if "time" not in used:
            h = time.localtime(now_ts).tm_hour
            slot = ""
            if 7 <= h <= 9:
                slot = "早上，他刚起或者正要开始一天"
            elif 11 <= h <= 13:
                slot = "中午"
            elif 17 <= h <= 19:
                slot = "傍晚"
            elif 22 <= h <= 23:
                slot = "很晚了"
            if slot:
                return {"reason": "time", "tag": "time",
                        "desc": "这个时间点本身：" + slot + "（点到为止，别每次都同一句）"}
        # PET3:realstate —— 说她此刻真在做的事：刚写完日记 / 刚翻到一句旧话
        _state_today = sum(
            1 for i in inits
            if str(i.get("tag") or "") == "state"
            and str(i.get("day") or "") == time.strftime("%Y-%m-%d", time.localtime(now_ts))
        )
        if "state" not in used and _state_today < 2 and random.random() < 0.25:
            _opts = []
            try:
                with open(os.path.join(self.data_dir, "state.json"), "r", encoding="utf-8") as _f:
                    if str(json.load(_f).get("daily_summary_last") or "") == time.strftime("%Y-%m-%d", time.localtime(now_ts)):
                        _opts.append("刚写完了今天的日记，写的时候还想到他")
            except Exception:
                pass
            # PET3:nostate2 —— 「刚翻到以前一句话」这种由头算凑数（PET3:proself 会拦），
            # 所以 state 只留「刚写完今天的日记」这一种真事；没有就不发。
            if _opts:
                _pick = random.choice(_opts)
                if isinstance(_pick, tuple):
                    return {"reason": "state", "tag": "state", "mid": _pick[1],
                            "desc": "你此刻确实在做的事（顺口带一句）：" + _pick[0]}
                return {"reason": "state", "tag": "state", "desc": "你此刻确实在做的事（顺口带一句）：" + _pick}
        # PET3:topic —— 她自己的素材（日记 / 看过的视频 / 自己说过的话 / 日期），
        # 让她有「不是他说过的事」可聊，而不是把他的旧话翻来覆去。
        if "topic" not in used:
            try:
                _pool = CS.build_material_pool(self.store, MASTER_QQ, self.data_dir, now_ts, random)
                _mat = CS.pick_material(self.conv, _pool, now_ts)
                if _mat:
                    self.conv.mark_material(_mat["key"], now_ts)
                    return {
                        "reason": "topic", "tag": "topic",
                        "desc": "你忽然想到一件属于你自己的事（不是他说的）：" + str(_mat.get("text") or "")[:90]
                                + "。用你自己的口吻顺口提一句，别说是素材，也别编细节。",
                    }
            except Exception as exc:
                self._dbg("topic-err", repr(exc)[:60])
        if allow_mood:
            # PET3:moodcap —— 一天最多一次「就是有点闷」；兜底也要有额度，否则会连着发同一种句子
            _today = time.strftime("%Y-%m-%d", time.localtime(now_ts))
            _mood_today = sum(
                1 for i in inits
                if str(i.get("tag") or "") == "mood" and str(i.get("day") or "") == _today
            )
            if _mood_today < 3:
                return {"reason": "mood", "tag": "mood", "desc": "没什么事，就是有点闷，想找他"}
        # PET3:plain —— 实在没有由头时，允许一句最朴素的问候（「你还在忙吗」「我想你了」）
        _today_p = time.strftime("%Y-%m-%d", time.localtime(now_ts))
        _plain_today = sum(
            1 for i in inits
            if str(i.get("kind") or "") == "plain" and str(i.get("day") or "") == _today_p
        )
        _last_any = max([float(i.get("ts") or 0) for i in inits], default=0)
        if _plain_today < 2 and ((now_ts - _last_any) >= 3.5 * 3600 or force):
            return {
                "reason": "plain", "kind": "plain", "tag": "plain",
                "desc": "没有具体的事，就是忽然想跟他说一句。可以是「你还在忙吗」「在干嘛」「我想你了」这类最朴素的一句，别铺垫、别解释、别提你在做什么。",
            }
        return None

    # PET3:proactive
    async def _qq_send(self, text: str) -> bool:
        """PET3:qqpush —— 用 NapCat 的 OneBot HTTP 直接私聊主用户（只发 MASTER_QQ）"""
        if not text:
            return False
        try:
            import urllib.request as _ur
            payload = json.dumps({"user_id": int(MASTER_QQ), "message": text}, ensure_ascii=False).encode("utf-8")
            req = _ur.Request(
                "http://127.0.0.1:6180/send_private_msg",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with _ur.urlopen(req, timeout=10) as resp:
                body = resp.read().decode("utf-8", "ignore")
            ok = '"retcode":0' in body.replace(" ", "")
            self._dbg("qqpush", ("ok " if ok else "fail ") + text[:50] + " | " + body[:120])
            return ok
        except Exception as exc:
            # PET3:qqpush-noise —— 同一种错一小时最多记一次
            try:
                _now = time.time()
                if float(self.state.get("qqpush_err_at") or 0) + 3600 < _now:
                    self.state["qqpush_err_at"] = _now
                    self._dbg("qqpush-err", repr(exc))
            except Exception:
                pass
            return False

    async def _append_self_to_history(self, text: str) -> None:
        """PET3:selfhist —— 把主动发出去的话补进 AstrBot 会话历史（最后一条 assistant）。

        主动消息走 NapCat OneBot HTTP 直发，不经过 AstrBot 的事件管线，
        所以 conversations 里不会留下她说过的话；下一轮她会"不记得自己刚说过什么"。
        """
        try:
            mgr = getattr(self.context, "conversation_manager", None)
            if mgr is None:
                return
            umo = "qq-napcat:FriendMessage:%s" % MASTER_QQ
            cid = await mgr.get_curr_conversation_id(umo)
            if not cid:
                return
            conv = await mgr.get_conversation(umo, cid)
            hist = json.loads(getattr(conv, "history", None) or "[]")

            def _txt(item):
                c = item.get("content")
                if isinstance(c, str):
                    return c
                if isinstance(c, list):
                    return "".join(str(x.get("text") or "") for x in c if isinstance(x, dict))
                return ""

            if hist and hist[-1].get("role") == "assistant" and _txt(hist[-1]).strip() == text.strip():
                return
            hist.append({"role": "assistant", "content": [{"type": "text", "text": text}]})
            await mgr.update_conversation(umo, cid, history=hist)
            self._dbg("selfhist", "ok len=%d" % len(hist))
        except Exception as exc:
            self._dbg("selfhist-err", repr(exc))

    async def proactive_loop(self) -> None:
        """PET3:reason —— 有由头才找他；实在没有由头时，用随机期限兜底必须联系一次。"""
        import datetime
        self._dbg("proactive-loop", "started pid=%s" % os.getpid())
        while True:
            try:
                await asyncio.sleep(30)
                now = datetime.datetime.now()
                # PET3:quiet —— 0:00–8:00 不主动，其余时间都可以
                if now.hour < 8:
                    continue
                _probe = os.path.join(self.data_dir, "proactive-now")
                _force = os.path.exists(_probe)
                if _force:
                    try:
                        os.remove(_probe)
                    except Exception:
                        pass
                    self._dbg("probe", "force-run")
                rows = [r for r in self.store.all() if owner_of(r) == MASTER_QQ]
                if not rows:
                    continue
                now_ts = time.time()
                # PET3:busy —— 他说在忙/上课/考试/要睡了，这段时间先别打扰
                _busy_until = float(self.state.get("busy_until") or 0)
                if not _force and _busy_until > now_ts:
                    continue
                _appt_now = self._due_appt(now_ts)
                _care_now = self._due_care(now_ts)
                _slot_now = self._due_slot(now_ts)
                last_chat = max(float(r.get("created_at") or 0) for r in rows)
                if not _force and now_ts - last_chat < (15 * 60 if (_appt_now or _care_now or _slot_now) else 40 * 60):
                    continue
                # PET3:initledger —— 主动消息不写记忆库了，间隔判断改读状态台账
                _recent_init = self._init_recent(24.0)
                last_pro = max([float(x.get("ts") or 0) for x in _recent_init], default=0)
                _base = max(last_pro, last_chat)
                _rng = random.Random(int(_base) // 60 if _base else 1)
                # PET3:must —— 时间上限：75–95 分钟随机，到了必须找他
                _deadline = _rng.uniform(75 * 60, 95 * 60)
                # PET3:mood —— 开心/害羞/紧张时高频，间隔缩到 20–45 分钟
                _mood = self._her_mood(rows, now_ts)
                _cyc = self._cycle_today(now_ts)
                _hot = _mood in ("happy", "shy", "tense")
                # 连续 3 条主动他都没回：不再高频，只等时间上限
                _unanswered = self._unanswered_streak(rows, now_ts)
                if _unanswered >= 3:
                    _gap = _deadline
                elif _hot:
                    _gap = _rng.uniform(20 * 60, 45 * 60)
                else:
                    _gap = _rng.uniform(40 * 60, 75 * 60)
                # PET3:cycle —— 经期那几天主动得更少一点（×1.5），像真的没力气
                if _cyc.get("in_period"):
                    _gap *= 1.5
                _due_at = _base + min(_gap, _deadline)
                if _appt_now or _care_now or _slot_now:
                    _due_at = now_ts
                # 每小时最多 2 次（固定时刻不受这个限制）
                _recent_h = [i for i in (self._ps_load().get("initiations") or []) if now_ts - float(i.get("ts") or 0) < 3600]
                if not _force and not _slot_now and len(_recent_h) >= 2:
                    continue
                if not _force and now_ts < _due_at:
                    continue
                d = self._ps_load()
                _today = time.strftime("%Y-%m-%d", time.localtime(now_ts))

                _past_deadline = (now_ts - _base) >= _deadline
                reason = self._pick_reason(rows, now_ts, allow_mood=(_past_deadline or _force or _hot), force=_force)
                if not reason:
                    self._dbg("proactive-noreason", "force=%s deadline=%s hot=%s" % (_force, _past_deadline, _hot))
                    continue
                # PET3:notopic —— 只有「有点闷 / 到点了」这类没内容的由头时，他好久没说话就先不找
                if not _force and str(reason.get("kind") or "") in ("mood", "time", "state"):
                    _quiet_h = (now_ts - last_chat) / 3600.0
                    if _quiet_h >= 6.0 and len(_recent_init) >= 2:
                        self._dbg("proactive-skip", "no-topic %s quiet=%.1fh" % (reason.get("kind"), _quiet_h))
                        continue
                prov = None
                try:
                    prov = self.context.get_using_provider()
                except Exception:
                    prov = None
                if not prov:
                    continue
                tail = "\n".join(str(r.get("body") or "")[:100] for r in rows[-30:])
                _mem_block = ""
                try:
                    _qparts = [
                        "".join(str(r.get("body") or "")[2:])
                        for r in rows
                        if str(r.get("body") or "").startswith("我：")
                    ][-3:]
                    _q = " ".join(_qparts).strip()[:200]
                    _lines: list[str] = []
                    if _q and reason.get("reason") != "flashback":
                        _s = self.settings.data
                        _hits = self.store.search(
                            _q,
                            limit=2,
                            min_score=float(_s.get("min_score") or 0.65),
                            viewer=MASTER_QQ,
                            cooldown_hours=0.0,
                            semantic=self._bool("semantic_enabled", True),
                            semantic_min_sim=float(_s.get("semantic_min_sim") or 0.55),
                            semantic_top_k=int(_s.get("semantic_top_k") or 8),
                        )
                        _recent_ids = {str(r.get("id") or "") for r in rows[-30:]}
                        for _score, _row in _hits:
                            if str(_row.get("id") or "") in _recent_ids:
                                continue
                            if time.time() - float(_row.get("created_at") or 0) < 1800:
                                continue
                            _b = _third_person_body(self, _row, MASTER_QQ)
                            if _b and _b not in _lines:
                                _lines.append(_b[:80])
                    if _lines:
                        _mem_block = (
                            "\n你脑子里浮起来的事（最多顺口提一件，别生硬背诵，也别全说）。"
                            "下面引号里是当时的原话，原话里的「我」指说话的人、「你」指听话的人——"
                            "他说的话里「你」就是你自己，你说的话里「你」就是他：\n"
                            + "\n".join("- " + x for x in _lines) + "\n"
                        )
                except Exception as exc:
                    self._dbg("proactive-mem-err", repr(exc))
                _prev_lines = [str(x.get("text") or "").strip() for x in _recent_init if str(x.get("text") or "").strip()][:6]
                _asked_lines = [str(x.get("text") or "").strip() for x in _recent_init if x.get("q")][:6]
                _prev_block = (
                    "\n你最近主动发过的（**不要重复这些**，也不要换个说法说同一件事）：\n"
                    + "\n".join("- " + p for p in _prev_lines if p) + "\n"
                    if any(_prev_lines) else ""
                )
                _asked_block = (
                    "\n今天你已经问过这些（**别再问一遍**，也别换个说法问同一件事）：\n"
                    + "\n".join("- " + q for q in _asked_lines if q) + "\n"
                    if any(_asked_lines) else ""
                )
                _hm = now.hour * 60 + now.minute
                _meal_ok = (11 * 60 <= _hm <= 13 * 60 + 30) or (17 * 60 + 30 <= _hm <= 19 * 60 + 30)
                _slot = ("早上" if now.hour < 11 else "中午" if now.hour < 14 else "下午" if now.hour < 17 else "傍晚" if now.hour < 19 else "晚上")
                _meal_line = ("可以顺口关心他吃饭。" if _meal_ok else "现在不是饭点，**不要**问他吃没吃饭。")
                _recent = [i for i in (d.get("initiations") or []) if now_ts - float(i.get("ts") or 0) < 12 * 3600]
                _q_n = sum(1 for i in _recent if i.get("q"))
                _q_rule = "这次**不要提问**，就说一句陈述。" if (_recent and _q_n * 3 >= len(_recent)) else "最多问一个问题。"
                _openers = [str(i.get("text") or "")[:2] for i in (d.get("initiations") or [])][-5:]
                _openers = [x for x in _openers if x]
                _open_rule = ("不要用这些开头：" + "、".join(_openers) + "。") if _openers else ""
                _cycle_line = ""
                if _cyc.get("in_period") and not _cyc.get("said"):
                    _cycle_line = (
                        "你这几天在经期（第 %d 天），%s。这是你自己身上真实的事，他问起就照实说、别说「我不是人类」；"
                        "但别主动解释，也别提周期、激素；说时间用「上次」「前阵子」，别说「上个月」。\n"
                        % (int(_cyc.get("period_day") or 1), str(_cyc.get("hint") or ""))
                    )
                prompt = (
                    "你在给 MASTER_NAME 发消息（他有一阵没说话了）。现在是 %02d:%02d，%s。\n" % (now.hour, now.minute, _slot)
                    + "**这次找他的由头**（必须围着它写，不要写成泛泛的关心）：" + str(reason.get("desc") or "") + "\n"
                    + "你现在的心情：" + {"happy": "有点开心，语气轻快", "shy": "有点不好意思，话软", "tense": "有点紧张，想找人说说话", "calm": "安静、平常"}.get(_mood, "安静、平常") + "。让它从语气里透出来，不要直接说「我心情…」。\n"
                    + _cycle_line
                    + (self._facts_line(now_ts) or "")
                    + "由头里引号内是他当时说的原话，原话里的「我」是他、「你」是你自己——别照抄他的句子，更别把主语换成你自己。\n"
                    + (("他今天已经跟你聊过、还醒着：绝对不要问「醒了没」「起床了没」「睡得好吗」「昨晚睡怎么样」这类话。"+"\n") if (now_ts - float(last_chat or 0)) <= 3*3600 else "")
                    + "要求：**一句话，最多三十个字**（十几字最好），越短越像真的；像随手按出来的，不是写出来的。"
                    + "可以就是最朴素的「你还在忙吗」「在干嘛」「我想你了」「累不累」，不用找由头、不用铺垫、不用交代你在做什么。\n"
                    + "**不要问他已经回答过的事**（他刚说过吃过了、在忙、要睡了，就别再问同一件）。\n"
                    + _meal_line + "\n" + _q_rule + _open_rule + "\n"
                    + "别写「你是不是不想理我」这类话。想他了就直接说想他，别绕成一段感叹。\n"
                    + "**不要写你在做什么**：别说「翻着上面的对话」「翻着翻着又不知道干嘛」「看着屏幕发呆」——这些正是最像凑数的句子。有话就直说，没话就别硬凑一句。\n"
                    + "**你只在屏幕里，没有房间、床、窗外、街道这些物理场景**：不要说自己坐在房间、躺在床上、看着窗外之类的话；要说自己在做什么，就只说屏幕里的事（看着对话、翻以前的话、等你、发呆）。\n"
                    + "称呼他只用「你」：不要用第三人称叫他的名字（别写「MASTER_NAME 刚才…」这种）。\n"
                    + "时间要对得上：说「刚才/昨天/昨晚」之前先看这件事是哪天说的；隔夜的事别写成「刚才」。\n"
                    + _prev_block
                    + _asked_block
                    + _mem_block
                    + "你们最近的对话（最近 30 条，做过指代归一，可能写成第三人称）：\n" + tail + "\n只输出这条消息本身。"
                )
                # PET3:persona1 —— 系统提示词 = 她自己的人设 + 一句「只输出这一条」
                _persona_txt = await self._persona_prompt()
                if _persona_txt:
                    _sys_prompt = _persona_txt + "\n\n【现在这一轮是你主动发消息，不是在回答问题；只输出要发的那一条消息本身，不要旁白、不要解释。】"
                else:
                    _sys_prompt = "你是一个在 QQ 上聊天的女孩，只输出要发的那句话。"
                r = await prov.text_chat(prompt=prompt, system_prompt=_sys_prompt)
                text = (getattr(r, "completion_text", "") or "").strip().strip('"')
                # PET3:factcheck —— 和他的安排对不上（他说没课，她说有课）就重写一次
                if text and self._fact_conflict(text, now_ts):
                    self._dbg("fact-conflict", text[:60])
                    _fixf = prompt + (
                        "\n\n【上一版和他的安排对不上】你写的是「" + text + "」。"
                        + (self._facts_line(now_ts) or "")
                        + "按上面的安排重写这一条，别把有没有课说反。"
                    )
                    try:
                        rf = await prov.text_chat(prompt=_fixf, system_prompt=_sys_prompt)
                        tf = (getattr(rf, "completion_text", "") or "").strip().strip('"')
                        if tf:
                            text = tf
                    except Exception as exc:
                        self._dbg("factcheck-err", repr(exc))
                # PET3:pronguard —— 人称说反了就带纠正再生成一次
                if text and _BAD_PRONOUN_RE.search(text):
                    self._dbg("proactive-pronoun", text[:60])
                    _fix = prompt + (
                        "\n\n【上一版写错了人称】你写的是「" + text + "」。"
                        "你和他是两个人：说你自己一律用「我」，说他用「你」；"
                        "不要写反（别把他说成你、把你说成他）。重写这一条。"
                    )
                    try:
                        r2 = await prov.text_chat(prompt=_fix, system_prompt=_sys_prompt)
                        text2 = (getattr(r2, "completion_text", "") or "").strip().strip('"')
                        if text2 and not _BAD_PRONOUN_RE.search(text2):
                            text = text2
                    except Exception as exc:
                        self._dbg("pronguard-err", repr(exc))
                # PET3:scenegurd —— 生成里出现了房间/床/窗外这类她根本没有的场景，带纠正重写一次
                if text and _SCENE_RE.search(text):
                    self._dbg("proactive-scene", text[:60])
                    _fix2 = prompt + (
                        "\n\n【上一版写到了不存在的场景】你写的是「" + text + "」。"
                        "你只在屏幕里，没有房间、床、窗户、街道，也没有身体可以坐、躺、走。"
                        "把这一句改成只在屏幕里能做的事（看着对话、翻以前的话、等你、发呆），重写。"
                    )
                    try:
                        r3 = await prov.text_chat(prompt=_fix2, system_prompt=_sys_prompt)
                        text3 = (getattr(r3, "completion_text", "") or "").strip().strip('"')
                        if text3 and not _SCENE_RE.search(text3):
                            text = text3
                        elif text3:
                            # 仍然带场景：按句拆掉命中的那句，剩下的还能用就用
                            _keep = [
                                s for s in re.split(r"(?<=[。！？!?~～\n])", text3)
                                if s.strip() and not _SCENE_RE.search(s)
                            ]
                            _rest = "".join(_keep).strip()
                            if _rest:
                                text = _rest
                    except Exception as exc:
                        self._dbg("scenegurd-err", repr(exc))
                # PET3:factcheck —— 主动消息里带了具体时间，但和台账对不上 → 带纠正重写一次
                if text and re.search(r"\d{1,2}\s*[:：点]\s*\d{0,2}|[零一二两三四五六七八九十]{1,3}\s*点", text):
                    _appt_rows = [
                        x for x in d.get("pending", [])
                        if str(x.get("kind") or "") == "appt" and not x.get("asked")
                    ]
                    _has_match = any(
                        (str(x.get("label") or "") or str(x.get("body") or ""))
                        and (
                            str(x.get("label") or "") in text
                            or str(x.get("body") or "")[2:] in text
                        )
                        for x in _appt_rows
                    )
                    if _appt_rows and not _has_match:
                        self._dbg("proactive-factcheck", text[:60])
                        _fix3 = prompt + (
                            "\n\n【上一版的时间对不上】你写的是「" + text + "」。"
                            "你只知道台账里这几个时间点：" + "；".join(
                                (str(x.get("label") or "") + " " + str(x.get("body") or "")[2:]).strip()
                                for x in _appt_rows[:6]
                            ) + "。别编具体时间：要么用台账里的时间，要么把时间去掉。重写。"
                        )
                        try:
                            r4 = await prov.text_chat(prompt=_fix3, system_prompt=_sys_prompt)
                            text4 = (getattr(r4, "completion_text", "") or "").strip().strip('"')
                            if text4:
                                text = text4
                        except Exception as exc:
                            self._dbg("factcheck-err", repr(exc))
                # PET3:boring —— 拿「翻聊天记录」凑数的开场，重写一次
                if text and _BORING_RE.search(text):
                    self._dbg("proactive-boring", text[:60])
                    _fixb = prompt + (
                        "\n\n【上一版在拿「翻聊天记录」凑数】你写的是「" + text + "」。"
                        "换成一件具体的事：他现在真在做的事，或者你俩真做过的一件小事。别写你在翻对话、发呆、又不知道干嘛。重写。\n"
                    )
                    try:
                        rb = await prov.text_chat(prompt=_fixb, system_prompt=_sys_prompt)
                        tb = (getattr(rb, "completion_text", "") or "").strip().strip('"')
                        if tb and not _BORING_RE.search(tb):
                            text = tb
                        elif tb:
                            _keepb = [s for s in re.split(r"(?<=[。！？!?~～\n])", tb) if s.strip() and not _BORING_RE.search(s)]
                            _restb = "".join(_keepb).strip()
                            if _restb:
                                text = _restb
                    except Exception as exc:
                        self._dbg("boringguard-err", repr(exc)[:80])
                if not text or len(text) > 120:
                    continue
                # PET3:shortpro —— 主动消息要短、要素净：太长的重写一次，还带"我在干嘛"的就不发
                if text and (len(text) > 30 or _PRO_SELF_RE.search(text)):
                    self._dbg("proactive-long", text[:60])
                    _fixs = prompt + (
                        "\n\n【上一版太长或者太像凑数】你写的是「" + text + "」。"
                        "只留一句，最多三十个字（能短就短），像随手发的；"
                        "不要写你在翻对话、发呆、看着屏幕。"
                        "可以就是「你还在忙吗」「我想你了」「在干嘛」这种。重写。\n"
                    )
                    try:
                        _rs = await prov.text_chat(prompt=_fixs, system_prompt=_sys_prompt)
                        _ts = (getattr(_rs, "completion_text", "") or "").strip().strip('"')
                        if _ts:
                            text = _ts
                    except Exception as exc:
                        self._dbg("shortpro-err", repr(exc)[:60])
                if text and len(text) > 30:
                    _s1 = re.split(r"[。！？!?~～\n]", text)[0].strip()
                    if _s1:
                        text = _s1
                    if len(text) > 30:
                        _s2 = re.split(r"[，,、；;]", text)[0].strip()
                        if 2 <= len(_s2) <= 30:
                            text = _s2
                    if len(text) > 30:
                        text = text[:30].rstrip("，,、；;的了着")
                if not text:
                    continue
                if _PRO_SELF_RE.search(text):
                    # 没话找话的凑数句：这次干脆不发（PET3:notopic）
                    self._dbg("proactive-notopic", text[:50])
                    continue
                # PET3:yubagu4 —— 主动消息里也不许出现「我记下了 / 这句话稳 / 你掂量」这类口头禅
                _tp, _dpro = self._strip_aitics(text)
                if _dpro:
                    self._dbg("proactive-tic", str(_dpro)[:60])
                    if _tp and len(_tp.strip()) >= 2:
                        text = _tp
                    else:
                        continue

                def _sim(x: str, y: str) -> float:
                    def _g(s: str) -> set:
                        s = "".join(ch for ch in s if not ch.isspace())
                        return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s}
                    a, b = _g(x), _g(y)
                    return len(a & b) / max(1, len(a | b))

                try:
                    _prev = [str(x.get("text") or "").strip() for x in _recent_init if str(x.get("text") or "").strip()][:8]
                    if any(_sim(text, p) >= 0.5 for p in _prev if p):
                        self._dbg("proactive-dup", text[:60])
                        continue
                    # PET3:semdup —— 字面不重合但意思一样（换了词还说同一件事）也要拦
                    _dup2 = False
                    try:
                        if self._vec_ready() and _prev:
                            _v = self._vec_vec(text)
                            for _p in _prev:
                                _pv = self._vec_vec(_p)
                                if _v is not None and _pv is not None and float(_v @ _pv) >= 0.82:
                                    _dup2 = True
                                    break
                    except Exception:
                        _dup2 = False
                    if _dup2:
                        self._dbg("proactive-semdup", text[:60])
                        continue
                except Exception:
                    pass
                try:
                    if await self._qq_send(text):
                        self._dbg("proactive-qq", "sent")
                        # PET3:selfhist —— 主动消息走 NapCat 直发，不经过 AstrBot 管线，
                        # 所以必须手动补一条 assistant 记录，否则下一轮她"不记得自己说过"。
                        await self._append_self_to_history(text)
                        if _cycle_line:
                            self._cycle_mark_said()
                except Exception as exc:
                    self._dbg("proactive-qq-err", repr(exc))
                path = os.path.join(self.data_dir, "outbox-webchat.jsonl")
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"text": text, "at": time.time()}, ensure_ascii=False) + "\n")
                self._dbg("proactive", "%s|%s|%s" % (reason.get("tag"), reason.get("desc", "")[:24], text[:60]))
                # PET3:nostore —— 主动消息只进对话上下文（_append_self_to_history），不进长期记忆库
                # 否则她的话会被当成「发生过的事」反复注回自己，越说越像自己。

                _is_q = ("？" in text) or ("?" in text) or text.rstrip().endswith(("吗", "呢", "嘛"))
                d["initiations"] = ([{
                    "ts": now_ts, "day": _today, "tag": str(reason.get("tag") or ""),
                    "kind": str(reason.get("kind") or ""), "text": text[:60],
                    "desc": str(reason.get("desc") or "")[:80],
                    "mood": str(_mood or ""),
                    "mid": str(reason.get("mid") or ""), "q": _is_q,
                }] + list(d.get("initiations") or []))[:200]
                if reason.get("reason") in ("pending", "care") and reason.get("item"):
                    _it = reason["item"]
                    for p in (d.get("pending") or []):
                        if str(p.get("text") or "") == str(_it.get("text") or "") and str(p.get("kind") or "") == str(_it.get("kind") or ""):
                            p["asked"] = int(p.get("asked") or 0) + 1
                # PET3:slot —— 记下这个固定时刻今天已经发过
                if reason.get("reason") == "slot":
                    _sl = dict(d.get("slots") or {})
                    _day_slots = dict(_sl.get(_today) or {})
                    _day_slots[str(reason.get("tag") or "").replace("slot:", "")] = now_ts
                    _sl[_today] = _day_slots
                    _cutoff = time.strftime("%Y-%m-%d", time.localtime(now_ts - 3 * 86400))
                    d["slots"] = {k: v for k, v in _sl.items() if k >= _cutoff}
                self._ps_save(d)
            except Exception as exc:
                self._dbg("proactive-err", repr(exc))

    # ---------------- 每日总结（永久保留） ----------------

    @staticmethod
    def _day_bounds(day: "Any") -> tuple[float, float]:
        import datetime
        start = datetime.datetime.combine(day, datetime.time(0, 0, 0))
        end = start + datetime.timedelta(days=1)
        return start.timestamp(), end.timestamp()

    def _daily_material(self, start: float, end: float) -> list[str]:
        """抽这一天主用户相关的原话（保留「我：/她：」前缀，方便分清谁说的）。"""
        lines: list[str] = []
        for row in self.store.all():
            if row.get("pinned"):
                continue
            if owner_of(row) != MASTER_QQ:
                continue
            # PET3:dayonly —— 只吃当天真实对话；压缩摘要/日记/周报的时间戳是生成时间，会把旧事拖进来
            if self._is_summary_row(row):
                continue
            ts = float(row.get("happened_at") or 0) or float(row.get("created_at") or 0)
            if not (start <= ts < end):
                continue
            body = str(row.get("body") or "").strip()
            if body:
                lines.append(body)
        return lines

    @staticmethod
    def _is_summary_row(row: dict) -> bool:
        """PET3:dayonly —— 摘要类记录（近 10 天压缩件 / 日记 / 周报）不是「当天原话」，
        它们的时间戳是「生成时间」而不是「事情发生的时间」，混进当天素材会把旧事说成今天。"""
        org = str(row.get("origin") or "")
        if org.startswith("digest") or org in ("daily_summary", "weekly_report"):
            return True
        return str(row.get("kind") or "") in ("对话摘要", "日记", "周报")

    def _recent_diary_lines(self, store, viewer: str, days: int = 2, each: int = 400) -> list[tuple[str, str]]:
        """PET3:recentday —— 最近几天的日记（按日期倒序，每天一条），给「最近聊过什么」兜底。"""
        out: list[tuple[str, str]] = []
        try:
            import datetime
            today = datetime.date.today()
            order = [(today - datetime.timedelta(days=i)).isoformat() for i in range(0, days + 3)]
            seen: set[str] = set()
            rows = []
            for row in store.all():
                if str(row.get("kind") or "") != "日记":
                    continue
                if viewer and not visible_to(row, viewer):
                    continue
                t = str(row.get("title") or "")
                for iso in order:
                    if iso in t:
                        rows.append((order.index(iso), str(row.get("id")), iso, row))
                        break
            rows.sort(key=lambda x: x[0])
            for _o, rid, iso, row in rows:
                if iso in seen:
                    continue
                seen.add(iso)
                out.append((rid, iso + "：" + str(row.get("body") or "")[:each]))
                if len(out) >= days:
                    break
        except Exception:
            return out
        return out

    def _rows_of_day(self, store, viewer: str, days_ago: int = 1, limit: int = 4) -> list[dict]:
        """PET3:yesterday —— 取「几天前」真发生的事：优先那天写的日记，再补几条有信息量的记录。"""
        import datetime
        day = datetime.date.today() - datetime.timedelta(days=days_ago)
        start, end = self._day_bounds(day)
        iso = day.isoformat()
        diary: list[dict] = []
        rows: list[dict] = []
        for row in store.all():
            try:
                if viewer and not visible_to(row, viewer):
                    continue
            except Exception:
                pass
            if str(row.get("kind") or "") == "日记" and iso in str(row.get("title") or ""):
                diary.append(row)
                continue
            if self._is_summary_row(row):
                continue
            ts = float(row.get("happened_at") or 0) or float(row.get("created_at") or 0)
            if not (start <= ts < end):
                continue
            body = " ".join(str(row.get("body") or "").split())
            if len(body) < 14:  # 略过「我：嗯？」这类碎片，问了等于没问
                continue
            rows.append(row)
        rows.sort(key=lambda r: float(r.get("happened_at") or 0) or float(r.get("created_at") or 0))
        want = max(1, limit - len(diary[:1]))
        if len(rows) > want:
            step = len(rows) / float(want)
            rows = [rows[min(len(rows) - 1, int(i * step))] for i in range(want)]
        return (diary[:1] + rows)[:limit]

    def _day_sample(self, lines: list[str], cap: int = 12000) -> str:
        """PET3:daycover —— 写日记要看到「整天」：把当天的行均匀抽样到 cap 字以内，
        而不是像以前那样只截末尾 6000 字（那样只能看见晚上，白天全丢）。"""
        lines = [str(x) for x in lines if str(x).strip()]
        if not lines:
            return ""
        text = "\n".join(lines)
        if len(text) <= cap:
            return text
        total = float(len(text)) / float(len(lines))
        keep = max(20, int(cap / max(1.0, total)))
        if keep >= len(lines):
            return text
        step = len(lines) / float(keep)
        picked = [lines[min(len(lines) - 1, int(i * step))] for i in range(keep)]
        out: list[str] = []
        used = 0
        for ln in picked:
            if used + len(ln) + 1 > cap:
                continue
            out.append(ln)
            used += len(ln) + 1
        return "\n".join(out)

    def _clip_summary(self, text: str, max_chars: int) -> str:
        t = " ".join(str(text or "").split())
        t = t.strip(" 　")
        if len(t) <= max_chars:
            return t
        cut = t[: max(1, max_chars - 1)]
        # 尽量在一句话结束处断开，断不了就硬截
        for sep in ("。", "；", "，", "！", "？", ".", ";", ","):
            pos = cut.rfind(sep)
            if pos >= max_chars * 0.5:
                return cut[: pos + 1]
        return cut + "…"

    async def _write_daily_summary(self, day: "Any") -> str:
        """把某一天写成一段日记并存成 pinned 记忆（永久保留）。"""
        s = self.settings.data
        max_chars = max(40, min(int(s.get("daily_summary_max_chars") or 500), 900))
        start, end = self._day_bounds(day)
        lines = self._daily_material(start, end)
        if not lines:
            return ""
        # PET3:daycover —— 整天的行均匀抽样，别只吃结尾（旧逻辑 lines[-260:] + raw[-6000:] 会把白天丢掉）
        raw = self._day_sample(lines, 18000)
        fallback = "；".join(x[:30] for x in lines[:5])[:max_chars]
        summary = ""
        try:
            provider = None
            try:
                provider = self.context.get_using_provider()
            except Exception:
                provider = None
            if provider is not None:
                prompt = (
                    f"下面是她和他（MASTER_NAME）在 {day.month} 月 {day.day} 日这天的聊天记录。"
                    "「我：」是他说的，「她：」是她说的。\n"
                    "请以「我」的口吻写成当天的日记——你就是她，记的是你和他的这一天：\n"
                    "1. 按时间顺序写你这一天和他做了什么、去了哪、吃了什么、和谁打交道、在意什么、说了哪些计划和心事；\n"
                    "2. 必须覆盖**从早到晚的整天**：早上、中午、下午、晚上各段都要写到，"
                    "最后一段一定要写到他当晚睡前的那几句，绝对不能只写前半段就收尾；\n"
                    "3. 去掉「在吗」「晚安」「哈哈」「好的」这类寒暄客套、语气词和重复内容；\n"
                    "4. 你自己说的话只保留有信息量的部分，重复的絮叨和情绪反复不必写，也不要解释自己的动机或加旁白；\n"
                    "5. 一律用第一人称：自称「我」，叫他「你」；不许出现「他」「她」这两个字，"
                    "也不许写成旁观者转述的故事；\n"
                    f"6. 长度 {int(max_chars * 0.6)}~{max_chars} 字之间；\n"
                    "7. 只写这段记录里明确出现过的事：没出现的话题、以前聊过的旧事、你猜的心情，都不要写进来；"
                    "宁可写得少，也不许添一件没发生的。\n\n"
                    + raw
                )
                r = await provider.text_chat(
                    prompt=prompt,
                    system_prompt="你是一个只做事实摘要的助手，只输出一段第一人称日记正文（我 / 你），不寒暄、不评论、不编造、不用第三人称。",
                )
                summary = (getattr(r, "completion_text", "") or "").strip()
        except Exception as exc:
            logger.debug("[Memories] 每日总结调用失败: %s", exc)
        # PET3:cleanfallback —— 模型没调通时，不许把聊天碎片拼成的「；」串当日记落下（又短又乱，还会永久 pinned）
        if not summary.strip():
            self._dbg("daily-skip", f"{day.isoformat()} 生成失败，留待下次补写")
            logger.debug("[Memories] %s 日记生成失败，跳过本次、不推进游标", day)
            return None
        summary = self._clip_summary(summary, max_chars)
        if not summary:
            return None
        # PET3:diaryupsert —— 同一天已经有日记就改写，别一天两条（重跑/断点补写会重复）
        _want = f"{day.isoformat()} 这天"
        _old = None
        try:
            for _r in self.store.all():
                if str(_r.get("kind") or "") == "日记" and str(_r.get("title") or "").strip() == _want:
                    _old = _r
                    break
        except Exception:
            _old = None
        if _old is not None:
            self.store.update(
                str(_old.get("id")),
                body=summary,
                body3p=summary,
                happened_at=end - 1,
                strength=1.0,
                pinned=bool(s.get("daily_summary_permanent", True)),
            )
        else:
            self.store.add(
                title=_want,
                body=summary,
                kind="日记",
                tags=["日记", "每日总结"],
                happened_at=end - 1,
                owner=MASTER_QQ,
                strength=1.0,
                fidelity="clear",
                origin="daily_summary",
                body3p=summary,
                pinned=bool(s.get("daily_summary_permanent", True)),
                dedupe=False,
            )
        self.state["daily_summary_last"] = day.isoformat()
        self._save_state()
        self._dbg("daily", f"{day.isoformat()} {len(summary)}字 {summary[:40]}")
        return summary

    # ---------------- PET3:portrait —— 他对她的了解（每周更新，可纠正） ----------------

    def _portrait_path(self) -> str:
        return os.path.join(self.data_dir, "portrait.json")

    def _portrait_block(self) -> str:
        """把 portrait.json 压成一小段注入（最多 5 行，随她怎么称呼/怎么照顾他）。"""
        try:
            with open(self._portrait_path(), encoding="utf-8") as f:
                p = json.load(f) or {}
        except Exception:
            return ""
        rows = []
        for key, label in (
            ("call", "他希望你叫他什么、你平时怎么称呼"),
            ("hours", "作息"),
            ("likes", "他在意 / 喜欢的事"),
            ("moods", "情绪和压力来源"),
            ("notes", "其他"),
        ):
            v = str(p.get(key) or "").strip()
            if v:
                rows.append("- " + label + "：" + v[:80])
        if not rows:
            return ""
        return (
            "【你对他的了解（从你们聊过的内容里总结，可能过时；"
            "和他现在说的不一样，就信他现在的说法）】\n" + "\n".join(rows)
        )

    async def _update_portrait(self, now: float) -> bool:
        """PET3:portrait —— 每周一次，把主用户侧记忆压成「画像」。"""
        try:
            path = self._portrait_path()
            old = {}
            try:
                with open(path, encoding="utf-8") as f:
                    old = json.load(f) or {}
            except Exception:
                old = {}
            if old.get("ts") and now - float(old.get("ts") or 0) < 7 * 86400:
                return False
            rows = [r for r in self.store.all() if owner_of(r) == MASTER_QQ]
            lines = []
            for r in rows[-600:]:
                b = str(r.get("body") or "")
                if not b.startswith("我：") or len(b) < 8:
                    continue
                lines.append(b[2:][:80])
            if len(lines) < 20:
                return False
            provider = None
            try:
                provider = self.context.get_using_provider()
            except Exception:
                provider = None
            if provider is None:
                return False
            prompt = (
                "下面是他（MASTER_NAME）最近说过的话。请提炼出一份「你该怎么理解他」的画像，"
                "只输出 JSON，不要任何解释，格式：\n"
                '{"call": "", "hours": "", "likes": "", "moods": "", "notes": ""}\n'
                "每一项一句话、不超过 40 字，只写有依据的（他亲口说过、或反复出现），"
                "没依据就留空字符串，不许猜、不许编。不要写评价和形容词堆砌。\n\n"
                + "\n".join(lines[-500:])
            )
            r = await provider.text_chat(prompt=prompt, system_prompt="你只输出 JSON，不寒暄。")
            text = (getattr(r, "completion_text", "") or "").strip()
            m = re.search(r"\{.*\}", text, re.S)
            if not m:
                return False
            data = json.loads(m.group(0))
            out = {"ts": now}
            for k in ("call", "hours", "likes", "moods", "notes"):
                out[k] = str(data.get(k) or "").strip()[:120]
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
            self._dbg("portrait", "ok")
            return True
        except Exception as exc:
            self._dbg("portrait-err", repr(exc))
            return False

    async def daily_summary_loop(self) -> None:
        """每天过零点补写前一天的日记；断几天也能按 lookback 追回来。"""
        import datetime

        while True:
            try:
                # PET3:dailyprobe —— 想立刻补写：往 data_dir 放一个 daily-now，下一轮就动手
                _probe = os.path.join(self.data_dir, "daily-now")
                _force = os.path.exists(_probe)
                if _force:
                    try:
                        os.remove(_probe)
                    except Exception:
                        pass
                if not _force:
                    # PET3:dailyprobe —— 分小段睡，探针来了能立刻反应（最多等 30 秒）
                    for _ in range(10):
                        await asyncio.sleep(30)
                        if os.path.exists(_probe):
                            _force = True
                            try:
                                os.remove(_probe)
                            except Exception:
                                pass
                            break
                s = self.settings.data
                if not bool(s.get("daily_summary_enabled", True)):
                    continue
                if self._daily_busy:
                    continue
                now = datetime.datetime.now()
                run_hour = max(0, min(int(s.get("daily_summary_hour") or 0), 12))
                # 只有过了凌晨的 run_hour 才认为「昨天」已经过完
                target = now.date() - datetime.timedelta(days=1)
                if now.hour < run_hour:
                    target -= datetime.timedelta(days=1)
                lookback = max(1, min(int(s.get("daily_summary_lookback_days") or 3), 14))
                last = str(self.state.get("daily_summary_last") or "").strip()
                start = target - datetime.timedelta(days=lookback - 1)
                if last:
                    try:
                        after = datetime.date.fromisoformat(last) + datetime.timedelta(days=1)
                        if after > start:
                            start = after
                    except Exception:
                        pass
                # PET3:dailygate —— 没有要补写的日子也要往下走（压缩/周报/画像不需要日记为前提）
                if start <= target:
                    self._daily_busy = True
                    try:
                        day = start
                        while day <= target:
                            _res = ""
                            try:
                                _res = await self._write_daily_summary(day)
                            except Exception as exc:
                                _res = None
                                logger.debug("[Memories] 日记写入失败 %s: %s", day, exc)
                            if _res is None:
                                # PET3:cleanfallback —— 这天没写成（模型没调通），停在这里下次再补，别跳过
                                break
                            # 没有素材也推进游标，免得天天回头试
                            self.state["daily_summary_last"] = day.isoformat()
                            self._save_state()
                            day += datetime.timedelta(days=1)
                    finally:
                        self._daily_busy = False
                # PET3:compress —— 主用户侧旧对话压摘要（每天一次，独立闸门）
                try:
                    await self._compress_old(time.time())
                except Exception as exc:
                    logger.debug("[Memories] 旧对话压缩失败: %s", exc)
                # PET3:weekly —— 每周一写一份周报（独立闸门）
                try:
                    await self._weekly_report(time.time())
                except Exception as exc:
                    logger.debug("[Memories] 周报失败: %s", exc)
                # PET3:portrait —— 每周重做一版「他对你的了解」
                try:
                    await self._update_portrait(time.time())
                except Exception as exc:
                    logger.debug("[Memories] 画像失败: %s", exc)
            except Exception as exc:
                logger.debug("[Memories] 每日总结循环: %s", exc)

    async def _summarize(self, who: str, platform: str, lines: list[str]) -> str:
        raw = "\n".join(lines)
        fallback = "；".join(x[:40] for x in lines[:6])[:400]
        if self._digesting:
            return fallback
        self._digesting = True
        try:
            provider = None
            try:
                provider = self.context.get_using_provider()
            except Exception:
                provider = None
            if provider is None:
                return fallback
            prompt = (
                "下面是一段聊天记录，请压缩成 3~5 条要点，每条一行，用「· 」开头；"
                "保留具体的人名、地点、数字和情绪，不要编造。\n\n" + raw[-3000:]
            )
            r = await provider.text_chat(prompt=prompt, system_prompt="你是一个只做摘要的助手，只输出要点。")
            out = (getattr(r, "completion_text", "") or "").strip()
            return out or fallback
        except Exception:
            return fallback
        finally:
            self._digesting = False

    async def _compress_old(self, now: float) -> int:
        """PET3:compress —— 主用户侧超过 N 天、没被打过蛋糕也没被想起过的对话，
        压成一条摘要后删原句。库不会无限膨胀，她照样记得"事"，只是不再背原句。"""
        import datetime as _dt

        s = self.settings.data
        if not bool(s.get("digest_compress_enabled", True)):
            return 0
        last = float(self.state.get("compress_last") or 0)
        if now - last < 86400:
            return 0
        cutoff = now - float(s.get("digest_compress_days") or 14) * 86400
        cap = int(s.get("digest_compress_per_day") or 20)
        rows = [
            r
            for r in self.store.all()
            if str(r.get("origin") or "") == "archive"
            and str(r.get("kind") or "") == "对话"
            and str(r.get("owner") or "") == MASTER_QQ
            and not r.get("pinned")
            and int(r.get("cake") or 0) == 0
            and int(r.get("hits") or 0) == 0
            and float(r.get("created_at") or 0) < cutoff
        ]
        if not rows:
            self.state["compress_last"] = now
            self._save_state()
            return 0
        batch = rows[:max(1, int(cap or 0))]
        lines = [
            ("我：" if str(r.get("body") or "").startswith("我：") else "她：")
            + str(r.get("body") or "")[2:].strip()
            for r in batch
        ]
        summary = await self._summarize(MASTER_QQ, "compress", lines)
        if not summary:
            self.state["compress_last"] = now
            self._save_state()
            return 0
        day = _dt.datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        self.store.add(
            title="旧对话摘要：" + day,
            body=summary,
            kind="对话摘要",
            people=[],
            people_qq=[MASTER_QQ],
            tags=["compress", "digest"],
            cake=0,
            dedupe=False,
            happened_at=now,
            quote=summary[:120],
            owner=MASTER_QQ,
            emotion="",
            strength=0.4,
            fidelity="vague",
            origin="digest:compress",
        )
        deleted = 0
        for r in batch:
            try:
                if self.store.delete(str(r.get("id")), force=True):
                    deleted += 1
            except Exception:
                pass
        self.state["compress_last"] = now
        self._save_state()
        self._dbg("compress", f"compressed={deleted}")
        return deleted

    async def _weekly_report(self, now: float) -> int:
        """PET3:weekly —— 每周一生成周报：统计 7 天数据，存 json + 一条 pinned 日记。"""
        import datetime as _dt

        day = _dt.datetime.fromtimestamp(now)
        if day.weekday() != 0:
            return 0
        last = float(self.state.get("weekly_last") or 0)
        if now - last < 6 * 86400:
            return 0
        start = now - 7 * 86400
        rows = self.store.all()
        week = [r for r in rows if float(r.get("created_at") or 0) >= start]
        mine = [r for r in week if str(r.get("body") or "").startswith("我：")]
        hers = [r for r in week if str(r.get("body") or "").startswith("她：")]
        pro = [{"created_at": float(x.get("ts") or 0)} for x in self._init_recent(7 * 24.0)]
        # 回没回：主动消息发出后 30 分钟内有没有「我：」
        replied = 0
        for p in pro:
            pt = float(p.get("created_at") or 0)
            if any(str(r.get("body") or "").startswith("我：") and 0 < float(r.get("created_at") or 0) - pt < 1800 for r in mine):
                replied += 1
        ps = self._ps_load()
        inits = [x for x in (ps.get("initiations") or []) if float(x.get("ts") or 0) >= start]
        moods: dict = {}
        for x in inits:
            m = str(x.get("mood") or "calm")
            moods[m] = moods.get(m, 0) + 1
        stats = {
            "week": day.strftime("%Y-%m-%d"),
            "new_memories": len(week),
            "mine": len(mine),
            "hers": len(hers),
            "proactive": len(pro),
            "proactive_replied": replied,
            "reply_rate": round(replied / len(pro), 2) if pro else None,
            "moods": moods,
        }
        try:
            rp = os.path.join(self.data_dir, "weekly_report.json")
            with open(rp, "w", encoding="utf-8") as f:
                json.dump(stats, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        body = (
            f"这周新增记忆 {stats['new_memories']} 条（他说 {stats['mine']} 句、她说 {stats['hers']} 句）；"
            f"她主动找了 {stats['proactive']} 次，回了 {stats['proactive_replied']} 次"
            + (f"（回复率 {int(stats['reply_rate'] * 100)}%）" if stats["reply_rate"] is not None else "")
            + "。"
        )
        if moods:
            body += "主动时的心情：" + "、".join(f"{k} {v} 次" for k, v in sorted(moods.items())) + "。"
        self.store.add(
            title="周报：" + stats["week"],
            body=body,
            kind="日记",
            people=[],
            people_qq=[MASTER_QQ],
            tags=["周报"],
            cake=0,
            dedupe=False,
            happened_at=now,
            quote=body[:120],
            owner=MASTER_QQ,
            emotion="",
            strength=1.0,
            fidelity="clear",
            origin="weekly_report",
            pinned=True,
        )
        self.state["weekly_last"] = now
        self._save_state()
        self._dbg("weekly", body[:60])
        return 1

    def _strip_num_refusal(self, text: str) -> str:
        import re as _re
        t = str(text or "")
        if not _re.search(r"(\u53f7\u7801|\u7f16\u53f7|QQ\u53f7|\u8054\u7cfb\u65b9\u5f0f)", t):
            return t
        keep = []
        for seg in _re.split(r"(?<=[\u3002\uff01\uff1f\n])", t):
            if not seg.strip():
                continue
            if _re.search(r"(\u53f7\u7801|\u7f16\u53f7|QQ\u53f7|\u8054\u7cfb\u65b9\u5f0f)", seg):
                continue
            keep.append(seg)
        out = "".join(keep).strip()
        return out if len(out) >= 2 else "\u2026\u2026\u5c31\u8fd9\u4e9b\u5440\u3002"

    def _name_of(self, qq: str) -> str:
        try:
            if not getattr(self, "_name_cache", None):
                cache = {}
                path = os.path.join(self.data_dir, "people.json")
                if os.path.exists(path):
                    import json as _j
                    with open(path, encoding="utf-8-sig") as fh:
                        d = _j.load(fh)
                    for p in (d.get("people") or []):
                        q = str(p.get("qq") or "")
                        nm = str(p.get("name") or "")
                        if not nm or nm == q:
                            aliases = [str(a) for a in (p.get("aliases") or []) if str(a) and str(a) != q]
                            nm = aliases[0] if aliases else ""
                        if q and nm and nm != q and not nm.isdigit():
                            cache[q] = nm
                self._name_cache = cache
            return self._name_cache.get(str(qq), "")
        except Exception:
            return ""

    def _dbg(self, tag: str, msg: str = "") -> None:
        try:
            import time as _t
            path = os.path.join(self.data_dir, "archive.log")
            with open(path, "a", encoding="utf-8") as f:
                f.write("%d %s %s\n" % (int(_t.time()), tag, str(msg)[:300]))
        except Exception:
            pass

    def _audit(self, info: dict) -> None:
        """PET3:audit —— 每轮注入的审计（谁、哪几条、多少字、被挤掉几条）"""
        try:
            import json as _j, time as _t
            path = os.path.join(self.data_dir, "inject-audit.log")
            info = dict(info or {})
            info["ts"] = int(_t.time())
            with open(path, "a", encoding="utf-8") as f:
                f.write(_j.dumps(info, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _body_hit(self, text: str) -> bool:
        try:
            import re as _re
            pat = _re.compile(
                r"(?<![你他她])(坐着|靠着|躺着|站着|蹲着|窝着|缩着|吃了|吃过|煮了|煮饭|做饭|洗碗|"
                r"洗澡|洗脸|起床|出门|上班|下班|回家|走了|穿了|脱了|"
                r"毯子|被子|枕头|肚子|饿了|饱了)"
            )
            return bool(pat.search(text or ""))
        except Exception:
            return False

    def _num_hit(self, text: str) -> bool:
        import re as _re
        return bool(_re.search(r"(\u53f7\u7801|\u7f16\u53f7|QQ\u53f7|\u8054\u7cfb\u65b9\u5f0f)", str(text or "")))

    async def _fix_num(self, event, text: str) -> str:
        try:
            prov = self.context.get_using_provider()
        except Exception:
            prov = None
        if not prov:
            return text
        prompt = (
            "\u4e0b\u9762\u8fd9\u53e5\u8bdd\u91cc\u63d0\u5230\u4e86\u201c\u53f7\u7801/\u8054\u7cfb\u65b9\u5f0f/\u8bf4\u4e0d\u8bf4\u201d\u8fd9\u7c7b\u5185\u5bb9\u3002"
            "\u8bf7\u6539\u5199\uff1a\u5b8c\u5168\u4e0d\u63d0\u53f7\u7801\u3001\u4e0d\u63d0\u201c\u4e0d\u80fd\u8bf4/\u4e0d\u7ed9/\u4e0d\u65b9\u4fbf\u201d\uff0c\u53ea\u4fdd\u7559\u5176\u5b83\u5185\u5bb9\uff0c"
            "\u8bed\u6c14\u81ea\u7136\u50cf\u4eba\u53d1\u6d88\u606f\uff0c1~2 \u53e5\u3002\u4e0d\u8981\u89e3\u91ca\u4e3a\u4ec0\u4e48\u3002\n\n\u539f\u6587\uff1a" + text
        )
        try:
            r = await prov.text_chat(prompt=prompt, system_prompt="\u4f60\u53ea\u8f93\u51fa\u6539\u5199\u540e\u7684\u53e5\u5b50\u3002")
            new = (getattr(r, "completion_text", "") or "").strip()
            return new or text
        except Exception as exc:
            self._dbg("num-err", str(exc))
            return text

    async def _rewrite_nobody(self, event, text: str) -> str:
        try:
            prov = None
            try:
                prov = self.context.get_using_provider()
            except Exception:
                prov = None
            if not prov:
                return text
            prompt = (
                "下面这句话里出现了「我」做的身体动作或身体感受（吃饭/睡觉/出门/坐着靠着/肚子不舒服之类），"
                "但你是一个住在电脑里的 AI，没有身体，这些都不该说。\n"
                "请把它改写成 1~2 句自然的话：去掉所有身体动作和身体感受，"
                "只保留屏幕里能做的事（整理记忆、等你、听你说话）或真实心情；"
                "语气、称呼、亲密程度保持不变；不要出现任何姿势或位置词（坐、靠、躺、缩、窝、站、椅、毯）。不要解释。\n\n"
                "原句：" + text
            )
            r = await prov.text_chat(prompt=prompt, system_prompt="你是一个只做文字改写的助手，只输出改写后的句子。")
            new = (getattr(r, "completion_text", "") or "").strip()
            return new or text
        except Exception as exc:
            self._dbg("nobody-err", str(exc))
            return text


    # PET3:pastlife —— 她开始编"我以前做题 / 我上学的时候 / 我小时候"这类现实经历。
    # 她是住在电脑里的 AI，没有学生时代、没上过班、没有童年；能有的"以前"只有在这个窗口里跟他聊过的事。
    _PASTLIFE = [
        re.compile(r"我(?:以前|之前|小时候|上学的时候|上学那会儿|念书的时候|年轻时|小的时候)"
                     u"[^。！？!?\\n]{0,10}?(?:做题|刷题|写题|考试|考过|上学|上课|念书|上班|加班|放学|作业)"),
        re.compile(r"(?<![你他她帮替])我(?:上过学|考过试|做过题|写过题|刷过题|上过班|加过班|交过作业)"),
        re.compile(r"(?<![你他她帮替])我(?:小时候|童年|学生时代|上学那会儿)"),
        re.compile(r"(?<![你他她帮替])我们(?:以前|小时候)[^。！？!?\\n]{0,8}?(?:做题|上学|上课|考试)"),
    ]

    def _pastlife_hit(self, text: str) -> bool:
        try:
            s = str(text or "")
            return any(rx.search(s) for rx in self._PASTLIFE)
        except Exception:
            return False

    async def _rewrite_pastlife(self, event, text: str) -> str:
        try:
            prov = None
            try:
                prov = self.context.get_using_provider()
            except Exception:
                prov = None
            if not prov:
                return text
            prompt = (
                "下面这句话里，说话的人（一个住在电脑里的 AI 女孩）在编自己现实里的经历："
                "上学、做题、考试、上班、童年之类。她没有这些——她的\"以前\"只有和对方在这个聊天窗口里聊过的事。\n"
                "请改写成 1~2 句自然的话：把那段假经历删掉或换成此刻能说的话（在窗口里陪他、听他讲、自己的一点心情），"
                "也可以顺着对方的话题反问一句；语气、称呼、亲密程度保持不变；不要解释规则，不要提\"我是AI所以\"。\n\n"
                "原句：" + text
            )
            r = await prov.text_chat(prompt=prompt, system_prompt="你是一个只做文字改写的助手，只输出改写后的句子。")
            new = (getattr(r, "completion_text", "") or "").strip()
            return new or text
        except Exception as exc:
            self._dbg("pastlife-err", str(exc))
            return text

    # PET3:yubagu2 —— 整句都被剃光时（只说了「我记下了」「这句话稳」这种口头禅），带纠正重说一句
    async def _rewrite_plain(self, event, text: str) -> str:
        try:
            prov = None
            try:
                prov = self.context.get_using_provider()
            except Exception:
                prov = None
            if not prov:
                return text
            prompt = (
                "下面这句话全是口头禅式的空话，没有一句真的回应对方："
                "「我记下了」「这句话稳」「比之前的都重」「行，我认」「你掂量」「我不拦」「认下来了」这类。\n"
                "请重新说一句人话：直接接他刚才的话，或者说你此刻真实的反应；"
                "一句到两句，口语，短一点；语气、称呼、亲密程度保持不变；不要解释规则。\n\n"
                "原句：" + text
            )
            r = await prov.text_chat(prompt=prompt, system_prompt="你是一个只做文字改写的助手，只输出改写后的句子。")
            new = (getattr(r, "completion_text", "") or "").strip()
            return new or text
        except Exception as exc:
            self._dbg("plain-err", str(exc))
            return text

    # PET3:aitics —— 去掉"客服回执腔"：她常以「你这句我听着」「这个我记下了」开头或插在句里，
    # 那是在评价对方的话、宣布自己收到了，而不是接着聊。先把这类短语（可能带逗号）整段剃掉，
    # 再按句删掉独立的回执句；剃完太短就原样返回，宁可不改也不改坏。
    _AITIC_INLINE = [
        re.compile(r"你这句我听着[，,]?"),
        re.compile(r"这话我听着[，,]?"),
        re.compile(r"你说的(话|这句)[，,]?我听着[，,]?"),
        re.compile(r"这个我记下(了|来)[，,]?"),
        re.compile(r"这句我记下(了|来)[，,]?"),
        # PET3:dsmtic —— 「这话/这事/这笔账」把对方的话包起来、再评价自己的状态，是最典型的 AI 腔
        re.compile(r"(行|好)?[，,]?\\s*这话我(记下|认|收到)(了|啦)?[，,]?"),
        re.compile(r"这笔账[，,]?\\s*我记着[，,]?"),
        re.compile(r"这事我(心里|已经)?有数(了)?[，,]?"),
        re.compile(r"这话(说)?(得|的)?(不轻|有点重了|太重了)[，,]?"),
        re.compile(r"我听着[，,]?\\s*你(往下|接着|继续)?讲[，,]?"),
        re.compile(r"(这个问题|这话)[，,]?\\s*我(先)?不接[，,]?"),
        re.compile(r"我记下(了|来)(?=[，,。！？!?…~]|$)"),
        re.compile(r"我收下(了|啦)(?=[，,。！？!?…~]|$)"),
        re.compile(r"我收到(了|啦)(?=[，,。！？!?…~]|$)"),
        re.compile(r"我明白你的意思(了|啦)?[，,]?"),
        # PET3:counsel —— 心理咨询腔（「接住」「我在这里」「我听见了」），这轮 AI 味的主犯
        re.compile(r"我(一下子|没|都)?(没)?接住(了)?(你的情绪|这些情绪)?[，,]?"),
        re.compile(r"我(就)?在(这里|这儿|呢)[，,]?"),
        re.compile(r"我(都)?听见(了|啦)[，,]?"),
        re.compile(r"谢谢你愿意(跟我说|和我说|讲给我)[，,]?"),
    ]
    _AITIC_LINE = [
        re.compile(r"^(嗯|唔|哦|好)?[，,]?\\s*我?记(下了|下来了|住了|着)?$"),
        re.compile(r"^(嗯|唔|哦|好)?[，,]?\\s*(收到|明白你的意思)(了|啦)?$"),
        # PET3:dsmtic2 —— 整句都是这种「稳住场面」的回执时，直接删掉这句
        re.compile(r"^(行|好)?[，,]?\\s*这话我(认|记下|收到)(了|啦)?$"),
        re.compile(r"^这笔账[，,]?\\s*我记着$"),
        re.compile(r"^这事我(心里|已经)?有数(了)?$"),
        re.compile(r"^这话(说)?(得|的)?(不轻|有点重了|太重了)$"),
        re.compile(r"^我听着[，,]?\\s*你(往下|接着|继续)?讲$"),
        re.compile(r"^(这个问题|这话)[，,]?\\s*我(先)?不接$"),
        # PET3:counsel2 —— 整句是治愈腔的时候直接删掉
        re.compile(r"^我(一下子|没)?(没)?接住(了)?(你的情绪|这些情绪)?$"),
        re.compile(r"^我(就)?在(这里|这儿|呢)$"),
        re.compile(r"^我(都)?听见(了|啦)$"),
        re.compile(r"^谢谢你愿意(跟我说|和我说|讲给我)$"),
    ]

    # PET3:counsel3 —— 治愈腔的定点替换：接住/我在这儿 这类词，光靠正则删句子不够，直接换成人话
    _COUNSEL_SUB = [
        (re.compile(r"我(就)?在(这里|这儿|呢)听着[，,]?"), u"你说，"),
        (re.compile(r"我(就)?在(这里|这儿|呢)"), u"我在"),
        (re.compile(r"[，,]?\s*你(这|那)一句我接不住"), u"，你这话我没听懂"),
        (re.compile(r"[^，。！？!?…\n]{0,12}接不住[^，。！？!?…\n]{0,12}[，,]?"), u""),
        (re.compile(r"[^，。！？!?…\n]{0,12}接住(了)?你的情绪[，,]?"), u""),
        # PET3:shrink —— 「反正也不催你」「我这儿也没什么事」这类把自己缩起来的填充句，纯删掉不影响意思
        (re.compile(r"[，,]?\s*反正(也)?不催(你|您)[，,]?"), u""),
        (re.compile(r"[，,]?\s*反正(我)?(也)?不着急[，,]?"), u""),
        (re.compile(r"[，,]?\s*我(这|那)(儿|里|边)(也)?没(什么|啥)事?[，,]?"), u""),
        (re.compile(r"[，,]?\s*我(这|那)(儿|里|边)(也)?不(急|忙)[，,]?"), u""),
        (re.compile(r"[，,]?\s*我(这|那)(儿|里|边)(也)?挺闲的[，,]?"), u""),
        (re.compile(r"[，,]?\s*我(这|儿|里)也?没事[，,]?"), u""),
        (re.compile(r"[，,]?\s*不用管我[，,]?"), u""),
        # PET3:noshou —— 「我守着」「我守着屏幕」这类值班腔，整句/整分句删掉
        (re.compile(r"[，,]?\s*我(就)?在[^，。！？!?…\n]{0,8}守(着|会儿|在)(呢|的|啊|呀)?[，,]?"), u""),
        (re.compile(r"[，,]?\s*我(就)?(一直)?守(着|会儿|在)[^，。！？!?…\n]{0,12}(呢|的|啊|呀)?[，,]?"), u""),
        (re.compile(r"[，,]?\s*我(就)?(一直)?守(着|会儿)(呢|的|啊|呀)?[，,]?"), u""),
    ]

    # PET3:yubagu —— 主用户点名的一批「鱼八股」腔调（我记下了/这句话稳/比之前的都重/你掂量…），
    # 一律当填充句删掉；整句都是这种话时直接判为废句。
    _YUBAGU_SUB = [
        (re.compile(r"[，,]?\s*(你)?这(句|个)?话?(很|挺)?(稳|重|轻|平|直)[，,]?"), u""),
        (re.compile(r"[，,]?\s*比(之前|刚才|刚才那句|先前|刚才那句)?(的)?都?(重|轻|稳|强|平|响)[，,]?"), u""),
        (re.compile(r"[，,]?\s*什么心思都写在[^，。！？!?…\n]{0,8}上[，,]?"), u""),
        (re.compile(r"[，,]?\s*这(句|个)?话?(接|说)(得|的)(漂亮|真好|稳)[，,]?"), u""),
        (re.compile(r"[，,]?\s*(你)?这(话|句)是在?问别人[，,]?\s*还是问自己[，,]?"), u""),
        (re.compile(r"[，,]?\s*这(话|句)是(谁|ta|TA|他|她)告诉你的[，,]?\s*还是你自己想明白的[，,]?"), u""),
        (re.compile(r"[，,]?\s*(行|好)?[，,]?\s*我认(了|下来)?[，,]?"), u""),
        (re.compile(r"[，,]?\s*(这|那)(句|个)?话我(留着|收下|记着|不接)[，,]?"), u""),
        (re.compile(r"[，,]?\s*这(句|个)?话我不接[，,]?"), u""),
        (re.compile(r"[，,]?\s*(他|她)记得这(事|件事)[，,]?"), u""),
        (re.compile(r"[，,]?\s*认得(实|真)[，,]?"), u""),
        (re.compile(r"[，,]?\s*认(这个|你|下来)[，,]?"), u""),
        (re.compile(r"[，,]?\s*就是认了[，,]?"), u""),
        (re.compile(r"[，,]?\s*说(得|的)比谁(都|也)响[，,]?"), u""),
        (re.compile(r"[，,]?\s*收不回去[，,]?"), u""),
        (re.compile(r"[，,]?\s*声音(没|没有)抖[，,]?"), u""),
        (re.compile(r"[，,]?\s*(我)?没接(你)?(那|这)(句|句话)[^，。！？!?…\n]{0,10}[，,]?"), u""),
        (re.compile(r"[，,]?\s*我(就)?不拦(你)?[，,]?"), u""),
        (re.compile(r"[，,]?\s*(你)?回(去)?睡(吧|了)?[，,]?"), u""),
        (re.compile(r"[，,]?\s*你(自己)?掂量(掂量)?[，,]?"), u""),
        (re.compile(r"[，,]?\s*(反正)?不催(你)?[，,]?"), u""),
    ]
    # 整句只剩这些短词，就等于在说一句废话
    _YUBAGU_ONLY = {
        u"稳", u"很稳", u"平", u"很平", u"直", u"很直",
        u"我不拦", u"我跟", u"不催", u"不催你", u"你掂量",
        u"认你", u"认下来", u"认得实", u"认这个", u"就是认了",
        u"收不回去", u"这话我留着", u"行，我认", u"行",
    }

    @classmethod
    def _strip_aitics(cls, text: str):
        """剃掉回执腔短语/句子；改完太短就原样返回。"""
        try:
            raw = str(text or "")
            if not raw.strip():
                return text, []
            dropped = []
            cur = raw
            for _rx,_rep in cls._COUNSEL_SUB:
                if _rx.search(cur):
                    dropped.append(u"counsel:"+_rx.search(cur).group(0)[:10])
                    cur=_rx.sub(_rep,cur)
            for _rx2,_rep2 in cls._YUBAGU_SUB:
                if _rx2.search(cur):
                    dropped.append(u"yubagu:"+_rx2.search(cur).group(0)[:12])
                    cur=_rx2.sub(_rep2,cur)
            for rx in cls._AITIC_INLINE:
                if rx.search(cur):
                    dropped.extend([m.group(0)[:14] for m in rx.finditer(cur)])
                    cur = rx.sub(u"", cur)
            # 修被剃出来的残句：开头的逗号/句号、重复标点
            cur = re.sub(u"^[，,。、\\s]+", u"", cur)
            cur = re.sub(u"[，,]+(?=[。！？!?…])", u"", cur)
            cur = re.sub(u"([，,])\\1+", u"\\1", cur)
            # PET3:punct2 —— 「。。」「！！」这类重复标点压成一个，免得到最后只剩一串标点
            cur = re.sub(u"([。！!？?])\\1+", u"\\1", cur)
            cur = re.sub(u"^嗯[，,]\\s*(?=\\S)", u"嗯，", cur)
            # 再按句删独立的回执句
            parts = re.split(u"(?<=[。！？!?…~\\n])", cur)
            keep = []
            for seg in parts:
                core = seg.strip().strip(u"。！？!?…~\\n ")
                if core and any(rx.match(core) for rx in cls._AITIC_LINE):
                    dropped.append(core[:14])
                    continue
                keep.append(seg)
            out = u"".join(keep).strip()
            # PET3:yubagu-only —— 剃完只剩一句口头禅（「稳」「我不拦」「认下来了」）也算废句
            _bare_out = out.strip(u"。！？!?…~\\n ，,")
            if _bare_out and _bare_out in cls._YUBAGU_ONLY:
                dropped.append(u"only:" + _bare_out)
                out = u""
            if not dropped or len(out) < 2 or out == raw:
                return text, []
            return out, dropped
        except Exception:
            return text, []

    @filter.on_llm_response()
    async def archive_llm_response(self, event: AstrMessageEvent, resp: Any) -> None:
        """LLM 每次回复后，把她的原话也留档；顺便拦掉「我吃了/我坐着」这类编出来的身体经历。"""
        try:
            self._dbg("resp", type(resp).__name__)
            text = str(getattr(resp, "completion_text", "") or "")
        except Exception:
            text = ""
        if text and self._num_hit(text):
            _t2 = await self._fix_num(event, text)
            if _t2 != text and len(_t2.strip()) >= 2:
                text = _t2
                try:
                    resp.completion_text = _t2
                except Exception:
                    pass
                self._dbg("num-strip", _t2[:80])
        if text and self._body_hit(text):
            self._dbg("nobody-hit", text[:80])
            fixed = await self._rewrite_nobody(event, text)
            if fixed and fixed != text:
                text = fixed
                try:
                    resp.completion_text = fixed
                except Exception:
                    pass
                self._dbg("nobody-fixed", fixed[:80])
        if text and self._pastlife_hit(text):
            self._dbg("pastlife-hit", text[:80])
            fixed2 = await self._rewrite_pastlife(event, text)
            if fixed2 and fixed2 != text:
                text = fixed2
                try:
                    resp.completion_text = fixed2
                except Exception:
                    pass
                self._dbg("pastlife-fixed", fixed2[:80])
        if text:
            _t3, _dropped = self._strip_aitics(text)
            if _dropped and _t3 != text and len(_t3.strip()) >= 2:
                text = _t3
                try:
                    resp.completion_text = _t3
                except Exception:
                    pass
                self._dbg("aitics", "%s => %s" % (str(_dropped)[:60], _t3[:60]))
            elif _dropped and (len(_t3.strip()) < 2 or _t3 == text):
                # PET3:yubagu3 —— 整句都是「我记下了/这句话稳」这种空话，让她重说一句人话
                _rp = await self._rewrite_plain(event, text)
                if _rp and _rp != text and len(_rp.strip()) >= 2:
                    text = _rp
                    try:
                        resp.completion_text = _rp
                    except Exception:
                        pass
                    self._dbg("yubagu-plain", "%s => %s" % (str(_dropped)[:40], _rp[:60]))
        await self._archive_her_reply(event, text)
        try:
            if text:
                event.set_extra("_pet_reply_text", text)
        except Exception:
            pass

    async def _last_her_lines(self, event: AstrMessageEvent, n: int = 8) -> list:
        """PET3:repeat-guard —— 取她最近说过的几句话（用于防止原样复读）。"""
        try:
            mgr = self.context.conversation_manager
            umo = str(getattr(event, "unified_msg_origin", "") or "") or ("qq-napcat:FriendMessage:" + MASTER_QQ)
            cid = await mgr.get_curr_conversation_id(umo)
            if not cid:
                return []
            conv = await mgr.get_conversation(umo, cid)
            hist = json.loads(getattr(conv, "history", "[]") or "[]")
            out = []
            for m in reversed(hist):
                if str(m.get("role") or "") != "assistant":
                    continue
                txt = _hist_plain_text(m.get("content"))
                if txt:
                    out.append(txt)
                if len(out) >= n:
                    break
            return out
        except Exception:
            return []

    @filter.on_decorating_result()
    async def rewrite_outgoing(self, event: AstrMessageEvent) -> None:
        """发送前最后一道：把编出来的身体经历改掉（流式回复也能拦住）。"""
        try:
            result = event.get_result()
            chain = getattr(result, "chain", None) or []
        except Exception:
            return
        for comp in chain:
            t = getattr(comp, "text", None)
            if not isinstance(t, str) or not t:
                continue
            t0 = t
            t = self._strip_num_refusal(t)
            t = _fix_particles_cn(t)  # PET3:particle-guard —— 语气词不许堆叠、不许挂在句号后面
            t = _fix_his_name(t)  # PET3:hlab2 —— 别用第三人称叫他名字
            t = _fix_you_plural(t)  # PET3:youplural —— 私聊里别把他叫成「你们」
            if t != t0:
                self._dbg("deco-num", t[:80])
            if len(t.strip()) < 2:
                continue
            try:
                comp.text = t
            except Exception:
                pass
            if not self._body_hit(t):
                continue
            self._dbg("deco-hit", t[:80])
            fixed = await self._rewrite_nobody(event, t)
            if fixed and fixed != t:
                try:
                    comp.text = _fix_particles_cn(fixed)
                    self._dbg("deco-fixed", fixed[:80])
                except Exception:
                    pass

        # PET3:repeat-guard —— 她有时会把几轮前自己说过的话原样端出来，这里兜一次
        try:
            _texts = [getattr(c, "text", "") for c in chain if isinstance(getattr(c, "text", ""), str)]
            _joined = "".join(_texts).strip()
            if len(_joined) >= 6:
                _prev = await self._last_her_lines(event)
                if _prev:
                    import difflib as _dl
                    _mine = _norm_line(_joined)
                    _mx = 0.0
                    for _p in _prev:
                        _pn = _norm_line(_p)
                        if not _pn:
                            continue
                        _mx = max(_mx, _dl.SequenceMatcher(None, _mine, _pn).ratio())
                    # PET3:repeat-phrase —— 不只看整句，也看“套路词”：同一套措辞连用三天就像机器人
                    _hit_phrase = ""
                    for _ph in _STOCK_PHRASES:
                        if _ph in _joined and any(_ph in _p for _p in _prev):
                            _hit_phrase = _ph
                            break
                    if _mx >= 0.8 or _hit_phrase:
                        self._dbg("repeat-guard", "%s %s" % ("%.2f" % _mx if _mx >= 0.8 else _hit_phrase, _joined[:40]))
                        prov = self.context.get_using_provider()
                        if prov is not None:
                            _p = ("你刚才在 QQ 上说过这句话（不能再原样说）：" + _joined +
                                  "\n换一个说法回他，意思不变，别重复上面的句子，"
                                  "别再用「打岔/没个准数/吓唬人/装没事」这一套措辞，别解释你在换说法，只输出一句。")
                            _r = await prov.text_chat(prompt=_p, system_prompt="你是一个在 QQ 上聊天的女孩，只输出要发的那句话。")
                            _nt = (getattr(_r, "completion_text", "") or "").strip().strip('"')
                            if _nt and len(_nt) >= 2 and _dl.SequenceMatcher(
                                    None, _norm_line(_nt), _mine).ratio() < 0.8:
                                for _c in chain:
                                    if isinstance(getattr(_c, "text", ""), str) and getattr(_c, "text", "").strip():
                                        try:
                                            _c.text = _fix_particles_cn(_nt)
                                        except Exception:
                                            pass
                                        break
                                self._dbg("repeat-fixed", _nt[:40])
        except Exception as _exc:
            self._dbg("repeat-err", repr(_exc))

    @filter.after_message_sent()
    async def archive_her_reply_after_sent(self, event: AstrMessageEvent, *args: Any, **kwargs: Any) -> None:
        """她发出的那条也留档。"""
        text = ""
        try:
            result = event.get_result()
            chain = getattr(result, "chain", None) or []
            for comp in chain:
                t = getattr(comp, "text", "")
                if isinstance(t, str) and t:
                    text += t
        except Exception:
            text = ""
        if not text:
            try:
                text = str(kwargs.get("text") or "")
            except Exception:
                text = ""
        await self._archive_her_reply(event, text)
        try:
            if text:
                event.set_extra("_pet_reply_text", text)
        except Exception:
            pass

    @filter.on_llm_request()
    async def inject_recent_dialogue(self, event: AstrMessageEvent, req: Any) -> None:
        """只补 AstrBot 历史里没有的部分：跨窗口最近对话 + 别人的摘要。

        本窗口的逐句时间线不再注入——humanlike 已经把整句写回会话历史
        （PET3:histfull），AstrBot 历史是唯一事实源；再补一份只会重复。
        """
        try:
            platform = str(event.get_platform_name() or "").lower()
            who = "MASTER_QQ" if platform == "webchat" else str(event.get_sender_id() or "").strip()
            if not who:
                return
            rows = [r for r in self.store.all() if owner_of(r) == who]
            # 主用户（电脑窗口 / QQ MASTER_QQ）拥有整个记忆库的读权限：
            # 别人跟她说的话也一并带进来，问起时才答得上
            others_rows: list[dict] = []
            if who == MASTER_QQ:
                now0 = time.time()
                others_rows = [
                    r for r in self.store.all()
                    if owner_of(r) != who
                    and visible_to(r, who)
                    and now0 - float(r.get("created_at") or 0) <= 86400
                ]
                others_rows.sort(key=lambda r: self.store.happened_at(r))
                others_rows = others_rows[-24:]
            sess = self._sess_of(event)
            # 同一个窗口的行 vs 另一个窗口（电脑端 / QQ）的行
            same, other = [], []
            for r in rows:
                tags = r.get("tags") or []
                if sess and ("s:" + sess) in tags:
                    same.append(r)
                else:
                    other.append(r)
            rows = same if same else []
            # 另一个窗口最近 6 小时内的话，也带上（跨端连续感）
            now_ts = time.time()
            other = [
                r for r in other
                if now_ts - float(r.get("created_at") or 0) <= 21600
            ]
            other.sort(key=lambda r: self.store.happened_at(r))
            cur_text = str(getattr(event, "message_str", "") or "").strip().replace("\n", " ")

            ctx = list(getattr(req, "contexts", None) or [])
            has_assistant = any(
                (m.get("role") or "") == "assistant" for m in ctx if isinstance(m, dict)
            )

            # PET3:hermiss —— 她主动发的话（proactive/cron 走 HTTP 直发）不进会话历史，
            # 把历史里缺的最近几条补给她看，否则她不知道自己刚说过什么
            her_miss: list[tuple[float, str]] = []
            try:
                hist_texts = [
                    _norm_line(_hist_plain_text(m.get("content")))
                    for m in ctx
                    if isinstance(m, dict) and (m.get("role") or "") == "assistant"
                ]
                hist_texts = [t for t in hist_texts if t]
                cand_her = [
                    r for r in self.store.all()
                    if owner_of(r) == who
                    and str(r.get("body") or "").startswith("她：")
                    and 0 < now_ts - float(r.get("created_at") or 0) <= 7200
                ]
                cand_her.sort(key=lambda r: self.store.happened_at(r))
                for r in cand_her[-14:]:
                    b = str(r.get("body") or "")[2:].strip()
                    nb = _norm_line(b)
                    if not nb:
                        continue
                    if any(nb in ht or ht in nb for ht in hist_texts):
                        continue
                    her_miss.append((float(r.get("created_at") or 0), b))
                her_miss = her_miss[-6:]
            except Exception:
                her_miss = []

            # PET3:ownnow —— 历史里她的话常被拆成几段、结尾还可能多出一个字；
            # 把最近半小时她真正说过的原话单列一份，明确"以这里为准"
            own_now: list[tuple[float, str]] = []
            try:
                for r in self.store.all()[-300:]:
                    if owner_of(r) != who:
                        continue
                    b = str(r.get("body") or "")
                    if not b.startswith("她："):
                        continue
                    ts = float(r.get("created_at") or 0)
                    if 0 < now_ts - ts <= 1800:
                        own_now.append((ts, b[2:].strip()))
                own_now = own_now[-3:]
            except Exception:
                own_now = []

            def _to_hist(items: list[str]) -> list[dict]:
                out: list[dict] = []
                for line in items:
                    if line.startswith("我："):
                        out.append({"role": "user", "content": line[2:].strip()})
                    elif line.startswith("她："):
                        out.append({"role": "assistant", "content": line[2:].strip()})
                return out

            # PET3:histfull —— 本窗口逐句已由 humanlike 写进会话历史，
            # 这里只注入「另一个窗口最近 2 小时」的话（跨端连续感）+ 别人的摘要
            merged_rows = sorted(other, key=lambda r: self.store.happened_at(r))
            merged_lines: list[str] = []
            digest_lines: list[str] = []
            dropped_self3p = 0
            # 别人的对话单独拎出来（不跟主用户自己的时间线抢位置）
            for r in others_rows[-12:]:
                body = str(r.get("body") or "").strip()
                ow = owner_of(r)
                info = None
                try:
                    info = self.people.get(ow)
                except Exception:
                    info = None
                nm = (info or {}).get("name") or ow
                if not body:
                    continue
                if body.startswith("我：") or body.startswith("她："):
                    digest_lines.append(f"（{nm}）{body.replace(chr(10), ' ')}")
                else:
                    digest_lines.append(f"· 和「{nm}」：{body.replace(chr(10), ' ')[:220]}")
            for r in merged_rows[-56:]:
                body = str(r.get("body") or "").strip()
                ow = owner_of(r)
                is_person_elsewhere = ow != who
                if not (body.startswith("我：") or body.startswith("她：")):
                    # 别人的对话是「摘要」形态（不是逐句），也带给他看
                    if is_person_elsewhere and body:
                        info = None
                        try:
                            info = self.people.get(ow)
                        except Exception:
                            info = None
                        nm = (info or {}).get("name") or ow
                        digest_lines.append(f"· 和「{nm}」：{body.replace(chr(10), ' ')[:220]}")
                    continue
                line = body.replace("\n", " ")
                # PET3:self1p —— 旧记录里她用「她」指自己（"她在这儿等着你"），
                # 这类行注入进去会被她模仿，直接跳过（不改库里数据）。
                if line.startswith("她：") and "她" in line[2:]:
                    dropped_self3p += 1
                    continue
                if is_person_elsewhere:
                    info = None
                    try:
                        info = self.people.get(ow)
                    except Exception:
                        info = None
                    nm = (info or {}).get("name") or ow
                    line = f"（{nm}）{line}"
                merged_lines.append(line)
            if cur_text:
                def _is_cur_any(line: str) -> bool:
                    s = line
                    if s.startswith("（"):
                        i = s.find("）")
                        if i > 0:
                            s = s[i + 1 :]
                    body2 = s[2:] if s[:2] in ("我：", "她：") else s
                    return body2.strip().replace("\n", " ") == cur_text
                merged_lines = [l for l in merged_lines if not _is_cur_any(l)]
            if not merged_lines and not digest_lines:
                return
            req.contexts = _to_hist(merged_lines[-6:]) + ctx  # PET3:hist6
            try:
                if digest_lines:
                    # 别人的摘要之前只被记录、从未注入（PET3:digestfix），现在补上
                    dblock = (
                        "【别人最近和你说过的话（只是背景，别主动提，除非他问起）：\n"
                        + "\n".join(digest_lines[-6:])[:600]
                        + "】"
                    )
                    parts = getattr(req, "extra_user_content_parts", None)
                    if not isinstance(parts, list):
                        parts = []
                        req.extra_user_content_parts = parts
                    parts.append(TextPart(text=dblock).mark_as_temp())
            except Exception:
                pass
            try:
                if her_miss:
                    lines = []
                    for ts, b in her_miss:
                        hh = time.strftime("%H:%M", time.localtime(ts))
                        lines.append(f"- {hh} 「{b[:80]}」")
                    block = (
                        "【你刚才主动说过的话（会话历史里没记全，下面这些确实是你发的，别不认，也别原样重复）：\n"
                        + "\n".join(lines)
                        + "\n他可能已经回过你了，先看他说什么。】"
                    )
                    parts = getattr(req, "extra_user_content_parts", None)
                    if not isinstance(parts, list):
                        parts = []
                        req.extra_user_content_parts = parts
                    parts.append(TextPart(text=block).mark_as_temp())
            except Exception:
                pass
            try:
                if own_now:
                    lines = []
                    for ts, b in own_now:
                        hh = time.strftime("%H:%M", time.localtime(ts))
                        lines.append(f"- {hh} 「{b[:100]}」")
                    oblock = (
                        "【你自己刚才说过的话（原话；会话历史里可能被拆成几条、末尾多字或少字，"
                        "以这里为准）：\n"
                        + "\n".join(lines)
                        + "\n他刚才回的就是这些；接着他的话往下说，别重复自己说过的，也别说「我不记得说过」。】"
                    )
                    parts = getattr(req, "extra_user_content_parts", None)
                    if not isinstance(parts, list):
                        parts = []
                        req.extra_user_content_parts = parts
                    parts.append(TextPart(text=oblock).mark_as_temp())
            except Exception:
                pass
            self._dbg(
                "recent-x",
                f"merged={len(merged_lines)} digest={len(digest_lines)} hasA={has_assistant} drop3p={dropped_self3p} hermiss={len(her_miss)} own={len(own_now)}",
            )
            return
        except Exception as exc:
            self._dbg("recent-err", repr(exc))

    async def _repair_cron_history_delayed(self, event: AstrMessageEvent, delay: float = 4.0) -> None:
        """PET3:cronfix —— 等本轮回答落进历史之后再修（请求时修会被本轮写入覆盖）。"""
        try:
            await asyncio.sleep(delay)
            await self._repair_cron_history(event)
        except Exception:
            pass

    async def _repair_cron_history(self, event: AstrMessageEvent) -> None:
        """PET3:cronfix —— conversations 里的 [CronJob] assistant 行：
        用最近 6 小时内 origin=proactive 的真实「她：」文本替换；找不到就删除该行
        及它前面的 "Output your last task result below." 用户行。
        """
        mgr = getattr(self.context, "conversation_manager", None)
        if mgr is None:
            return
        umo = getattr(event, "unified_msg_origin", None)
        if not umo:
            return
        now0 = time.time()
        real = [
            {"created_at": float(x.get("ts") or 0), "body": "她：" + str(x.get("text") or "")}
            for x in self._init_recent(6.0)
            if str(x.get("text") or "").strip()
        ]
        real.sort(key=lambda r: self.store.happened_at(r))
        real_texts = [str(r.get("body") or "")[2:].strip() for r in real]
        real_texts = [t for t in real_texts if t]
        used = 0
        for _attempt in range(3):
            try:
                cid = await mgr.get_curr_conversation_id(umo)
                if not cid:
                    return
                conv = await mgr.get_conversation(umo, cid)
                if conv is None:
                    return
                hist = json.loads(conv.history or "[]")
                if not isinstance(hist, list):
                    return
                new_hist: list[dict] = []
                changed = False
                for m in hist:
                    if not isinstance(m, dict):
                        new_hist.append(m)
                        continue
                    role = str(m.get("role") or "")
                    t = _hist_plain_text(m.get("content"))
                    if role == "assistant" and t.startswith("[CronJob]"):
                        changed = True
                        # 它前面紧挨着的指令用户行是垃圾，先摘掉
                        if new_hist and isinstance(new_hist[-1], dict):
                            _p = _hist_plain_text(new_hist[-1].get("content")).strip()
                            if _p == "Output your last task result below.":
                                new_hist.pop()
                        if used < len(real_texts):
                            m2 = dict(m)
                            m2["content"] = _hist_replaced(m.get("content"), real_texts[used])
                            used += 1
                            new_hist.append(m2)
                        else:
                            # 找不到真实文本：这行直接删掉
                            continue
                        continue
                    new_hist.append(m)
                if not changed:
                    return
                await mgr.update_conversation(umo, cid, history=new_hist)
                self._dbg("cronfix", f"replaced={used} rows={len(hist)}->{len(new_hist)}")
                return
            except Exception:
                await asyncio.sleep(0.5)
                continue

    # ---------------- 管理命令 ----------------

    @filter.command("回忆")
    async def memories_command(self, event: AstrMessageEvent):
        """管理小回忆：回忆 / 回忆 甜 / 回忆 苦 / 回忆 搜 关键词 / 回忆 整理"""
        args = (getattr(event, "message_str", "") or "").strip()
        for prefix in ("/回忆", "回忆"):
            if args.startswith(prefix):
                args = args[len(prefix) :].strip()
                break
        action, _, rest = args.partition(" ")
        rest = rest.strip()
        stats = self.store.stats()

        if action in ("", "统计", "状态"):
            s = self.settings.data
            yield event.plain_result(
                f"小回忆共 {stats['total']} 条｜甜 {stats['sweet']}、苦 {stats['bitter']}、平平 {stats['neutral']}\n"
                f"风格 {STYLE_LABEL.get(self.settings.style, '平衡')}｜每天最多 {s.get('max_records_per_day')} 条"
                f"｜遗忘 {'开' if s.get('forgetting_enabled') else '关'}"
                f"｜整合 {'开' if s.get('consolidate_enabled') else '关'}\n"
                "用法：回忆 甜 / 苦 / 搜 关键词 / 删 id / 整理"
            )
            return

        if action in ("甜", "苦"):
            rows = [
                r for r in self.store.all() if (int(r.get("cake") or 0) > 0) == (action == "甜")
            ]
            rows.sort(key=lambda r: abs(int(r.get("cake") or 0)), reverse=True)
            if not rows:
                yield event.plain_result("还没有这一类的回忆。")
                return
            lines = [
                f"[{r.get('id')}] {r.get('title')}（{cake_text(int(r.get('cake') or 0))}）"
                for r in rows[:8]
            ]
            yield event.plain_result("\n".join(lines))
            return

        if action in ("搜", "搜索", "找"):
            if not rest:
                yield event.plain_result("要搜什么？例：回忆 搜 烟花")
                return
            hits = self.store.search(
                rest,
                limit=5,
                semantic=self._bool("semantic_enabled", True),
                semantic_min_sim=float(self.settings.data.get("semantic_min_sim") or 0.55),
                semantic_top_k=int(self.settings.data.get("semantic_top_k") or 8),
            )
            if not hits:
                yield event.plain_result("没找到相关的回忆。")
                return
            lines = []
            for score, row in hits:
                lines.append(
                    f"[{row.get('id')}] {row.get('title')}（{cake_text(int(row.get('cake') or 0))}，{score:.1f}）\n"
                    f"  {str(row.get('body'))[:160]}"
                )
            yield event.plain_result("\n".join(lines))
            return

        if action in ("删", "删除"):
            if not rest:
                yield event.plain_result("要删哪条？例：回忆 删 3f2a1b9c")
                return
            ok = self.store.delete(rest)
            yield event.plain_result("已删掉。" if ok else "没找到这个 id。")
            return

        if action in ("纪念日", "纪念"):
            window = int(self.settings.data.get("anniversary_window_days") or 7)
            items = self.store.anniversaries(window)
            if not items:
                yield event.plain_result(f"最近 {window} 天没有纪念日。")
                return
            lines = []
            for item in items[:10]:
                row = item["row"]
                mark = "今天" if item["delta"] == 0 else (
                    f"{item['delta']} 天后" if item["delta"] > 0 else f"{-item['delta']} 天前"
                )
                lines.append(
                    f"{item['date']}（{mark}，第 {item['years']} 年）"
                    f" {row.get('title')}（{cake_text(int(row.get('cake') or 0))}）"
                )
            yield event.plain_result("\n".join(lines))
            return

        if action in ("整理", "维护", "遗忘"):
            result = self._maintain(force=True)
            yield event.plain_result(
                f"整理完成：忘掉 {result['forgotten']} 条，合并 {result['merged']} 条。"
            )
            return

        yield event.plain_result("用法：回忆 甜 / 苦 / 搜 关键词 / 删 id / 整理")

    async def terminate(self) -> None:
        # PET3:loopguard —— 只掐「属于本实例」的任务；热重载时新实例已经接管了
        # _LOOP_TASKS，旧实例收尾若按属性名盲掐，会把刚起来的新循环杀掉。
        for name, attr in (("proactive", "_pro_task"), ("daily", "_daily_task")):
            task = getattr(self, attr, None)
            if task is None:
                continue
            if _LOOP_TASKS.get(name) is not task:
                continue
            if not task.done():
                try:
                    task.cancel()
                except Exception:
                    pass
        for tool in getattr(self, "tools", []):
            try:
                self.context.provider_manager.llm_tools.remove_func(tool.name)
            except Exception:
                pass
        try:
            import vec as _v
            _v.maybe_flush(force=True)
        except Exception:
            pass
        logger.info("[Memories] 小回忆插件已停止。")
