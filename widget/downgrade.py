# widget/downgrade.py
"""强制把微信 / 企业微信降到**可注入版本**（挂件唯一支持的版本）。

为什么要有这个模块：hook 是版本强绑定的（微信 4.1.10.27、企微 5.0.3.6005 —— 企微侧
`WeComHookConfig.strict_version` 版本不符直接拒注）。客户机上腾讯自动更新一升级，渠道就
整条掉线。以前只有「重跑启动器」这一条路（而且企微那条路 **明确拒绝降级**），现在挂件里
可以一键强降。

设计铁律：
- **微信不重新实现**：装 4.1.10.27 + 备份/铺 version.dll 的流程，项目自己发布的
  `Start-AICustomerService.ps1` 里已经有（`Install-WeChatIfNeeded` / `Install-Hook` /
  `Ensure-WeChatUpdateBlocked` / `Block-WeChatUpdateHosts`）。这里**只复用那几个函数**。
  ⚠️ 不能直接 `-File` 跑整份启动器 —— 它的主流程会重起后端、并且 `Stop-StaleWidgetProcesses`
  会把**正在调用它的挂件自己**杀掉。所以用 PowerShell AST 只把「函数定义」摘出来 dot-source，
  再按顺序调用需要的那几个，主流程一行都不执行。
- **企微是净新增**：启动器里 `Install-WeComIfNeeded` 只在「完全没装」时装，版本不符是明确
  跳过不降级的。这里补上：停 WXWork → 静默 `/S` 装**包内自带**的 5.0.3.6005 安装包。
- **绝不联网下载**：包里没带安装包就 fail closed（返回 False + 明确提示），不去 dldir1 拉。
- **需要管理员**：装程序 / 换 `Program Files` 下的 version.dll / 改 hosts 都要提权，统一走
  `Start-Process -Verb RunAs`（客户会看到一次 UAC）。
- **best-effort，绝不抛进 GUI**：全部包 try/except，失败只返回 False + 记日志。
- 真正的「要不要降」由 UI 确认框把关；本模块被调用 = 已经确认过了。

测试契约：`runner(argv: list[str]) -> int`（注入即可全程不真跑）；命令里内层脚本用
`-EncodedCommand`（base64/UTF-16LE）避免转义地狱，`decode_elevated_argv()` 可原样解回来断言。
"""
from __future__ import annotations

import base64
import re
import subprocess
from pathlib import Path
from typing import Callable, Optional, Union

CREATE_NO_WINDOW = 0x08000000

#: 微信可注入版本（hook 按这个版本逆的；启动器 $ExpectedWeChatVersion 同值）
WECHAT_INJECTABLE_VERSION = "4.1.10.27"
#: 企微可注入版本（与 `WeComHookConfig.required_version` / offsets yaml 必须一致）
WECOM_INJECTABLE_VERSION = "5.0.3.6005"

LAUNCHER_SCRIPT_NAME = "Start-AICustomerService.ps1"
WECHAT_INSTALLER_NAME = f"WeChatSetup_{WECHAT_INJECTABLE_VERSION}.exe"
WECOM_INSTALLER_NAME = f"WeCom_{WECOM_INJECTABLE_VERSION}.exe"

#: 装程序可能很慢（企微安装包 500MB+），给足超时；仍然有上限，免得卡死调用线程
DEFAULT_TIMEOUT_S = 1800

PathLike = Union[str, Path]
Runner = Callable[[list[str]], int]
Logger = Callable[[str], None]


# --------------------------------------------------------------------------- 基础设施

def repo_root() -> Path:
    """安装根目录（挂件包 / 开发仓的根）—— `widget/` 的上一级。"""
    return Path(__file__).resolve().parents[1]


def _log(log: Optional[Logger], message: str) -> None:
    """记一行日志；日志槽自己炸了也不能影响主流程。"""
    if log is None:
        return
    try:
        log(message)
    except Exception:                                   # noqa: BLE001
        pass


def is_admin() -> bool:
    """当前进程是否管理员。拿不到就当不是（只用于提示，不做门禁）。绝不抛。"""
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())   # type: ignore[attr-defined]
    except Exception:                                   # noqa: BLE001
        return False


def _ps_quote(value: PathLike) -> str:
    """包成 PowerShell 单引号字符串（内部单引号翻倍）。"""
    return "'" + str(value).replace("'", "''") + "'"


