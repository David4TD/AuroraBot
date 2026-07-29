#!/usr/bin/env python3
"""Container healthcheck: exit 0 if the bot's /health endpoint reports healthy.

Kept dependency-free (stdlib only) so it works in the slim runtime image.
"""
import os
import sys
import urllib.request

PORT = os.getenv("HEALTH_PORT", "8080")
URL = f"http://127.0.0.1:{PORT}/health"

try:
    with urllib.request.urlopen(URL, timeout=5) as resp:
        sys.exit(0 if resp.status == 200 else 1)
except Exception:
    sys.exit(1)
