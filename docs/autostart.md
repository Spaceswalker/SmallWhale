# 自启动（开机 → QQ/NapCat → AstrBot）

整条链一共两段，**每一段都是独立的**，坏一段不影响别的段。仓库里 `scripts/` 下的脚本就是干这件事的。

```
开机登录
  └─ 任务计划：wscript.exe scripts\xj-boot.vbs
        └─ start-stack.ps1
              ├─ 1. 拉起 QQ（NapCat 是注入进 QQNT 的，QQ 起来 NapCat 就在）→ 等 :6099
              └─ 2. 拉起 AstrBot → 等 :6185 / :6199
```

## 一次性设置

1. **QQ 自动登录**：打开 QQ → 设置 → 登录 → 勾「自动登录 / 记住密码」。
   ⚠️ 这一步只能你自己勾，脚本没法替你输密码；不勾的话开机后 QQ 会停在扫码界面，NapCat 也就不会上号。
2. **NapCat**：本项目的 NapCat 是「装进 QQNT 目录」那种（`NapCat` 的 `bootmain/framework/shell` 三个目录和 QQ 的 `resources` 同级）。装好后启动 QQ 会自动带起 NapCat，WebUI 在 `http://127.0.0.1:6099`。
3. **AstrBot**：`docs/astrbot.md` 里配好 `cmd_config.json`（DeepSeek 等 provider、NapCat 反向 WS 地址 `ws://127.0.0.1:6199/ws`）。
4. **改配置**：编辑 `scripts/_config.ps1`，把 `XJ_ASTRBOT`、`XJ_NAPCAT`、`XJ_QQNT`、`XJ_QQ_EXE` 换成你自己的路径。

## 挂到「登录时自动运行」

用任务计划程序（推荐，比启动文件夹稳）：

```powershell
# 在仓库根目录执行（把 <REPO> 换成实际路径）
$repo = "<REPO>"
$act  = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$repo\scripts\xj-boot.vbs`""
$trg  = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName "XiaoJing-Boot" -Action $act -Trigger $trg -RunLevel Highest -Force
```

不想用任务计划，也可以把 `xj-boot.vbs` 的快捷方式丢进 `shell:startup`（`Win+R` 输入 `shell:startup`）。

## 各脚本职责

| 脚本 | 作用 |
| --- | --- |
| `start-stack.ps1` | 按顺序拉起 QQ/NapCat → AstrBot；`-DryRun` 只检查不启动 |
| `xj-boot.vbs` | 隐藏窗口调用 `start-stack.ps1`（开机入口） |
| `astrbot-keeper.ps1` | 常驻：AstrBot 挂了就重拉 |
| `keeper-watchdog.ps1` | 常驻：keeper 自己挂了就重拉 |
| `qq-update-guard.ps1` | 锁住 QQ 版本，防止 QQ 自动升级把 NapCat 注入搞坏 |
| `backup-astrbot.ps1` | 定时备份 AstrBot 的 data 目录 |

## 单独用

```powershell
# 只检查配置对不对（不会启动任何程序）
powershell -File scripts\start-stack.ps1 -DryRun

# 完整拉起（QQ + AstrBot）
powershell -File scripts\start-stack.ps1

# 今天已经手动开过 QQ 了，只想补 AstrBot
powershell -File scripts\start-stack.ps1 -NoQQ
```

## 排错

- `:6099` 不通 → QQ 没登录（多半是没勾自动登录，或者 QQ 版本被升级了）。
- `:6185` 不通 → 看 `scripts/logs/` 与 AstrBot 自己的日志；`astrbot-keeper.ps1` 会一直重试。
- 中文乱码 → `.ps1` 要存成 **UTF-8 with BOM**；`pwsh` 7 与 Windows PowerShell 5.1 都认这个格式。
