from __future__ import annotations
from typing import Callable
from urllib.parse import urlencode
import httpx
from widget.config import WidgetConfig
from widget.models import InboundMsg
from widget.secret import resolve_password

class BridgeError(Exception):
    pass

def _err_detail(e: httpx.HTTPStatusError) -> str:
    try:
        d = e.response.json().get("detail")
        return str(d) if d else ""
    except Exception:
        return ""

class Bridge:
    def __init__(self, cfg: WidgetConfig, client: httpx.Client | None = None):
        self.cfg = cfg
        self.token: str = ""
        self._client = client or httpx.Client(
            base_url=cfg.backend_base_url, timeout=15.0, trust_env=False
        )

    def login(self) -> None:
        try:
            r = self._client.post("/v1/auth/login", json={
                "tenant_id": self.cfg.tenant_id,
                "login": self.cfg.login,
                "password": resolve_password(self.cfg),
            })
            r.raise_for_status()
            self.token = r.json()["access_token"]
        except (httpx.HTTPError, ValueError, KeyError) as e:
            raise BridgeError(f"login failed: {e}") from e

    def chat(
        self, msg: InboundMsg, conversation_id: int | None = None,
        *, timeout: float = 135.0,
    ) -> dict:
        # 后端会串行执行 Triage + Handoff 目标 Agent；不能沿用普通接口 15 秒超时，
        # 否则后端生成成功但桌面端拿不到 reply/message_id，抖音私信游标又已推进，消息永久漏发。
        return self._authed("POST", "/v1/chat", json={
            "channel": msg["channel"], "contact_id": msg["contact_id"],
            "text": msg["text"], "conversation_id": conversation_id,
            "source_message_id": msg["msg_id"],
        }, timeout=timeout)

    def list_conversations(self, limit: int = 50, offset: int = 0) -> list[dict]:
        return self._authed("GET", f"/v1/conversations?limit={limit}&offset={offset}")

    def get_conversation(self, conv_id: int) -> dict:
        return self._authed("GET", f"/v1/conversations/{conv_id}")

    def mark_delivery(
        self, message_id: int, delivery_status: str, *, attempt_id: str = "",
    ) -> dict:
        return self._authed(
            "PATCH",
            f"/v1/conversations/messages/{int(message_id)}/delivery",
            json={"delivery_status": delivery_status, "attempt_id": attempt_id or None},
        )

    def draft_broadcast(self, brief: str, tone: str = "") -> str:
        body = {"brief": brief, "tone": tone or None}
        return self._authed("POST", "/v1/broadcast/draft", json=body)["draft"]

    def get_customer_profile(self, channel: str, contact_id: str) -> dict:
        q = urlencode({"channel": channel, "contact_id": contact_id})
        return self._authed("GET", f"/v1/customers/profile?{q}")

    # ---- 客户长期记忆（独立页面 + 回复个性化上下文）----
    def list_memories(self, search: str = "", memory_type: str = "", channel: str = "",
                      contact_id: str = "", limit: int = 100) -> list[dict]:
        q = urlencode({
            "search": search, "memory_type": memory_type, "channel": channel,
            "contact_id": contact_id, "limit": limit,
        })
        return self._authed("GET", f"/v1/memories?{q}")

    def memory_stats(self) -> dict:
        return self._authed("GET", "/v1/memories/stats")

    def list_memory_contacts(self) -> list[dict]:
        return self._authed("GET", "/v1/memories/contacts")

    def get_long_term_profile(self, channel: str, contact_id: str) -> dict:
        q = urlencode({"channel": channel, "contact_id": contact_id})
        return self._authed("GET", f"/v1/memories/profile?{q}")

    def ingest_memory_conversation(self, channel: str, contact_id: str,
                                   messages: list[dict], display_name: str = "",
                                   on_progress: Callable[[int, int, str], None] | None = None) -> dict:
        # 每批最多 40 条，与后端的推理边界保持一致。桌面端逐批提交后，既能展示真实
        # 完成量，也能避免用定时器伪造百分比。单批仍使用长超时覆盖本地模型推理。
        if not messages:
            raise BridgeError("没有可提取的聊天消息")
        batch_count = max(1, (len(messages) + 39) // 40)
        ingest_timeout = 180.0
        total = len(messages)
        completed = 0
        processed = 0
        skipped = 0
        final: dict = {}

        def report(done: int, stage: str) -> None:
            if on_progress is None:
                return
            try:
                on_progress(done, total, stage)
            except Exception:
                # 进度展示是旁路能力；窗口关闭等 UI 生命周期变化不能中断已确认的提取。
                pass

        for index, offset in enumerate(range(0, total, 40), 1):
            batch = messages[offset:offset + 40]
            report(
                completed,
                f"第 {index}/{batch_count} 批：Mem0 正在提取事实、偏好、需求和承诺…",
            )
            result = self._authed("POST", "/v1/memories/conversations/ingest", json={
                "channel": channel,
                "contact_id": contact_id,
                "display_name": display_name,
                "messages": batch,
            }, timeout=ingest_timeout)
            final = dict(result or {})
            processed += int(final.get("processed_messages") or 0)
            skipped += int(final.get("skipped_messages") or 0)
            completed += len(batch)
            report(completed, f"第 {index}/{batch_count} 批完成，已同步长期记忆")

        final.update({
            "processed_messages": processed,
            "skipped_messages": skipped,
            "submitted_messages": total,
            "batch_count": batch_count,
        })
        return final

    def memory_workbench(self, case_index: int = 0, session_index: int = 1,
                         question_index: int = 0, preview_limit: int = 8) -> dict:
        q = urlencode({
            "case_index": case_index,
            "session_index": session_index,
            "question_index": question_index,
            "preview_limit": preview_limit,
        })
        return self._authed("GET", f"/v1/memories/benchmarks/workbench?{q}")

    def recall_memories(self, channel: str, contact_id: str, query: str,
                        limit: int = 8) -> dict:
        return self._authed("POST", "/v1/memories/recall", json={
            "channel": channel,
            "contact_id": contact_id,
            "query": query,
            "limit": limit,
        })

    def recall_memory_benchmark(self, case_index: int, question_index: int, query: str,
                                limit: int = 8, session_index: int = 1,
                                across_sessions: bool = True) -> dict:
        return self._authed("POST", "/v1/memories/benchmarks/recall", json={
            "case_index": case_index,
            "question_index": question_index,
            "query": query,
            "limit": limit,
            "session_index": session_index,
            "across_sessions": across_sessions,
        })

    def create_memory(self, values: dict) -> dict:
        return self._authed("POST", "/v1/memories", json=values)

    def update_memory(self, memory_id: int, values: dict) -> dict:
        return self._authed("PATCH", f"/v1/memories/{memory_id}", json=values)

    def delete_memory(self, memory_id: int) -> None:
        self._authed("DELETE", f"/v1/memories/{memory_id}")

    def memory_history(self, memory_id: int) -> dict:
        return self._authed("GET", f"/v1/memories/{memory_id}/history")

    def list_memory_decisions(self, limit: int = 100) -> dict:
        return self._authed("GET", f"/v1/memories/decisions/recent?limit={int(limit)}")

    def delete_memory_decisions(self, decision_ids: list[int]) -> dict:
        return self._authed("DELETE", "/v1/memories/decisions", json={"ids": decision_ids})

    # ---- 运行观测（所有数据由后端按 JWT 租户过滤）----
    def ops_overview(self) -> dict:
        return self._authed("GET", "/v1/ops/overview")

    def browse_ops_data(self, dataset: str, limit: int = 100) -> dict:
        q = urlencode({"dataset": dataset, "limit": limit})
        return self._authed("GET", f"/v1/ops/data?{q}")

    def delete_ops_tasks(self, task_ids: list[str]) -> dict:
        return self._authed("DELETE", "/v1/ops/tasks", json={"ids": task_ids})

    def delete_ops_data(self, dataset: str, row_ids: list[int]) -> dict:
        return self._authed("DELETE", "/v1/ops/data", json={
            "dataset": dataset, "ids": row_ids,
        })

    def run_ops_tests(self) -> dict:
        return self._authed("POST", "/v1/ops/tests", json={})

    def summarize_to_kb(self, channel: str, contact_id: str) -> dict:
        return self._authed("POST", "/v1/kb/summarize",
                            json={"channel": channel, "contact_id": contact_id})

    def ingest_history_texts(self, texts: list[str], title: str = "渠道历史反哺") -> dict:
        """把渠道历史消息文本送后端 LLM 蒸馏成 FAQ 反哺知识库（非裸转储）。"""
        return self._authed("POST", "/v1/kb/summarize-texts",
                            json={"texts": texts, "title": title})

    # ---- 客户标签 / 会话级 AI 托管开关（控制台右详情面板用）----
    def list_customer_tags(self, channel: str, contact_id: str) -> list[dict]:
        q = urlencode({"channel": channel, "contact_id": contact_id})
        return self._authed("GET", f"/v1/customers/tags?{q}")

    def get_ai_mute(self, channel: str, contact_id: str) -> bool:
        q = urlencode({"channel": channel, "contact_id": contact_id})
        return bool(self._authed("GET", f"/v1/customers/ai-mute?{q}").get("muted"))

    def set_ai_mute(self, channel: str, contact_id: str, muted: bool) -> bool:
        return bool(self._authed("POST", "/v1/customers/ai-mute", json={
            "channel": channel, "contact_id": contact_id, "muted": muted}).get("muted"))

    def _authed(self, method: str, path: str, json: dict | None = None,
                timeout: float | None = None):
        # 空 token（从未登录，或启动时登录失败被吞）→ 先登录。否则 header 拼成 'Bearer '，
        # httpx 发送前就抛 LocalProtocolError（非 401）→ 永远触发不了下面的重登，AI 再不恢复。
        if not self.token:
            self.login()
        def do():
            kwargs = {
                "headers": {"Authorization": f"Bearer {self.token}"},
                "json": json,
            }
            if timeout is not None:
                kwargs["timeout"] = timeout
            r = self._client.request(method, path, **kwargs)
            r.raise_for_status()
            if r.status_code == 204:
                return None
            return r.json()
        try:
            return do()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:          # token 过期 → 重登一次重试
                self.login()
                try:
                    return do()
                except (httpx.HTTPError, ValueError, KeyError) as e2:
                    raise BridgeError(f"request failed after re-login: {e2}") from e2
            raise BridgeError(_err_detail(e) or f"request failed: {e}") from e
        except (httpx.HTTPError, ValueError, KeyError) as e:
            raise BridgeError(f"request failed: {e}") from e
