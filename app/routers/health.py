from fastapi import APIRouter

from app.llm import runtime_summary

router = APIRouter()


@router.get("/health")
def health():
    # 只暴露 provider/model 名称，绝不暴露 API Key；便于确认修改后的运行进程是否已重载。
    return {"status": "ok", "llm": runtime_summary()}
