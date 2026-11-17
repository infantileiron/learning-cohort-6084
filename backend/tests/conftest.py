import os, sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
DB = Path(tempfile.mkdtemp(prefix='cohort-test-')) / 'test.db'
os.environ.setdefault('DATABASE_URL', f'sqlite:///{DB}')
os.environ.setdefault('REDIS_URL', 'redis://127.0.0.1:6379/15')
os.environ.setdefault('JWT_SECRET', 'test-secret')
os.environ.setdefault('SEED_DEMO', 'false')

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def schema():
    from src.config.database import Base, engine
    import src.models.entities  # noqa: F401
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture()
def client(schema):
    from src.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def auth(client):
    from src.config.database import Session
    from src.models.entities import User
    from src.utils.security import hash_password, token
    with Session() as s:
        ids = {}
        for role in ('admin', 'analyst', 'viewer'):
            u = User(email=f'{role}@test.local', role=role, password=hash_password('Demo123!'))
            s.add(u)
            s.flush()
            ids[role] = (u.id, token(u))
        s.commit()
    return {role: {'Authorization': 'Bearer ' + tok} for role, (_, tok) in ids.items()}


@pytest.fixture()
def push_jobs(monkeypatch):
    jobs = []

    class FakeRedis:
        def rpush(self, name, value):
            jobs.append((name, value))
            return 1

        def blpop(self, *a, **k):
            return None

    import src.routes.api as api
    monkeypatch.setattr(api.redis, 'Redis', type('R', (), {'from_url': staticmethod(lambda *a, **k: FakeRedis())}))
    return jobs
