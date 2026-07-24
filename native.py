"""CyberRig native window — same FastAPI server + web UI, shown in a real
desktop window (Edge WebView2) instead of a browser tab.

Run:
    pip install -r requirements.txt
    python native.py

Closing this window does NOT stop the server — CAT, the waterfall/panadapter,
rigctld, and any remote web access you've set up (e.g. for remote SSB) all need
to keep running whether or not anyone's looking at the local window. To fully
stop CyberRig, close the python.exe process itself (Task Manager, or your usual
process-restart workflow) — same as running main.py alone.
"""

import logging
import threading
import time

import uvicorn
import webview

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

HOST = "127.0.0.1"
PORT = 8000


def _run_server():
    uvicorn.run(
        "cyberrig.web.app:app",
        host="0.0.0.0",
        port=PORT,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    # Not a daemon thread on purpose: the process must stay alive after the
    # pywebview window closes, since the server also serves remote web clients.
    server_thread = threading.Thread(target=_run_server, name="uvicorn-server")
    server_thread.start()

    # Give uvicorn a moment to bind before pointing the window at it.
    time.sleep(1.5)

    webview.create_window("CyberRig", f"http://{HOST}:{PORT}/", width=1440, height=960)
    webview.start()
    # Window closed here — server_thread keeps running in the background.
