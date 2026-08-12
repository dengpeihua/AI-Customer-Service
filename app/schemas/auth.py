from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    tenant_id: int
    login: str
    password: str = Field(max_length=72)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: int
    tenant_id: int
    login: str
    role: str
