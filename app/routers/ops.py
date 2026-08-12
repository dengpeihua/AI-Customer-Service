from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import CurrentUser
from app.ops import (
    browse_dataset, delete_dataset_rows, ops_overview, ops_registry,
    run_functional_checks,
)


router = APIRouter(prefix="/v1/ops", tags=["operations"])


class TaskDeleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[str] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def normalize_ids(self):
        self.ids = list(dict.fromkeys(value.strip() for value in self.ids if value.strip()))
        if not self.ids or any(len(value) > 64 for value in self.ids):
            raise ValueError("任务 ID 无效")
        return self


class DataDeleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset: Literal["conversations", "messages", "memories", "knowledge"]
    ids: list[int] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def normalize_ids(self):
        if any(value <= 0 for value in self.ids):
            raise ValueError("数据 ID 必须为正整数")
        self.ids = list(dict.fromkeys(self.ids))
        return self


@router.get("/overview")
def overview(user: CurrentUser, db: Annotated[Session, Depends(get_db)]):
    return ops_overview(db, user.tenant_id)


@router.delete("/tasks")
def delete_tasks(body: TaskDeleteIn, user: CurrentUser):
    return {"deleted": ops_registry.delete(user.tenant_id, body.ids)}


@router.get("/data")
def data_browser(user: CurrentUser, db: Annotated[Session, Depends(get_db)],
                 dataset: str = Query(default="conversations"),
                 limit: int = Query(default=100, ge=1, le=200)):
    try:
        return browse_dataset(db, user.tenant_id, dataset, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/data")
def delete_data_rows(
    body: DataDeleteIn,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
):
    try:
        return delete_dataset_rows(
            db, user.tenant_id, body.dataset, body.ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/tests")
def functional_tests(user: CurrentUser, db: Annotated[Session, Depends(get_db)]):
    task_id = ops_registry.start(user.tenant_id, "ops.functional_tests", "运行安全功能检查")
    try:
        checks = run_functional_checks(db, user.tenant_id)
        failed = sum(item["status"] == "failed" for item in checks)
        ops_registry.finish(task_id, status="failed" if failed else "completed",
                            detail=f"{len(checks) - failed}/{len(checks)} 项通过")
        return {"checks": checks, "passed": failed == 0}
    except Exception as exc:  # noqa: BLE001
        ops_registry.finish(task_id, status="failed", detail=type(exc).__name__)
        raise
