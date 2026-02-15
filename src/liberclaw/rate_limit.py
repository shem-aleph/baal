"""Shared rate limiter for LiberClaw API."""

import os

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(
    key_func=get_remote_address,
    enabled=os.environ.get("LIBERCLAW_RATE_LIMIT_ENABLED", "true").lower() != "false",
)
