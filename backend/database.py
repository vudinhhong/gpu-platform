from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from config import settings

# Build engine; SQLite needs check_same_thread=False for multi-threaded use
_connect_args = {}
if settings.DATABASE_URL.startswith("sqlite"):
    # One writer at a time; the others wait up to `timeout` seconds for the
    # lock.  The default of 5 turned a burst of workspace starts into errors.
    _connect_args = {"check_same_thread": False, "timeout": 30}

_pool_args = {}
if settings.DATABASE_URL.startswith("sqlite"):
    # A request holds its connection for as long as it runs, and starting a
    # workspace opens a second one inside.  With SQLAlchemy's default of
    # 5 + 10, sixteen accounts starting at once used them all up: every
    # request then waited 30 s for one, and the scheduler and GPU guard
    # loops failed outright.  A SQLite connection is only a file handle, so
    # the pool is sized above FastAPI's 40 worker threads.
    _pool_args = {"pool_size": 20, "max_overflow": 80}

engine = create_engine(
    settings.DATABASE_URL,
    connect_args=_connect_args,
    **_pool_args,
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
