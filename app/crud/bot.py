from sqlalchemy.orm import Session

from app.models.config import BotConfig


def get_or_create(db: Session, tenant_id: int) -> BotConfig:
    cfg = db.get(BotConfig, tenant_id)
    if cfg is None:
        cfg = BotConfig(tenant_id=tenant_id)
        db.add(cfg)
        db.commit()
    return cfg


def update(
    db: Session, tenant_id: int, *, welcome: str, persona: str,
    tone_level: str = "warm",
) -> BotConfig:
    cfg = get_or_create(db, tenant_id)
    cfg.welcome = welcome
    cfg.persona = persona
    cfg.tone_level = tone_level
    db.add(cfg)
    db.commit()
    return cfg
