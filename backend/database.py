from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from config import settings

# Build engine; SQLite needs check_same_thread=False for multi-threaded use
_connect_args = {}
if settings.DATABASE_URL.startswith("sqlite"):
    _connect_args = {"check_same_thread": False}

engine = create_engine(
    settings.DATABASE_URL,
    connect_args=_connect_args,
    # Echo SQL to stdout – set to False in production
    echo=False,
)


# Tune SQLite for concurrent read/write access and referential integrity
if settings.DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")   # concurrent readers + one writer
        cursor.execute("PRAGMA foreign_keys=ON")    # enforce FK constraints
        cursor.execute("PRAGMA synchronous=NORMAL") # good durability / performance balance
        cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""
    pass


def get_db():
    """FastAPI dependency that yields a database session and closes it afterwards."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
