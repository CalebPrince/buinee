"""
Entry point for cPanel's Python Selector (Phusion Passenger). Passenger
imports this file once per app process and looks for `application` at
module level - everything else lives in server.py, which this just wires up.

Not used for local dev; `python server.py` runs its own socket server
instead and never imports this file.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import server  # noqa: E402

# Runs once per Passenger worker process. Both are idempotent - init_db's
# CREATE TABLE IF NOT EXISTS is a no-op past the first run, and
# maybe_bootstrap_admin no-ops the instant any platform admin exists - so
# there's no harm if Passenger starts more than one worker.
server.db.init_db()
server.maybe_bootstrap_admin()

application = server.application
