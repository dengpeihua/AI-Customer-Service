from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI

from app.admin.router import router as admin_router
from app.config import validate_agent_routing_config, validate_production_secrets
from app.llm import close_llm, runtime_summary
from app.routers import (auth, broadcast, chat, conversations, customers, health, kb,
                         memories, ops, tags, tenant)

logger = logging.getLogger(__name__)

# 进程启动即自检：生产环境用弱/默认密钥则直接拒绝启动（dev 无感）。
validate_production_secrets()
validate_agent_routing_config()
logger.info("LLM runtime configured: %s", runtime_summary())

@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        yield
    finally:
        close_llm()


app = FastAPI(title="AI Customer Service", lifespan=lifespan)
app.include_router(health.router)
app.include_router(auth.router)
app.include_router(kb.router)
app.include_router(tenant.router)
app.include_router(chat.router)
app.include_router(conversations.router)
app.include_router(customers.router)
app.include_router(memories.router)
app.include_router(ops.router)
app.include_router(tags.router)
app.include_router(broadcast.router)
app.include_router(admin_router)
