from __future__ import annotations


def show_front(window) -> None:
    """Restore a Qt window and ask Windows to bring it to the foreground."""
    if hasattr(window, "showNormal"):
        window.showNormal()
    else:
        window.show()
    window.raise_()
    window.activateWindow()
