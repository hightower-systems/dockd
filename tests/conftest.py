"""Shared test fixtures for Dockd.

Dockd's operational tables live in Postgres. The suite runs against a real
``postgres:16`` (default: the local docker on port 5434; override with
``DOCKD_TEST_DATABASE_URL``) so tests exercise real constraint/FK
enforcement, not a SQLite shim.

Isolation model -- rollback-per-test:
- The schema is created once per session via ``alembic upgrade head``.
- One shared connection is opened for the session. Every ``get_db()`` the
  app makes is routed to that single connection through a fake pool, and the
  app's ``commit()`` is neutered to a no-op, so all of a test's writes stay
  inside one open transaction.
- After each test the shared connection is rolled back, wiping the test's
  data while leaving the schema intact. No cross-test bleed.
"""

import os
import sys
import tempfile

import pytest

# ---------------------------------------------------------------------------
# Point the app at the test Postgres BEFORE any app import. python-dotenv's
# load_dotenv() (called in app.config) only sets unset vars, so seeding
# DATABASE_URL here wins over any developer .env.
# ---------------------------------------------------------------------------
TEST_DATABASE_URL = os.environ.get(
    'DOCKD_TEST_DATABASE_URL',
    'postgresql://dockd:dockd@localhost:5434/dockd_test',
)
os.environ['DATABASE_URL'] = TEST_DATABASE_URL

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Stub hardware modules before any app imports
fake_hid = type(sys)('hid')


class FakeDevice:
    def open(self, vid, pid): pass
    def set_nonblocking(self, v): pass
    def read(self, n): return []
    def close(self): pass


fake_hid.device = FakeDevice
sys.modules['hid'] = fake_hid

# Set fake credentials to prevent real API calls
os.environ.setdefault('SECRET_KEY', 'test-secret-key')
os.environ.setdefault('SHIPRUSH_TOKEN', 'fake-sr-token')
os.environ.setdefault('SHIPRUSH_ENDPOINT', 'https://fake.shiprush.test/shipment/ship')

# Defensive clear: a developer running an integration test against a
# real Sentry instance may have BACKEND / SENTRY_BASE_URL /
# DOCKD_SENTRY_TOKEN populated in their local .env. python-dotenv
# loads that file before pytest imports `app`, which would wire a
# real SentryBackend into the test fixture and break tests that
# rely on backend=None or that mock the backend in a fixture. Strip
# those values out for the duration of the test run.
for _env_key in ('BACKEND', 'SENTRY_BASE_URL', 'DOCKD_SENTRY_TOKEN',
                 'DOCKD_RETRY_PENDING_ON_BOOT', 'DOCKD_RETRY_POLL_INTERVAL'):
    # Set to empty (not pop) so dotenv's "only-set-if-unset" semantics
    # do not re-import the dev .env value during create_app().
    os.environ[_env_key] = ''


# ---------------------------------------------------------------------------
# Postgres test substrate
# ---------------------------------------------------------------------------


class _ProxyConn:
    """Wraps the one shared test connection.

    App code calls ``conn.commit()`` after every write; here that is a
    no-op so the writes stay inside the session-wide transaction that the
    per-test fixture rolls back. ``cursor()`` and ``rollback()`` pass
    through -- the latter matters for the ``get_db()`` except-path that
    clears an aborted transaction after a UniqueViolation.
    """

    def __init__(self, real):
        self._real = real

    def cursor(self, *args, **kwargs):
        return self._real.cursor(*args, **kwargs)

    def commit(self):
        pass

    def rollback(self):
        self._real.rollback()

    @property
    def closed(self):
        # get_db() checks this before returning a connection to the pool;
        # mirror the real connection so the double is faithful.
        return self._real.closed


class _FakePool:
    """Minimal ThreadedConnectionPool stand-in that always hands back the
    one shared (proxied) test connection and never really closes it."""

    def __init__(self, proxy):
        self._proxy = proxy

    def getconn(self, *args, **kwargs):
        return self._proxy

    def putconn(self, conn, *args, **kwargs):
        pass

    def closeall(self):
        pass


