"""Alembic environment: URLs come from app.db.session (DATABASE_URL aware)."""

from alembic import context
from app.db.models import Base
from app.db.session import get_engine

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=context.config.get_main_option("sqlalchemy.url") or "sqlite:///geo_agent.db",
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,  # SQLite-friendly ALTERs
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    context.configure(
        connection=get_engine().connect(),
        target_metadata=target_metadata,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
