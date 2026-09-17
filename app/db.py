from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings
from .models import Base


def build_engine(settings: Settings):
    if settings.database_url.startswith("sqlite:///"):
        db_path = Path(settings.database_url.removeprefix("sqlite:///"))
        db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
    engine = create_engine(settings.database_url, pool_pre_ping=True, connect_args=connect_args)
    if settings.database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()
    return engine


class Database:
    def __init__(self, settings: Settings):
        self.engine = build_engine(settings)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)

    def initialize(self) -> None:
        Base.metadata.create_all(self.engine)
        if self.engine.url.get_backend_name() == "sqlite":
            inspector = inspect(self.engine)
            user_columns = {item["name"] for item in inspector.get_columns("platform_users")}
            scan_columns = {item["name"] for item in inspector.get_columns("scan_jobs")}
            with self.engine.begin() as connection:
                if "must_change_password" not in user_columns:
                    connection.execute(text("ALTER TABLE platform_users ADD COLUMN must_change_password BOOLEAN DEFAULT 0 NOT NULL"))
                if "collector_account" not in scan_columns:
                    connection.execute(text("ALTER TABLE scan_jobs ADD COLUMN collector_account VARCHAR(24) DEFAULT '' NOT NULL"))
                if "failover_reason" not in scan_columns:
                    connection.execute(text("ALTER TABLE scan_jobs ADD COLUMN failover_reason VARCHAR(128) DEFAULT '' NOT NULL"))
                if "created_by_user_id" not in scan_columns:
                    connection.execute(text("ALTER TABLE scan_jobs ADD COLUMN created_by_user_id INTEGER"))
                if "created_by_username" not in scan_columns:
                    connection.execute(text("ALTER TABLE scan_jobs ADD COLUMN created_by_username VARCHAR(64) DEFAULT '' NOT NULL"))
                if "tags" not in scan_columns:
                    connection.execute(text("ALTER TABLE scan_jobs ADD COLUMN tags JSON DEFAULT '[]' NOT NULL"))

                for table_name in ("commenters", "contact_evidence"):
                    columns = {item["name"] for item in inspector.get_columns(table_name)}
                    if "lead_status" not in columns:
                        connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN lead_status VARCHAR(32) DEFAULT 'new' NOT NULL"))
                    if "tags" not in columns:
                        connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN tags JSON DEFAULT '[]' NOT NULL"))
                    if "followup_note" not in columns:
                        connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN followup_note TEXT DEFAULT '' NOT NULL"))
                    if table_name == "contact_evidence":
                        if "entity_kind" not in columns:
                            connection.execute(text("ALTER TABLE contact_evidence ADD COLUMN entity_kind VARCHAR(24) DEFAULT 'unknown' NOT NULL"))
                        if "entity_title" not in columns:
                            connection.execute(text("ALTER TABLE contact_evidence ADD COLUMN entity_title VARCHAR(256) DEFAULT '' NOT NULL"))
                        if "is_contactable" not in columns:
                            connection.execute(text("ALTER TABLE contact_evidence ADD COLUMN is_contactable BOOLEAN DEFAULT 0 NOT NULL"))
                        if "classification_status" not in columns:
                            connection.execute(text("ALTER TABLE contact_evidence ADD COLUMN classification_status VARCHAR(24) DEFAULT 'not_applicable' NOT NULL"))

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self.sessions()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