@pytest.fixture(scope='session', autouse=True)
def _pg_schema():
    """Create the schema once per session via the real migration chain."""
    import psycopg2
    from alembic import command
    from alembic.config import Config as AlembicConfig

    # Clean slate so `alembic upgrade head` is idempotent across re-runs.
    admin = psycopg2.connect(TEST_DATABASE_URL)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;")
    admin.close()

    cfg = AlembicConfig(os.path.join(PROJECT_ROOT, 'alembic.ini'))
    cfg.set_main_option('script_location', os.path.join(PROJECT_ROOT, 'alembic'))
    command.upgrade(cfg, 'head')
    yield


@pytest.fixture(scope='session')
def _shared_conn(_pg_schema):
    """The single connection every app query is routed to for the session."""
    import psycopg2
    from psycopg2.extras import RealDictCursor

    conn = psycopg2.connect(TEST_DATABASE_URL, cursor_factory=RealDictCursor)
    conn.autocommit = False
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture(scope='session')
def app(_shared_conn):
    """Create the Dockd app with the test DB pool wired in."""
    from app.models import database

    # Route every get_db() through the shared connection.
    database._pool = _FakePool(_ProxyConn(_shared_conn))

    from app import create_app
    from app.config import Config

    test_app = create_app(config_class=Config)  # init_pool() is a no-op: pool set
    test_app.config['TESTING'] = True

    yield test_app

    database.close_pool()


@pytest.fixture(autouse=True)
def _rollback_after_test(_shared_conn):
    """Wipe each test's writes by rolling back the shared transaction."""
    yield
    _shared_conn.rollback()


@pytest.fixture
def client(app):
    """Unauthenticated test client."""
    return app.test_client()


@pytest.fixture
def auth_client(app):
    """Pre-authenticated test client (operator role)."""
    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess['user'] = {'name': 'TestUser', 'role': 'user'}
        yield c


@pytest.fixture
def admin_client(app):
    """Pre-authenticated test client with admin role."""
    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess['user'] = {'name': 'admin', 'role': 'admin'}
        yield c


@pytest.fixture(autouse=True)
def _seed_settings(app):
    """Seed DEFAULT_SETTINGS INSIDE each test's transaction.

    Settings live in Postgres and every test is rolled back, so
    session-scoped seeding would vanish after the first test. Re-seeding
    per test keeps DEFAULT_SETTINGS populated for every test (carrier /
    shipping / printer read settings via the store). Users are Sentry's
    now, so there is nothing user-related to seed.
    """
    app.settings_store.ensure_seeded()
    yield


@pytest.fixture
def mock_sentry_auth(app):
    """Replace the Sentry identity provider with a mock for login tests.

    Dockd's /login calls app.sentry_auth.login(); tests set the mock's
    return_value or side_effect to drive success / typed failures.
    """
    from unittest.mock import MagicMock
    original = app.sentry_auth
    mock = MagicMock()
    app.sentry_auth = mock
    yield mock
    app.sentry_auth = original


@pytest.fixture(autouse=True)
def disable_rate_limiting(app):
    """Disable rate limiter during tests."""
    from app.extensions import limiter
    limiter.enabled = False
    yield
    limiter.enabled = True


@pytest.fixture
def mock_backend(app):
    """Mock the order backend on the app's shipping_service.

    Replaces the legacy mock_netsuite fixture; future Sentry backend
    tests will use this same shape.
    """
    from unittest.mock import MagicMock
    original = app.shipping_service.backend
    mock = MagicMock()
    app.shipping_service.backend = mock
    yield mock
    app.shipping_service.backend = original


@pytest.fixture
def mock_printer(app):
    """Mock the PrinterService."""
    from unittest.mock import MagicMock
    original = app.shipping_service.printer
    mock = MagicMock()
    mock.resolve_station.return_value = '2'
    app.shipping_service.printer = mock
    yield mock
    app.shipping_service.printer = original


@pytest.fixture
def mock_shiprush(app):
    """Mock the ShipRushClient with a success response."""
    from unittest.mock import MagicMock
    original = app.shipping_service.shiprush
    mock = MagicMock()
    mock.generate_label.return_value = {
        'status': 'success',
        'tracking': '1Z999AA10123456784',
        'zpl_b64': 'XlhBClRFU1QKXlha',
        'cost': 8.75,
    }
    mock.void_label.return_value = {
        'status': 'success',
        'message': 'Label voided successfully.',
    }
    app.shipping_service.shiprush = mock
    yield mock
    app.shipping_service.shiprush = original