def _default_runner(argv: list[str]) -> int:
    return subprocess.run(argv, capture_output=True, text=True,
                          timeout=DEFAULT_TIMEOUT_S,
                          creationflags=CREATE_NO_WINDOW).returncode


# --------------------------------------------------------------------------- 定位资源

def find_launcher_script(root: Optional[PathLike] = None) -> Optional[Path]:
    """定位已发布的启动器脚本：客户包在 `scripts\\`，开发仓在 `customer_docs\\scripts\\`。"""
    base = Path(root) if root is not None else repo_root()
    for rel in (Path("scripts"), Path("customer_docs") / "scripts"):
        candidate = base / rel / LAUNCHER_SCRIPT_NAME
        if candidate.is_file():
            return candidate
    return None


def find_wecom_installer(root: Optional[PathLike] = None) -> Optional[Path]:
    """定位**包内自带**的企微 5.0.3.6005 安装包（客户包在 `assets\\wecom\\`，开发仓在根）。"""
    base = Path(root) if root is not None else repo_root()
    for rel in (Path("assets") / "wecom" / WECOM_INSTALLER_NAME, Path(WECOM_INSTALLER_NAME)):
        candidate = base / rel
        if candidate.is_file():
            return candidate
    return None


# --------------------------------------------------------------------------- 提权命令

_ENCODED_RE = re.compile(r"-EncodedCommand','([A-Za-z0-9+/=]+)'")


def build_elevated_argv(inner_script: str) -> list[str]:
    """把一段 PowerShell 脚本包成「以管理员身份跑、等它结束、把返回码带回来」的命令行。

    内层用 `-EncodedCommand`（base64/UTF-16LE）：脚本里怎么写引号都不会被外层吃掉。
    """
    encoded = base64.b64encode(inner_script.encode("utf-16-le")).decode("ascii")
    outer = (
        # UAC 被取消 / 起不来时 Start-Process 会报错且 $p 为 $null——直接 `exit $p.ExitCode`
        # 等于 `exit $null` = 0 = **假成功**。所以 Stop + 显式 null 判空，失败必须是非 0。
        "$ErrorActionPreference = 'Stop'; "
        "$p = Start-Process -FilePath 'powershell.exe' -Verb RunAs -PassThru -Wait "
        "-ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-EncodedCommand',"
        f"'{encoded}'); "
        "if ($null -eq $p) { exit 1 }; exit $p.ExitCode"
    )
    return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", outer]


def decode_elevated_argv(argv: list[str]) -> str:
    """从 `build_elevated_argv` 造出的命令行里解回内层脚本（测试/排障可读）。"""
    for part in argv:
        match = _ENCODED_RE.search(str(part))
        if match:
            try:
                return base64.b64decode(match.group(1)).decode("utf-16-le")
            except Exception:                           # noqa: BLE001
                return ""
    return ""


# --------------------------------------------------------------------------- 脚本正文

def build_wechat_downgrade_script(*, script: PathLike, root: PathLike) -> str:
    """只摘启动器里的**函数定义**并调用装机/铺 hook 那几个——主流程（起后端/起挂件）一行不跑。"""
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"$Root = {_ps_quote(root)}",
        f"$ExpectedWeChatVersion = '{WECHAT_INJECTABLE_VERSION}'",
        f"$launcher = {_ps_quote(script)}",
        "try {",
        "    $ast = [System.Management.Automation.Language.Parser]::ParseFile("
        "$launcher, [ref]$null, [ref]$null)",
        "    $defs = $ast.FindAll({ $args[0] -is "
        "[System.Management.Automation.Language.FunctionDefinitionAst] }, $true)",
        "    foreach ($d in $defs) { . ([scriptblock]::Create($d.Extent.Text)) }",
        "    $dir = Install-WeChatIfNeeded",          # 版本不符就静默 /S 重装 4.1.10.27
        "    Ensure-WeChatUpdateBlocked $dir",        # 掐掉自动更新器，免得又被升回去
        "    Block-WeChatUpdateHosts",
        "    Assert-WeChatVersion $dir",
        "    Install-Hook $dir",                      # 备份 version.dll → 铺 hook dll
        "    Write-Host \"ACS-DOWNGRADE-OK $dir\"",
        "    exit 0",
        "} catch {",
        "    Write-Host \"ACS-DOWNGRADE-FAILED $($_.Exception.Message)\"",
        "    exit 1",
        "}",
    ])


