# 第三方组件、出处与署名

本仓库**不整包转发**下面这些上游项目，只提供我们自己的代码与说明。使用前请按各自协议自行获取。
下面把「哪些是我们的、哪些是别人的、别人是谁」逐项写清。

## 一、运行时依赖（本仓库不含其源码）

| 组件 | 用途 | 上游 | 协议 |
| --- | --- | --- | --- |
| AstrBot | QQ 侧对话框架 / 插件宿主 | <https://github.com/AstrBotDevs/AstrBot> | 见上游仓库 |
| NapCat | QQ ↔ OneBot v11 适配 | <https://github.com/NapNeko/NapCatQQ> | 见上游仓库 |
| QQNT | 被 NapCat 注入的 QQ 客户端 | 腾讯 | 腾讯软件许可 |

## 二、`astrbot/plugins/astrbot_plugin_memories/`（**上游 MIT 插件的大幅修改版**）

- 原始插件名：`astrbot_plugin_memories`（"她自己的小回忆"）
- **原作者：若剑清**
- 原始版本：v0.5.1（`metadata.yaml` 保留原 `version` 与 `author` 字段）
- 协议：MIT（原插件目录内的 `LICENSE` 一并保留）
- 上游仓库地址：原作者未在插件元数据中填写（`repo: ""`），请按插件名在该作者的发布页查找；本项目不对上游地址做猜测。
- 修改者：XiaoJing contributors
- 每个 `.py` 文件头部都加了三行出处声明；`metadata.yaml` 的 `author` 写成「若剑清（原作者） / XiaoJing contributors（修改）」。

我们在这个插件上做的修改（按主题）：

| 文件 | 我们改了什么 |
| --- | --- |
| `main.py` | 记忆注入链路重写（四路召回、昨日日记、最近两天日记、称呼规则）；每日总结「只写当天 / 覆盖整天 / 第一人称」；主动消息改造（30 字上限、不许由头、没话题不发、`plain` 兜底）；语言后处理（`_strip_aitics` 与 `_YUBAGU_*` 禁句表、心理咨询腔、自我抹淡、"守着"族） |
| `store.py` | 稀有词门槛与 `_name_terms`（人名可检索）、`pinned` 免疫遗忘、蛋糕衰减 |
| `settings.py` | 注入/遗忘/整合/语义等阈值项 |
| `vec.py` | 保持上游实现，仅调常量与注释 |
| `people.py` / `tools.py` / `conv_state.py` | 示例数据脱敏、工具返回值与状态块沿用上游结构 |
| `pages/memories-panel/` | 管理面板页（上游 HTML/JS），仅脱敏示例 |
| `README.md` | 上游插件文档，仅脱敏示例 |

## 三、借鉴的第三方思路

| 出处 | 借鉴了什么 | 落在哪 |
| --- | --- | --- |
| `astrbot_plugin_fish_heart`（AstrBot 插件，作者未在元数据中署名） | 「发送前自检」的思路 | `main.py:195` 的 `PET3:scenegurd` —— 我们把它做成了确定性正则（0 token），只拦「她描述自己的身体/物理场景」 |
| NapCat 官方文档 | QQ 更新守卫（锁 `hotUpdate`）、OneBot 反向 WS 接法 | `napcat/qq-update-guard.ps1`、`napcat/onebot11.example.json`、`docs/napcat.md` |
| AstrBot 官方文档 | 插件目录结构、`metadata.yaml` 字段、WebUI 端口约定 | `docs/astrbot.md`、`astrbot/config.example.json` |

以上借鉴均为**思路层面**，代码为本项目自行编写；如原作者认为署名方式需要调整，欢迎提 issue。

## 四、模型与素材

本仓库不含任何模型、贴图、表情文件，也不含 Live2D 相关代码。

如果你自己接了 Live2D 桌宠，文档里提到的那只免费模型（不在本仓库内）：

- 「免费 live2D 模型｜Deepseek 小鲸鱼｜连带 psd 一起分享」
  <https://www.bilibili.com/video/BV1KmxY66EZ3/>
  - 美术（Artist）：@神宫凉子
  - 绑定（Rigger）：@霓虹猫_NeonNeko
  - 视频剪辑：@泣零7ero
  - 人设出处：@ZipZipPipe、@上善无形
- 获取方式（作者原文）：工房 0.1，或作者群公告里的网盘链接。
- 作者声明：完全开放以此模型为素材的二创与直播使用；**具体条款以压缩包内的《使用须知》为准**，下载后先读它。
- 本仓库不转发该模型，也不对其授权做任何转授。

## 五、本仓库原创部分

`astrbot/plugins/` 下除 `astrbot_plugin_memories` 之外的三个插件（`astrbot_plugin_forward`、`astrbot_plugin_ocr`、`astrbot_plugin_video_bridge`）、`astrbot/persona/banned-phrases.md`、`scripts/`、`docs/`、根目录的 README 与示例配置，均为 **XiaoJing contributors** 原创，按本仓库 `LICENSE`（MIT）发布。

如果你要转发、改编，保留本文件与各文件头部的出处声明即可。
