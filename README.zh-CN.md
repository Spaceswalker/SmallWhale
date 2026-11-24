# 小鲸 · XiaoJing

> English version: [README.md](README.md)
> `docs/` 里的安装文档同样是中文的。

一个能自己搭起来的 QQ AI 陪伴角色：她会在 QQ 里跟你聊天、记得住事、偶尔主动找你。

**这是什么。** 小鲸不是一个双击就能装好的软件，而是一套**能被复现的工程**：一个会记事、说话像人的 QQ 聊天人格，加上一圈让它不容易死掉的守护脚本。我们踩过的坑、调过的参数——记忆管线、人设里的语言卫生、端口接线、版本守卫——都以代码和文档的形式留在这里。

**为什么它不太一样。** 大多数「AI 陪伴」项目止步于一段系统提示词，这里把记忆当成一个真正的子系统：每天一份第一人称日记，只写当天真实发生过的事；日记会被直接塞进系统提示词，而不是指望检索碰运气；同时明确告诉模型——记录里没有的事就是没发生过，所以它会说「我记不清了」，而不是编一个版本。再叠一层语言过滤，把「这话我记下了」「我就在这儿」这类一眼假的句式在输出前后各拦一次，再配上一批真人口吻的例句，话就短下来了。

**真正的创新点。** 一是「用日记回答昨晚」的召回设计——不靠向量搜索猜，而是靠写下来的记录；二是**语言卫生做成两层**——人设层面给正例，代码层面用正则兜底，同一句套话既不会从模型嘴里冒出来，也不会在转发前漏过去。

**便捷性。** 端口全部固定并写进文档（NapCat、AstrBot、反向 WS），一个脚本按顺序拉起整条链，一个 VBS 入口让它在开机时无声启动，每个守护脚本都知道怎么把自己那一段救回来。除了一个 OpenAI 兼容的模型接口，不需要任何云服务，数据不出本机。

**用 Agent 直接部署。** 这套文档是**写给执行者看的**：把仓库 clone 下来，用 DeepSeek Harness、Claude Code 或 Codex CLI 打开，直接说「帮我部署这个项目」，它就能照着 `docs/quickstart.md` 一步步做——装 Python 依赖、把插件拷进 AstrBot、配好 NapCat 的反向 WebSocket、改 `scripts/_config.ps1` 里的路径、最后跑一次 `start-stack.ps1 -DryRun` 自检。你需要亲手做的只有两件：登录一次 QQ、填自己的模型 key。

---

## 它由哪几块组成

```
                    ┌──────────────────────────────┐
   QQ 客户端 ──────▶ │  NapCat（OneBot v11 实现）    │
   （QQNT）          └──────────────┬───────────────┘
                                    │ 反向 WebSocket :6199
                                    ▼
   ┌────────────────────────────────────────────────┐
   │  AstrBot（Python）                              │
   │   ├─ astrbot_plugin_memories   记忆 / 日记 / 主动找人 │
   │   ├─ astrbot_plugin_forward    转发                │
   │   ├─ astrbot_plugin_ocr        图片文字进对话       │
   │   └─ astrbot_plugin_video_bridge 视频号桥接        │
   └────────────────────────────────────────────────┘
```

