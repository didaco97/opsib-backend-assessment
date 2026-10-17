from __future__ import annotations

from fastapi import Depends
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.base import get_session_factory


def get_session():
    factory = get_session_factory()
    session = factory()
    try:
        yield session
    finally:
        session.close()


def settings() -> Settings:
    return get_settings()
