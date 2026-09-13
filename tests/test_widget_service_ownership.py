from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_service_cleanup_requires_single_instance_ownership() -> None:
    runner = (ROOT / "run_widget.py").read_text(encoding="utf-8")
    app = (ROOT / "widget" / "app.py").read_text(encoding="utf-8")

    assert "if _owns_app and os.environ.get" in runner
    assert "on_ownership_acquired=_mark_app_owned" in runner
    assert "on_ownership_acquired()" in app
    assert app.index("if not acquire(") < app.index("on_ownership_acquired()")
