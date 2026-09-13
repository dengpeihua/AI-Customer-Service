from __future__ import annotations

import os
import sys
from pathlib import Path


def redirect_output_from_env():
    log_dir = os.environ.get("ACS_WIDGET_LOG_DIR")
    if not log_dir:
        return []
    path = Path(log_dir)
    path.mkdir(parents=True, exist_ok=True)
    stdout = (path / "widget.out.log").open("w", encoding="utf-8", buffering=1)
    stderr = (path / "widget.err.log").open("w", encoding="utf-8", buffering=1)
    sys.stdout = stdout
    sys.stderr = stderr
    return [stdout, stderr]


if __name__ == "__main__":
    _log_handles = redirect_output_from_env()
    from widget.app import run

    _owns_app = False

    def _mark_app_owned() -> None:
        global _owns_app
        _owns_app = True

    try:
        run("widget_config.yaml", on_ownership_acquired=_mark_app_owned)
    finally:
        # A second shortcut launch is rejected by the single-instance lock. It must not
        # stop the backend/Mem0 owned by the already-running first workbench.
        if _owns_app and os.environ.get("ACS_MANAGE_SERVICES") == "1":
            try:
                from widget.process_lifecycle import stop_product_services

                stop_product_services(Path(__file__).resolve().parent)
            except Exception as error:
                print(f"[退出] 后台服务清理失败：{error}", file=sys.stderr)
        # 托盘「退出」后进程曾残留在后台，客户得去任务管理器手动杀。原因：QApplication 退出、
        # 各渠道 stop 后仍有 Qt/网络等非 daemon 资源线程挂着，pythonw 不会自然收尾。
        # 该退时就干净退：先刷日志句柄，再 os._exit(0) 兜底强杀所有残留线程（业务清理已在 run() 里做完）。
        for _h in _log_handles:
            try:
                _h.flush()
            except Exception:
                pass
        os._exit(0)
