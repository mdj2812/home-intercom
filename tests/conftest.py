"""Pytest defaults: do not hold pending /devices/hello in unit tests."""

import os

os.environ.setdefault("HOME_INTERCOM_PENDING_HELLO_WAIT", "0")
