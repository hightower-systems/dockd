"""Shared test fixtures for Dockd."""

import os
import sys
import tempfile
import pytest

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


@pytest.fixture(scope='session')
def app():
    """Create the Dockd app with test config and isolated temp paths."""
    tmp = tempfile.mkdtemp(prefix='dockd_test_')

    # Redirect DB and store paths to temp dir before app creation.
    from app.models import database
    database.SHIP_DB_PATH = os.path.join(tmp, 'shipping_history.db')
    database.OVERRIDE_DB_PATH = os.path.join(tmp, 'override.db')

    os.environ['SETTINGS_PATH'] = os.path.join(tmp, 'settings.json')
    os.environ['USERS_PATH'] = os.path.join(tmp, 'users.json')

    from app import create_app
    from app.config import Config

    test_app = create_app(config_class=Config)
    test_app.config['TESTING'] = True

    # Re-init databases at temp paths
    database.init_all_dbs()

    yield test_app


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
def inject_test_user(app):
    """Ensure a TestUser exists in the UsersStore for every test, with
    must_change_password cleared so blueprint tests can hit gated
    endpoints without rotating the password first."""
    store = app.users_store
    if not store.get_user('TestUser'):
        store.add_user('TestUser', 'testpass123', 'user', must_change_password=False)
    yield
    if store.get_user('TestUser'):
        try:
            store.remove_user('TestUser')
        except Exception:
            pass


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
