"""Start the page: `python -m webapp` (or double-click start.bat / start.sh).

Binds to 127.0.0.1 only -- this is a local tool, not a service -- and opens
the browser once the server is listening.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

HOST = "127.0.0.1"
PORT = int(os.environ.get("SANDBOX_UI_PORT", "8765"))


def _missing() -> list:
    missing = []
    for module, package in (("fastapi", "fastapi"), ("uvicorn", "uvicorn"),
                            ("multipart", "python-multipart"), ("requests", "requests")):
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    return missing


def _open_when_ready(url: str) -> None:
    for _ in range(100):
        try:
            with socket.create_connection((HOST, PORT), timeout=0.2):
                webbrowser.open(url)
                return
        except OSError:
            time.sleep(0.1)


def main() -> int:
    missing = _missing()
    if missing:
        print("Some pieces are not installed yet. Run this once:\n")
        print(f"    python -m pip install {' '.join(missing)}\n")
        return 1

    # imports resolve from the project root, whatever folder this was run from
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)
    sys.path.insert(0, str(root))

    import uvicorn

    url = f"http://{HOST}:{PORT}/"
    print(f"Sandbox account page: {url}")
    print("Leave this window open while you use the page. Close it to stop.\n")
    threading.Thread(target=_open_when_ready, args=(url,), daemon=True).start()
    uvicorn.run("webapp.app:app", host=HOST, port=PORT, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
