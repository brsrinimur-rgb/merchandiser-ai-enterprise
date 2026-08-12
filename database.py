"""
Database connection setup.

Default: SQLite file (merchandiser.db) -- zero-config, works anywhere.
Production: point DATABASE_URL at Postgres, e.g.
    postgresql+psycopg2://user:password@host:5432/merchandiser
No other code needs to change -- SQLAlchemy abstracts the dialect.
"""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./merchandiser.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
