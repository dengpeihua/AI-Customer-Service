from sqlalchemy.orm import Session

from app.models.tenant import Tenant


def create_tenant(db: Session, name: str, plan: str = "basic") -> Tenant:
    t = Tenant(name=name, plan=plan)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def get_tenant(db: Session, tenant_id: int) -> Tenant | None:
    return db.get(Tenant, tenant_id)
