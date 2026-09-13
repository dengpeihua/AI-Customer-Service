from __future__ import annotations

from typing import Callable

from widget.models import InboundMsg


class ChannelHub:
    """抖音多账号渠道注册中心。

    每条渠道 = 一个 adapter（收发）+ 一个 pipeline（收→判→发/待人工）。入站消息按其
    `channel` 字段路由到对应 pipeline；人工回复/释放也按 channel 找回正确的 adapter/pipeline，
    保证待人工回复始终发回原抖音账号。

    第一个注册的渠道为默认渠道：channel=None 时（单渠道旧调用/无渠道信息）落到它。
    """

    def __init__(self):
        self._adapters: dict[str, object] = {}
        self._pipelines: dict[str, object] = {}
        self._default: str | None = None

    def register(self, channel: str, adapter, pipeline) -> None:
        self._adapters[channel] = adapter
        self._pipelines[channel] = pipeline
        if self._default is None:
            self._default = channel

    def _resolve(self, channel: str | None) -> str | None:
        if channel is not None:
            return channel
        if len(self._adapters) > 1:
            return None            # 多实例：禁隐式回落，须显式 channel_key
        return self._default

    def handle(self, msg: InboundMsg) -> str:
        pipe = self._pipelines.get(self._resolve(msg.get("channel")))
        return pipe.handle(msg) if pipe is not None else "ignored"

    def adapter(self, channel: str | None = None):
        return self._adapters.get(self._resolve(channel))

    def pipeline(self, channel: str | None = None):
        return self._pipelines.get(self._resolve(channel))

    def channels(self) -> list[str]:
        return list(self._adapters)

    def adapters(self) -> dict[str, object]:
        return dict(self._adapters)

    def start_all(self, on_message: Callable[[InboundMsg], None]) -> dict[str, str]:
        """启动所有渠道的轮询；返回 {channel: ""(成功) | 错误信息}，单条失败不影响其它。"""
        result: dict[str, str] = {}
        for ch, ad in self._adapters.items():
            try:
                ad.start(on_message)
                result[ch] = ""
            except Exception as e:               # noqa: BLE001 —— 一条渠道起不来不该拖垮另一条
                result[ch] = str(e)
        return result

    def stop_all(self) -> None:
        for ad in self._adapters.values():
            try:
                ad.stop()
            except Exception:
                pass
