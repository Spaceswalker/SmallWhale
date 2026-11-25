# 快速开始

目标：从零把这个仓库跑起来，让「小鲸」在 QQ 里能聊、记得住事、偶尔主动找你。

## 0. 准备

| 需要 | 版本 | 说明 |
| --- | --- | --- |
| Windows | 10/11 | 当前脚本是 PowerShell，Linux 需要自己改写 |
| Python | 3.10+ | AstrBot 用 |
| QQ（QQNT） | 建议锁一个版本 | NapCat 只适配部分版本，见 `docs/napcat.md` |
| LLM API | OpenAI 兼容 | 在 AstrBot 里配 |

> 两个上游都不在仓库里：**AstrBot 是 AGPL-3.0**，自己 clone 或下载；**NapCat 已停止维护、无许可证**，
> 需要自己拿到并校验（见 `docs/napcat.md` 与 `napcat/CHECKSUMS.txt`）。本仓库不含任何二进制。

目录建议（本仓库就是部署根目录）：

```
XiaoJing/
├─ AstrBot/          ← 上游 AstrBot（自己 git clone 或下载）
├─ NapCat/           ← 上游 NapCat
├─ astrbot/          ← 本仓库：插件
├─ napcat/           ← 本仓库：QQ 版本守卫
└─ scripts/          ← 本仓库：守护脚本
```

## 1. AstrBot

```powershell
git clone https://github.com/AstrBotDevs/AstrBot
cd AstrBot
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
```

把本仓库的插件拷进去：

```powershell
Copy-Item .\astrbot\plugins\* .\AstrBot\data\plugins\ -Recurse
```

启动一次，打开 `http://127.0.0.1:6185`，配好 provider（模型 + API Key），再把
人设自己写一段贴进「人格」；不想自己写的话，把 `astrbot/persona/banned-phrases.md` 里那份禁句表抄进去，至少不会一眼 AI 腔。详见 `docs/astrbot.md`。

## 2. NapCat

**这一步要你自己解决**：NapCat 上游已停止维护、也没有发布许可证，所以本仓库不分发、不转发它。

按 `docs/napcat.md` 自己拿到（社区镜像 / 社区分流 / 源码自建），先核对
`napcat/CHECKSUMS.txt` 里的 SHA-256，再装、再登录小鲸的 QQ，把反向 WebSocket 指到
`ws://127.0.0.1:6199/ws`。AstrBot 侧确认收到连接。

> 不想走 NapCat 也行：任何能提供 OneBot v11 反向 WS 的实现都可以，接法一样。

## 3. 守护

```powershell
# 手动跑一次看看
.\scripts\astrbot-keeper.ps1
```

没问题就按 `docs/autostart.md` 配开机自启（`scripts/xj-boot.vbs` 会按顺序拉起 QQ → AstrBot → 守护）。

## 4. 检查清单

- [ ] AstrBot WebUI 打得开，provider 能回话
- [ ] NapCat 已登录，AstrBot 里能看到平台适配器在线
- [ ] 私聊她，她能回、会记住「你几号生日」这类你告诉过她的事
- [ ] 问「昨天我们做了什么」她能答上来（当天日记已生成的话）
- [ ] `scripts\_config.ps1` 里的 QQ 号 / 路径都对

## 5. 想要一张脸？

桌宠不在本仓库里。如果你自己接了 Live2D，模型可以用 [这个开源模型](https://www.bilibili.com/video/BV1KmxY66EZ3/)（美术 @神宫凉子 ／ 绑定 @霓虹猫_NeonNeko，用前读压缩包里的使用须知）。
