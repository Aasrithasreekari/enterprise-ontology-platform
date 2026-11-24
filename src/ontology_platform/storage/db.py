"""Engine and session management."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ontology_platform.storage.orm import Base


class Database:
    def __init__(self, url: str, echo: bool = False, pool_size: int = 10, max_overflow: int = 10, pool_timeout: float = 30.0) -> None:
        kwargs: dict = {"echo": echo, "future": True}
        # One in-memory SQLite connection is shared by all threads, so concurrent use is not safe;
        # callers (the agent orchestrator) serialize work when this is False.
        self.supports_concurrency = True
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
            if url in ("sqlite://", "sqlite:///:memory:"):
                kwargs["poolclass"] = StaticPool  # share one in-memory DB across threads (tests)
                self.supports_concurrency = False
        else:
            kwargs.update(pool_pre_ping=True, pool_size=pool_size, max_overflow=max_overflow, pool_timeout=pool_timeout)
        self.url = url
        self.engine: Engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):
            event.listen(self.engine, "connect", _sqlite_pragmas)
        _instrument(self.engine)
        self._factory = sessionmaker(bind=self.engine, expire_on_commit=False)

    @classmethod
    def from_settings(cls, settings) -> "Database":
        return cls(
            settings.database_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout_seconds,
        )

    def create_all(self) -> None:
        """Create tables directly (tests / quickstart). Production uses Alembic migrations."""
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def _instrument(engine: Engine) -> None:
    """Record per-statement latency by SQL operation (SELECT/INSERT/UPDATE/DELETE) in Prometheus."""
    from ontology_platform.observability import DB_QUERY_LATENCY

    @event.listens_for(engine, "before_cursor_execute")
    def _start(conn, cursor, statement, parameters, context, executemany):  # pragma: no cover - driver hook
        conn.info.setdefault("op_query_start", []).append(time.perf_counter())

    @event.listens_for(engine, "after_cursor_execute")
    def _end(conn, cursor, statement, parameters, context, executemany):  # pragma: no cover - driver hook
        started = conn.info["op_query_start"].pop()
        operation = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else "OTHER"
        DB_QUERY_LATENCY.labels(operation=operation if operation in _OPS else "OTHER").observe(time.perf_counter() - started)


_OPS = {"SELECT", "INSERT", "UPDATE", "DELETE", "WITH"}


def _sqlite_pragmas(dbapi_conn, _record) -> None:  # pragma: no cover - driver hook
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA journal_mode=WAL")
    cur.close()
