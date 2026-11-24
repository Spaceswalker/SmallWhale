# astrbot

| 目录/文件 | 说明 |
| --- | --- |
| `plugins/` | 四个插件：记忆（改动最大）、转发、OCR、视频桥 |
| `persona/banned-phrases.md` | 鱼八股禁句清单（可拼进自己的人设） |
| `config.example.json` | AstrBot 关键配置样例 |

## 改成你自己的

1. 每个插件文件头部的「部署占位符」：`MASTER_QQ` / `BOT_QQ` / `MASTER_NAME`
2. provider 的模型名与 API Key（放 AstrBot 的配置里，不要提交）

## 记忆插件的目录

运行后会生成：

```
AstrBot/data/plugin_data/astrbot_plugin_memories/
├─ memories.jsonl     全部记忆（一行一条 JSON）
├─ people.json        人物表
├─ settings.json      记忆阈值
├─ state.json         维护时间、游标
├─ proactive_state.json  主动消息台账
├─ archive.log        插件日志
└─ inject-audit.log   每次注入审计（查「她为什么这么说」用这个）
```
