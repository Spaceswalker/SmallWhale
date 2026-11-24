# napcat

QQ 版本守卫。QQ 自动升级会让 NapCat 适配层失效、小鲸直接掉线，所以这里把 QQ 钉在某个版本上。

## 文件

| 文件 | 说明 |
| --- | --- |
| `qq-update-guard.ps1` | 清掉 QQNT `versions/config.json` 里的 `hotUpdate.ready`，让启动器停在当前版本 |
| `qq-update-guard.vbs` | 隐藏窗口启动上面那个脚本（给任务计划用） |
| `onebot11.example.json` | OneBot v11 连接示例（反向 WS + HTTP） |

## 配置

版本号与 QQ 安装目录在 `../scripts/_config.ps1`：

```powershell
$XJ_QQ_VERSION = '9.9.33-52230'
$XJ_QQNT       = 'C:\Program Files\Tencent\QQNT'
```

改成你自己在用的那个能跑 NapCat 的版本。

## 手动跑

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\qq-update-guard.ps1
```

日志写 `qq-update-guard.log`。撤销就是删掉任务计划里那一条（这个脚本没有其它副作用）。
