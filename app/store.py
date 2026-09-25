"""A transactional local store; DATABASE_URL also supports PostgreSQL.

JSON payloads keep pilot migrations small. Indexed identity/kind/revision columns
enforce ownership, optimistic edits and delivery idempotence in either database.
"""

import json
import os
from pathlib import Path

from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    delete,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError

from .models import iso


class Conflict(Exception):
    pass


class Store:
    def __init__(self, url: str | None = None):
        Path("data").mkdir(mode=0o700, exist_ok=True)
        Path("data").chmod(0o700)
        self.engine = create_engine(
            url or os.getenv("DATABASE_URL", "sqlite:///data/pilot.sqlite"),
            **(
                {"connect_args": {"check_same_thread": False}}
                if (url or os.getenv("DATABASE_URL", "sqlite:")).startswith("sqlite")
                else {}
            ),
        )
        if self.engine.dialect.name == "sqlite":
            from sqlalchemy import event

            @event.listens_for(self.engine, "connect")
            def configure(dbapi, _):
                dbapi.execute("PRAGMA journal_mode=WAL")
                dbapi.execute("PRAGMA busy_timeout=5000")

        meta = MetaData()
        self.table = Table(
            "records",
            meta,
            Column("id", String(300), primary_key=True),
            Column("kind", String(40), index=True, nullable=False),
            Column("revision", Integer, nullable=False),
            Column("payload", Text, nullable=False),
            Column("updated_at", String(50), nullable=False),
        )
        meta.create_all(self.engine)

    def get(self, key):
        with self.engine.connect() as conn:
            row = (
                conn.execute(select(self.table).where(self.table.c.id == key))
                .mappings()
                .first()
            )
        return self.unpack(row) if row else None

    @staticmethod
    def unpack(row):
        return {
            **json.loads(row["payload"]),
            "id": row["id"],
            "revision": row["revision"],
            "updated_at": row["updated_at"],
        }

    def all(self, kind):
        with self.engine.connect() as conn:
            return [
                self.unpack(r)
                for r in conn.execute(
                    select(self.table).where(self.table.c.kind == kind)
                ).mappings()
            ]

    def put(self, key, kind, payload, expected=None, only_new=False):
        values = {
            k: v
            for k, v in payload.items()
            if k not in ("id", "revision", "updated_at", "decision")
        }
        with self.engine.begin() as conn:
            old = (
                conn.execute(select(self.table).where(self.table.c.id == key))
                .mappings()
                .first()
            )
            if old:
                if only_new:
                    return self.unpack(old)
                if expected is not None and old["revision"] != expected:
                    raise Conflict("This record changed. Refresh before saving.")
                result = conn.execute(
                    update(self.table)
                    .where(
                        self.table.c.id == key, self.table.c.revision == old["revision"]
                    )
                    .values(
                        payload=json.dumps(values),
                        revision=old["revision"] + 1,
                        updated_at=iso(),
                    )
                )
                if result.rowcount != 1:
                    raise Conflict("Concurrent edit. Refresh before saving.")
            else:
                try:
                    conn.execute(
                        self.table.insert().values(
                            id=key,
                            kind=kind,
                            payload=json.dumps(values),
                            revision=1,
                            updated_at=iso(),
                        )
                    )
                except IntegrityError as e:
                    raise Conflict("Record already exists. Refresh and retry.") from e
        return self.get(key)

    def remove(self, key):
        with self.engine.begin() as conn:
            conn.execute(delete(self.table).where(self.table.c.id == key))
