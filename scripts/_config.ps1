# ============================================================
# 小鲸部署配置（所有脚本都读这个文件）
# 默认按「本仓库就是部署根目录」推导；目录不一样就改这里。
# ============================================================

if (-not $XJ_ROOT)     { $XJ_ROOT     = Split-Path -Parent $PSScriptRoot }   # 仓库根

# 组件位置（D:\XiaoJing 这类自定义目录也可以直接写绝对路径）
if (-not $XJ_HOME)     { $XJ_HOME     = $XJ_ROOT }
if (-not $XJ_ASTRBOT)  { $XJ_ASTRBOT  = Join-Path $XJ_HOME 'AstrBot' }
if (-not $XJ_NAPCAT)   { $XJ_NAPCAT   = Join-Path $XJ_HOME 'NapCat' }
if (-not $XJ_QQNT)     { $XJ_QQNT     = 'C:\Program Files\Tencent\QQNT' }
if (-not $XJ_DSH)      { $XJ_DSH      = Join-Path $env:USERPROFILE '.dsh' }

# 账号
if (-not $XJ_BOT_QQ)    { $XJ_BOT_QQ    = '1000000001' }   # 小鲸自己的 QQ
if (-not $XJ_MASTER_QQ) { $XJ_MASTER_QQ = '1000000002' }   # 你（主用户）的 QQ

# 端口
$XJ_ASTRBOT_PORT = 6185    # AstrBot WebUI
$XJ_ONEBOT_HTTP  = 6180    # NapCat 的 OneBot HTTP（插件直推消息用）
$XJ_NAPCAT_WS    = 6199    # AstrBot <- NapCat 反向 WS

# QQ 版本锁（NapCat 只适配某几个 QQNT 版本，升级会掉线）
$XJ_QQ_VERSION   = '9.9.33-52230'

# QQ 主程序（NapCat 是注入进 QQNT 的，起 QQ 就等于起 NapCat）
if (-not $XJ_QQ_EXE)   { $XJ_QQ_EXE   = Join-Path $XJ_QQNT 'QQ.exe' }  # 若用 NapCat 自带的启动器，改成那个 exe
