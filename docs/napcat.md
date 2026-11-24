# NapCat 侧：接入与「挂件」

## 装与登录

1. 从上游装 NapCat（推荐它自带的 QQNT 集成方式）
2. 启动后打开 WebUI（默认 `http://127.0.0.1:6099`）
3. 扫码登录小鲸的 QQ

## 把 OneBot 接到 AstrBot

NapCat → 网络配置 → **WebSocket 客户端（反向 WS）**：

```
ws://127.0.0.1:6199/ws
```

AstrBot 侧是 WS 服务端（默认 6199），收到连接即在线。
需要 HTTP 推送（比如转发插件主动发消息）时，在 NapCat 里另开一个 HTTP 服务端到 `6180`。

样例见 `napcat/onebot11.example.json`。

## 「挂件」指什么

这个仓库里的 `napcat/` 放的是**与你 QQ 生活强相关的那部分**：

- `qq-update-guard.ps1` —— QQ 自动升级会直接把 NapCat 顶掉线。这个脚本盯住
  `QQNT\versions\config.json`，把 `hotUpdate.ready` 清回 false，让启动器停在当前版本
- `qq-update-guard.vbs` —— 隐藏窗口跑它，配合任务计划

> 原理与风险：脚本只改「ready / pendingVersion」这两个字段，不动安装文件、不碰账号数据，
> 写回时强制 UTF-8 无 BOM（QQ 自己读这个文件，BOM 会让它报配置损坏）。

如果你不用 QQ 版本锁，删掉这两个文件不影响其它功能。

## 常见坑

- 登录二维码过期：重开 WebUI 刷新
- AstrBot 显示适配器离线：多半是 6199 端口被占，或 NapCat 的反向 WS 地址写错（要带 `/ws`）
- 消息发不出：检查小鲸那个号是否被风控、以及 AstrBot 里平台适配器是否 Online
