"""企业微信「自研 hook」渠道（路径 B）。

架构对照个人微信 hook：外部注入器把自研 `wecom_hook.dll` 注入企微进程 →
DLL 在企微进程内起本地 HTTP 桥 `127.0.0.1:<port>` → Python 侧 `WeComConnector`
管理注入生命周期、`BridgeClient` 收发、`WeComHookAdapter` 接入挂件的 answer() 大脑。

【当前=走路骨架】：桥的 /health /ping /echo 已通；消息 hook（send/收）尚未实现，
等企微 5.0.3.6005 的内存偏移逆向到位后填 `native/offsets/wework-5.0.3.6005.yaml`。
"""
from widget.wecom.config import WeComHookConfig
from widget.wecom.bridge_client import BridgeClient, BridgeError
from widget.wecom.adapter import WeComHookAdapter
from widget.wecom.connector import WeComConnector, ConnState, WeComConnectError

__all__ = [
    "WeComHookConfig", "BridgeClient", "BridgeError", "WeComHookAdapter",
    "WeComConnector", "ConnState", "WeComConnectError",
]
