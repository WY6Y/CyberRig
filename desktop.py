"""CyberRig desktop UI — native Qt window, optionally serving the web UI too.

Two modes:

    python desktop.py            # Qt UI + embedded web server (default)
    python desktop.py --no-web   # Qt UI only

Why one process: the FTDX10's serial port has exactly one owner. `FTdx10.connect()`
does not set `exclusive=`, and there is no lockfile, so a second process opening
the same port will not fail -- it will silently interleave writes and steal reply
frames, producing wrong readings rather than a clean error. Running the Qt UI and
the web server in one process, sharing one `FTdx10`, is what makes both safe to
use at once.

`cyberrig.web.app` creates its rig as a module-level singleton and connects it
(plus starts rigctld on 4532) from its FastAPI startup handler. In web mode we
import that singleton and let the server own the lifecycle, so the Qt window is
constructed with `owns_rig=False`.

Closing the window exits the process, which also stops the embedded web server
and rigctld. Use --no-web, or run `main.py` separately, if you want the server
to outlive the window.
"""

import argparse
import logging
import sys
import threading
import time

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("cyberrig.desktop")

WEB_HOST = "0.0.0.0"
WEB_PORT = 8000


def _serve_web(port: int):
    import uvicorn
    from cyberrig.web.app import app

    # uvicorn.run() installs signal handlers, which only works on the main
    # thread; drive Server directly so it can run on a worker thread.
    config = uvicorn.Config(app, host=WEB_HOST, port=port, log_level="info")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None
    server.run()


def main() -> int:
    ap = argparse.ArgumentParser(description="CyberRig native desktop UI")
    ap.add_argument(
        "--no-web",
        action="store_true",
        help="run the Qt UI alone; do not start the embedded web server",
    )
    ap.add_argument(
        "--web-port", type=int, default=WEB_PORT, help=f"web server port (default {WEB_PORT})"
    )
    args = ap.parse_args()

    from PySide6.QtWidgets import QApplication

    from cyberrig.ui_legacy.main_window import MainWindow
    from cyberrig.ui_legacy.rig_adapter import RigProxy

    app_qt = QApplication(sys.argv)

    if args.no_web:
        from cyberrig.cat.ftdx10 import FTdx10

        rig = RigProxy(FTdx10())
        owns_rig = True
        log.info("Qt UI only — this window owns the CAT lifecycle")
    else:
        # Import the web app's module-level rig singleton and share it.
        import cyberrig.web.app as webapp

        rig = RigProxy(webapp.rig)
        owns_rig = False

        server = threading.Thread(
            target=_serve_web, args=(args.web_port,), name="uvicorn-server", daemon=True
        )
        server.start()
        # Let uvicorn run its startup handler (which opens the serial port and
        # starts polling) before the window paints, so the UI opens populated
        # rather than showing Disconnected and correcting itself a moment later.
        time.sleep(1.5)
        log.info("Embedded web UI on port %d — server owns the CAT lifecycle", args.web_port)

    win = MainWindow(rig=rig, owns_rig=owns_rig)
    win.show()
    return app_qt.exec()


if __name__ == "__main__":
    sys.exit(main())
