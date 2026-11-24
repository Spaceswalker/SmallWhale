# AstrBot 侧：插件、人设、配置

## 插件放哪

`AstrBot/data/plugins/<插件目录>/`，把本仓库 `astrbot/plugins/` 下四个目录整体拷过去。
每个插件目录里 `main.py` 是入口，`metadata.yaml`（如有）是元信息。

| 插件 | 作用 | 来源 |
| --- | --- | --- |
| `astrbot_plugin_memories` | 记忆、日记、主动消息、语言过滤 | 上游 MIT 的大幅修改版 |
| `astrbot_plugin_forward` | 消息转发 | 本项目 |
| `astrbot_plugin_ocr` | 图片文字进对话 | 本项目 |
| `astrbot_plugin_video_bridge` | 视频号桥接 | 本项目 |

## 三个 QQ 号占位符

插件里用 `MASTER_QQ` / `BOT_QQ` / `MASTER_NAME` 表示「你」「她」「你的称呼」。
每个插件文件头部已经有一段「部署占位符」，按需要直接改，或者设环境变量：

```powershell
$env:XJ_MASTER_QQ = "你的QQ"
$env:XJ_BOT_QQ    = "小鲸的QQ"
$env:XJ_MASTER_NAME = "主用户"   # 或你的昵称
```

> 这些常量用了一个字符串子类，能同时和 `str` / `int` 比较，避免上游代码里类型不一致导致的判断失效。

## 人设（自己写）

本仓库**不附带人设**（只带一份 `astrbot/persona/banned-phrases.md`：鱼八股禁句清单，纯语言约束）。原因很简单：人设是「你想要她成为谁」，这件事没有通用答案。

想抄我们的说话风格，`docs/memory-and-persona.md` 末尾有「语言规范」一节（说话方式 + 禁用句式），
直接贴到 AstrBot 的「人格」（`personas` 表的 `system_prompt`）里，再在它前面补一段你想要的身份与关系即可。

## 关键配置项

`astrbot/config.example.json` 给了一份样例，重点是：

- `provider[0].custom_extra_body = {"temperature": 0.95}` —— 温度高一点，句子才不像公文
- 记忆插件自己的 `settings.json` 在 `AstrBot/data/plugin_data/astrbot_plugin_memories/`，
  首次运行自动生成；常用项见 `docs/memory-and-persona.md`

## 改完怎么生效

```powershell
# 只改插件 python：重启 AstrBot
netstat -ano | Select-String ':6185\s+0.0.0.0:0\s+LISTENING'
Stop-Process -Id <上面的PID> -Force
# keeper 会在 20~80 秒内拉起；或者手动跑 scripts\start-astrbot.vbs
```

日志在 `AstrBot/data/logs/`，插件自己的 `archive.log` / `inject-audit.log` 在 plugin_data 下。
