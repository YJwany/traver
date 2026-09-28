from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from travel_api.db.models import Base, create_engine_from_url


class Database:
    def __init__(self, database_url: str) -> None:
        self.engine: Engine = create_engine_from_url(database_url)
        self.session_factory = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False
        )

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    def ping(self) -> None:
        """Round-trip the database so readiness reflects reachable storage.

        ``SELECT 1`` rather than a metadata call: it must go to the server, which
        is why the health endpoint can distinguish "process is up" from "process
        cannot talk to its database".
        """
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))

    @contextmanager
    def session(self) -> Iterator[Session]:
        with self.session_factory() as session:
            yield session