#: 找 WXWork.exe 的方式与启动器 `Get-WeComInstallDir` 同源（注册表 + Program Files 回落），
#: 也与 `widget/version_check.py` 的 Python 版一致 —— 三处必须找到同一个 exe。
_WECOM_DIR_KEYS = (
    r"HKCU:\Software\Tencent\WXWork",
    r"HKLM:\SOFTWARE\WOW6432Node\Tencent\WXWork",
    r"HKLM:\SOFTWARE\Tencent\WXWork",
)
_WECOM_DIR_FALLBACKS = (r"C:\Program Files (x86)\WXWork", r"C:\Program Files\WXWork")


def _wecom_locate_functions() -> list[str]:
    """内层脚本用的两个小函数：定位安装目录 / 读 WXWork.exe 真实版本（读不到 → 空串）。"""
    keys = ", ".join(_ps_quote(k) for k in _WECOM_DIR_KEYS)
    fallbacks = ", ".join(_ps_quote(c) for c in _WECOM_DIR_FALLBACKS)
    return [
        "function Get-AcsWeComDir {",
        f"    foreach ($key in @({keys})) {{",
        "        $ip = (Get-ItemProperty $key -ErrorAction SilentlyContinue).InstallPath",
        "        if ($ip -and (Test-Path -LiteralPath (Join-Path $ip 'WXWork.exe'))) "
        "{ return $ip }",
        "    }",
        f"    foreach ($c in @({fallbacks})) {{",
        "        if (Test-Path -LiteralPath (Join-Path $c 'WXWork.exe')) { return $c }",
        "    }",
        "    return $null",
        "}",
        "function Get-AcsWeComVersion {",
        "    $d = Get-AcsWeComDir",
        "    if (-not $d) { return '' }",
        "    try { return [string]((Get-Item -LiteralPath (Join-Path $d 'WXWork.exe'))"
        ".VersionInfo.FileVersion) } catch { return '' }",
        "}",
    ]


