"""Pydantic schema：账户与 Token 的输入/输出结构。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class UserCreate(BaseModel):
    """注册/登录入参。"""

    username: str
    password: str = Field(..., min_length=8)


class UserOut(BaseModel):
    """账户出参。"""

    id: int
    username: str


class Token(BaseModel):
    """登录返回的访问令牌。"""

    access_token: str
    token_type: str = "bearer"
