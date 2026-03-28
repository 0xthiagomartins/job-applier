"""SQLite and SQLAlchemy runtime helpers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import Engine, MetaData, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base metadata shared by the SQLite models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


type SessionFactory = sessionmaker[Session]
type SessionProvider = Callable[[], Session]


def create_sqlalchemy_engine(database_url: str) -> Engine:
    """Create an engine with SQLite foreign keys enabled."""

    engine = create_engine(database_url, future=True, pool_pre_ping=True)
    url = make_url(database_url)

    if url.get_backend_name() == "sqlite":

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection: Any, connection_record: object) -> None:
            del connection_record
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def create_session_factory(database_url: str) -> SessionFactory:
    """Create a reusable session factory for the given database URL."""

    engine = create_sqlalchemy_engine(database_url)
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
