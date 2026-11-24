# 排错

## 小鲸不回话

1. AstrBot WebUI（:6185）打得开吗？打不开 → 看 `AstrBot/data/logs/`，多半天亮（provider 报错）
2. WebUI 里「平台适配器」在线吗？离线 → NapCat 的反向 WS 地址要带 `/ws`（`ws://127.0.0.1:6199/ws`）
3. 插件日志 `plugin_data/astrbot_plugin_memories/archive.log` 最后 20 行有没有异常
4. 她回了但很怪 → 看 `inject-audit.log`，能查到这一轮注入了哪些记忆 id

## 她编造没发生的事

三步定位：

```powershell
# 1. 库里到底有没有这件事
Select-String -Path memories.jsonl -Pattern '关键词'
# 2. 这轮注入了什么
Get-Content inject-audit.log -Tail 5
# 3. 人设里有没有教她编的例子
Select-String -Path <你的人设文件> -Pattern '比如'
```

常见原因：人设里举例的具体名词被当成事实、`digest` 摘要被当当天素材、上下文窗口滚动把已定的事挤掉。
前两条本仓库的插件已处理（摘要不参与当天素材；人设例句不许带具体名词）；第三条靠「已定的事别重问」那条规矩。

## 她说口头禅（「我记下了」「这句话稳」）

本仓库在 `main.py` 有 `_strip_aitics` 三层过滤：定点替换 → 正则剃分句 → 剃完只剩口头禅就判废句重说。
要加新词：往 `_YUBAGU_SUB` / `_YUBAGU_ONLY`（或 `_COUNSEL_SUB`）里加一条，然后重启 AstrBot。

## 主动消息太勤 / 太像凑数

- 太勤：调 `settings.json` 里 `wake_up_per_day`，或看 `proactive_state.json` 的台账
- 太像凑数：提示词要求「一句话、最多三十个字、不许找由头」，代码里还有 `_PRO_SELF_RE` 兜底；如果她还在说「翻着我们的对话」，往那条正则里加词

## 重启之后改动没生效

AstrBot 的插件代码与人格都是启动时加载的：

```powershell
netstat -ano | Select-String ':6185\s+0.0.0.0:0\s+LISTENING'
Stop-Process -Id <pid> -Force
# keeper 会在 20~60 秒内拉起，日志里能看到 proactive-loop started pid=<新>
```

## 桌面 Live2D 相关

本仓库不含 Live2D 桌宠代码，也没有那部分排错。想做的话，模型可以用 [这个开源模型](https://www.bilibili.com/video/BV1KmxY66EZ3/)。