- **AstrBot**：QQ 侧的对话大脑（上游，MIT）
- **NapCat**：把 QQ 变成 OneBot v11 接口（上游，见其仓库协议）
- **astrbot_plugin_memories**：本仓库改动最多的插件——事件记忆、蛋糕权重、遗忘/合并、每日日记、第一人称回忆、主动消息、语言过滤
- **scripts/**：守护与守卫（AstrBot 挂了自动拉起、QQ 偷偷升级时把版本锁回去）

---

## 她能做什么（功能清单）

- 私聊 / 群聊，短句口语化；性格由你自己的人设决定，插件只保证说话不像 AI
- 长期记忆：写日记（只写当天、第一人称、覆盖整天）、周报、人物卡、称呼偏好
- 主动开口：有话就说一句（「你还在忙吗」），没话题就不发；一天最多几次

---

## 快速开始

> 完整步骤见 `docs/quickstart.md`。这里只给路径。

0. **省事的方法**：把仓库交给一个 coding agent（DeepSeek Harness / Claude Code / Codex CLI），说一句「照着 docs/quickstart.md 帮我部署」，剩下的步骤让它跑；你只需要登录 QQ、填模型 key。
1. 装依赖：Python 3.10+、QQ（QQNT）
2. 按 `docs/astrbot.md` 装 AstrBot，把 `astrbot/plugins/*` 拷进 `AstrBot/data/plugins/`
3. 按 `docs/napcat.md` 装 NapCat，配置反向 WS 到 `ws://127.0.0.1:6199/ws`
4. 人设本仓库不提供——自己写一段贴进 AstrBot 的「人格」（WebUI → 人格）。
   如果想让她的说话方式别太像 AI，把 `astrbot/persona/banned-phrases.md` 里那份禁句表抄进去就行。
5. 按 `docs/autostart.md` 配好守护脚本，之后它们会自己活着

---

## 目录结构

```
.
├─ README.md
├─ LICENSE                    本仓库自己的代码（MIT）
├─ THIRD-PARTY.md             上游项目与协议
├─ docs/                      安装、配置、排错
├─ astrbot/
│  ├─ plugins/                记忆 / 转发 / OCR / 视频桥
│  ├─ persona/banned-phrases.md  只要语言约束、不要身份设定时用这份
│  └─ config.example.json     AstrBot 关键配置样例
├─ napcat/                    QQ 版本守卫 + OneBot 配置样例
└─ scripts/                   守护脚本（PowerShell + VBS）
```

---

## 你需要自己准备

- 一个能用的 LLM API（OpenAI 兼容接口即可）：在 AstrBot 里配 provider
- 一个 QQ 号给小鲸（建议小号），以及 NapCat 的登录
- 一个 Live2D 模型 —— 如果你也想让她有张脸，可以用这个开源模型：[「免费 live2D 模型｜Deepseek 小鲸鱼」](https://www.bilibili.com/video/BV1KmxY66EZ3/)（美术 @神宫凉子 ／ 绑定 @霓虹猫_NeonNeko），用前读压缩包里的使用须知。本仓库不含模型文件，也不含 Live2D 相关代码。

---

## 说明

- 这套东西是「一个人自用 + 边用边改」长出来的：参数是按非常具体的个人使用场景调的，不是通用最优值，换模型/换人设要重新试。
- 涉及第三方上游的部分只给补丁与说明，不整包转发，请按各自协议自行获取。
- 提交说明统一写成 `<type>: <一句话>`，见 [docs/commit-style.md](docs/commit-style.md) 和 `scripts/commit.ps1`。

---

## 出处与署名

本仓库不整包转发任何第三方项目；所有借用的部分在 [THIRD-PARTY.md](THIRD-PARTY.md) 里逐文件列明。

- **astrbot_plugin_memories** —— 原插件作者 **若剑清**，MIT，v0.5.1。这里的副本是大幅修改版：每个 `.py` 文件头部都有三行出处声明，`metadata.yaml` 保留原作者，原 `LICENSE` 一并保留。
- **AstrBot / NapCat / QQNT** —— 运行依赖，由使用者自行获取，本仓库不打包。
- **借鉴的思路**（代码为本项目自写）：「发送前自检」来自 `astrbot_plugin_fish_heart` 插件的思路；QQ 版本守卫与 OneBot 反向 WS 来自 NapCat 官方文档。
- **文档推荐的 Live2D 模型** —— 美术 @神宫凉子、绑定 @霓虹猫_NeonNeko、剪辑 @泣零7ero；不在本仓库内，按它自己的使用须知授权。
