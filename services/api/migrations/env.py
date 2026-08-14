"""Alembic environment configuration.

This module sets up the Alembic context for both online and offline migrations.
The SQLAlchemy URL is read from the DATABASE_URL_DIRECT environment variable,
with a fallback to DATABASE_URL if the direct URL is not set.
"""
import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

# Load .env from the repo root so DATABASE_URL_DIRECT/DATABASE_URL are available
# when alembic is invoked directly (mirrors services/common/src/common/settings.py,
# which loads the same file via pydantic-settings for the running app).
load_dotenv(Path(__file__).resolve().parents[3] / ".env")

# this is the Alembic Config object, which provides
# the values of the [alembic] section of the .ini
# file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata
target_metadata = None


def get_sqlalchemy_url() -> str:
    """Get the SQLAlchemy URL from environment.

    Reads DATABASE_URL_DIRECT (preferred for migrations, targets port 5432),
    falls back to DATABASE_URL, and raises a clear error if neither is set.

    Returns:
        The SQLAlchemy URL string.

    Raises:
        RuntimeError: If neither DATABASE_URL_DIRECT nor DATABASE_URL is set.
    """
    url = os.environ.get("DATABASE_URL_DIRECT") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL_DIRECT or DATABASE_URL must be set to run migrations. "
            "DATABASE_URL_DIRECT is preferred (targets port 5432 directly); "
            "DATABASE_URL is used as a fallback. See .env.example for format."
        )
    return url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.
    """
    configuration = config.get_section(config.config_ini_section)
    configuration["sqlalchemy.url"] = get_sqlalchemy_url()

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.
    """
    configuration = config.get_section(config.config_ini_section)
    configuration["sqlalchemy.url"] = get_sqlalchemy_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