def build_wecom_downgrade_script(*, installer: PathLike) -> str:
    """停 WXWork →（尽力）静默卸掉不符版本 → 静默装包内自带的 5.0.3.6005 → **回读版本校验**。

    净新增（启动器那条路是明确不降级的），所以两个坑都得自己踩：
    - NSIS `/S` 把**旧版**装到**更新版**头上，多半直接返回 0 却什么都没装。只看返回码 =
      客户看到「降级成功」、白关一次企微、渠道照样注不进（假绿）。→ 装完必须回读
      `WXWork.exe` 的 `VersionInfo.FileVersion`，不等于目标版本就 `exit 1`。
    - 所以先尽力静默卸掉现装的不符版本（`Uninstall.exe /S`）再装；卸载**尽力而为**，
      包在自己的 try/catch 里，卸不掉也照样往下装（有的版本装得上去）。
    """
    lines = ["$ErrorActionPreference = 'Stop'"]
    lines += _wecom_locate_functions()
    lines += [
        f"$required = '{WECOM_INJECTABLE_VERSION}'",
        "try {",
        "    Get-Process WXWork -ErrorAction SilentlyContinue | "
        "Stop-Process -Force -ErrorAction SilentlyContinue",
        "    Start-Sleep -Seconds 3",
        # 现装版本已经是目标版本就不用卸（只是 hook 没铺/没生效，装一遍即可）
        "    $before = Get-AcsWeComVersion",
        # 版本串偶尔带逗号/后缀，与启动器一样用前缀 `-like` 判，别用严格相等
        "    if ($before -and -not ($before -like \"$required*\")) {",
        "        try {",
        "            $dir = Get-AcsWeComDir",
        "            $un = if ($dir) { Join-Path $dir 'Uninstall.exe' } else { $null }",
        "            if ($un -and (Test-Path -LiteralPath $un)) {",
        "                Write-Host \"ACS-DOWNGRADE-UNINSTALL $before\"",
        "                Start-Process -FilePath $un -ArgumentList '/S' -Wait -PassThru | "
        "Out-Null",
        "                Start-Sleep -Seconds 5",
        "            }",
        "        } catch { Write-Host \"ACS-DOWNGRADE-UNINSTALL-SKIPPED "
        "$($_.Exception.Message)\" }",
        "    }",
        f"    $installer = {_ps_quote(installer)}",
        "    $p = Start-Process -FilePath $installer -ArgumentList '/S' -Wait -PassThru",
        "    Start-Sleep -Seconds 5",
        # 安装器没起来时 $p 是 $null，`exit $p.ExitCode` = `exit 0` = 又一处假绿 → 恒 exit 1
        "    if ($null -eq $p -or $p.ExitCode -ne 0) {",
        "        Write-Host \"ACS-DOWNGRADE-FAILED installer exit $($p.ExitCode)\"",
        "        exit 1",
        "    }",
        # ★装完回读真实版本★：安装器返回 0 ≠ 真的装上了（在更新版之上尤其常见）
        "    $after = Get-AcsWeComVersion",
        "    if (-not ($after -like \"$required*\")) {",
        "        Write-Host \"ACS-DOWNGRADE-FAILED version is '$after' (want $required)\"",
        "        exit 1",
        "    }",
        "    Write-Host \"ACS-DOWNGRADE-OK $after\"",
        "    exit 0",
        "} catch {",
        "    Write-Host \"ACS-DOWNGRADE-FAILED $($_.Exception.Message)\"",
        "    exit 1",
        "}",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- 对外入口

def downgrade_wechat(
    *,
    runner: Optional[Runner] = None,
    log: Optional[Logger] = None,
    root: Optional[PathLike] = None,
) -> bool:
    """把微信强降到 4.1.10.27 并铺好 hook（复用启动器已有流程）。需管理员，会弹一次 UAC。

    成功返回 True；找不到脚本 / 用户取消 UAC / 安装失败 / 任何异常 → False（绝不抛）。
    """
    try:
        base = Path(root) if root is not None else repo_root()
        script = find_launcher_script(root=base)
        if script is None:
            _log(log, f"[downgrade] 找不到 {LAUNCHER_SCRIPT_NAME}（安装目录不完整），"
                      f"无法自动降级微信；请重新解压客户包后重试")
            return False
        argv = build_elevated_argv(build_wechat_downgrade_script(script=script, root=base))
        _log(log, f"[downgrade] 微信 → {WECHAT_INJECTABLE_VERSION}：复用 {script.name} 的"
                  f"安装/铺 hook 流程（需管理员，请在 UAC 弹窗点『是』）")
        rc = (runner or _default_runner)(argv)
    except Exception as e:                              # noqa: BLE001
        _log(log, f"[downgrade] 微信降级失败（已中止，未继续改动）：{e}")
        return False
    if rc != 0:
        _log(log, f"[downgrade] 微信降级未完成（返回码 {rc}：可能被取消或安装失败）")
        return False
    _log(log, f"[downgrade] 微信已降到 {WECHAT_INJECTABLE_VERSION} 并铺好 hook，"
              f"请重新登录微信后重启挂件")
    return True


def downgrade_wecom(
    *,
    runner: Optional[Runner] = None,
    log: Optional[Logger] = None,
    root: Optional[PathLike] = None,
) -> bool:
    """把企业微信强降到 5.0.3.6005（停 WXWork + 静默装包内安装包）。需管理员，会弹一次 UAC。

    包内没带安装包 → 直接 False + 明确提示（**绝不联网下载**）。任何异常都吞掉返回 False。
    """
    try:
        base = Path(root) if root is not None else repo_root()
        installer = find_wecom_installer(root=base)
        if installer is None:
            _log(log, f"[downgrade] 缺少自带安装包 {WECOM_INSTALLER_NAME}（找过 "
                      f"assets\\wecom\\ 和安装根目录），无法自动降级企业微信；"
                      f"请用完整客户包，或手动安装 {WECOM_INJECTABLE_VERSION} 后重启挂件")
            return False
        argv = build_elevated_argv(build_wecom_downgrade_script(installer=installer))
        _log(log, f"[downgrade] 企业微信 → {WECOM_INJECTABLE_VERSION}：将关闭企业微信并静默安装"
                  f"（需管理员，请在 UAC 弹窗点『是』）")
        rc = (runner or _default_runner)(argv)
    except Exception as e:                              # noqa: BLE001
        _log(log, f"[downgrade] 企业微信降级失败（已中止，未继续改动）：{e}")
        return False
    if rc != 0:
        _log(log, f"[downgrade] 企业微信降级未完成（返回码 {rc}：可能被取消或安装失败）")
        return False
    _log(log, f"[downgrade] 企业微信已降到 {WECOM_INJECTABLE_VERSION}，"
              f"请重新登录企业微信后重启挂件")
    return True
