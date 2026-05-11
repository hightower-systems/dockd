"""Shared Flask extensions, initialized without an app (init_app pattern)."""

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

limiter = Limiter(key_func=get_remote_address, default_limits=['120 per minute'])
