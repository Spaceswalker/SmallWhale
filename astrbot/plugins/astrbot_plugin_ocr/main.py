"""本地 OCR：把她收到的图片里的文字读出来，塞进这一轮上下文。

没有视觉模型，只有 Windows 自带的 OCR（Windows.Media.Ocr）。所以：
- 能读截图、聊天记录、文档、表格里的字；
- 读不出画面内容（表情、风景、照片里发生了什么）；
- 认不出字时会明确告诉她"没认出字"，避免她瞎编。
"""

from __future__ import annotations

import asyncio
import json
import os
import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.message import TextPart
from astrbot.core.message.components import Image

_PS1 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ocr.ps1")
_MAX_IMAGES = 3
_MAX_CHARS = 1200

_CJK_GAP = re.compile(r"(?<=[\u4e00-\u9fff\u3000-\u303f])\s+(?=[\u4e00-\u9fff\u3000-\u303f])")


def _tidy(text: str) -> str:
    """Windows OCR 会在汉字之间塞空格，这里去掉；其余换行保留。"""
    lines = []
    for line in str(text or "").replace("\r\n", "\n").split("\n"):
        line = _CJK_GAP.sub("", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


@register("astrbot_plugin_ocr", "MASTER_NAME", "本地 OCR：把图片里的文字读进对话（不需要视觉模型）", "1.0.0")
class OcrPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}

    async def initialize(self):
        await super().initialize()
        logger.info("[ocr] ready")

    @filter.on_llm_request(priority=120000)
    async def inject_ocr(self, event: AstrMessageEvent, req):
        try:
            if not bool(self.config.get("enabled", True)):
                return
            comps = list(getattr(event.message_obj, "message", []) or [])
            imgs = [c for c in comps if isinstance(c, Image)]
            if not imgs:
                return
            blocks = []
            for comp in imgs[:_MAX_IMAGES]:
                try:
                    path = await comp.convert_to_file_path()
                    text = _tidy(await self._ocr(path)).strip()
                    vis = await self._vision(path)
                    if vis:
                        text = text + ("\n[画面] " + vis if text else "[画面] " + vis)
                except Exception as exc:
                    logger.warning(f"[ocr] image failed: {exc!r}")
                    continue
                if text:
                    blocks.append(text[:_MAX_CHARS])
                else:
                    blocks.append("（没有识别到文字）")
            if not blocks:
                return
            body = "\n\n---\n\n".join(blocks)
            notice = (
                "[图片文字识别] 下面是本地 OCR 从这张图里读到的字（不是你看图看出来的；"
                "OCR 只能读文字，读不出画面、表情、颜色、谁在做什么）。\n"
                "如果下面写着「没有识别到文字」，就说明这张图里没有可读的字，"
                "别猜图里画了什么，直接跟他说你没看清就行。\n\n" + body
            )
            req.extra_user_content_parts.append(TextPart(text=notice).mark_as_temp())
        except Exception as exc:
            logger.warning(f"[ocr] hook error: {exc!r}")

    async def _ocr(self, path: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            _PS1,
            path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=15)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            raise RuntimeError("OCR 超时（15s）")
        if proc.returncode != 0:
            raise RuntimeError((err or b"").decode("utf-8", "ignore")[:200] or f"exit {proc.returncode}")
        return out.decode("utf-8", "ignore")

    async def _vision(self, path: str) -> str | None:
        """PET3:vision —— 配了视觉模型就用它描述图片；没配/失败返回 None（回落 OCR）。"""
        key = str(self.config.get("vision_api_key") or "").strip()
        model = str(self.config.get("vision_model") or "").strip()
        base = str(self.config.get("vision_base_url") or "").strip()
        if not (bool(self.config.get("vision_enabled", False)) and key and model and base):
            return None
        try:
            import base64 as _b64
            import urllib.request as _ur

            with open(path, "rb") as f:
                data = _b64.b64encode(f.read()).decode("ascii")
            mime = "image/png"
            payload = {
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}},
                            {
                                "type": "text",
                                "text": "用一两句话描述这张图：图里有什么、谁在做什么、什么气氛。只描述事实，不要编。",
                            },
                        ],
                    }
                ],
                "max_tokens": 300,
            }
            req = _ur.Request(
                base.rstrip("/") + "/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
            )
            with _ur.urlopen(req, timeout=60) as resp:
                body = resp.read().decode("utf-8", "ignore")
            obj = json.loads(body)
            return str(obj["choices"][0]["message"]["content"]).strip() or None
        except Exception as exc:
            logger.warning(f"[ocr] vision failed, fallback to OCR: {exc!r}")
            return None
