"""CyberRig control panel — fixed-geometry Qt UI.

    python panel.py             # panel + embedded web server (default)
    python panel.py --no-web    # panel alone

The FTDX10's serial port has exactly one owner: FTdx10.connect() does not set
exclusive=, and there is no lockfile, so a second process opening the same port
will silently interleave writes and steal reply frames rather than failing
cleanly. Running the panel and the web server in one process, sharing one
FTdx10, is what makes both safe to use at once.

`python desktop.py` still runs the legacy dock-based UI against the same rig.
"""

import argparse
import logging
import sys
import threading
import time

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("cyberrig.panel")

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
    ap = argparse.ArgumentParser(description="CyberRig fixed-geometry control panel")
    ap.add_argument("--no-web", action="store_true",
                    help="run the panel alone; do not start the embedded web server")
    ap.add_argument("--web-port", type=int, default=WEB_PORT,
                    help=f"web server port (default {WEB_PORT})")
    args = ap.parse_args()

    import os
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    from cyberrig.panel import theme as T
    from cyberrig.panel.window import PanelWindow
    from cyberrig.settings import Settings
    from cyberrig.ui_legacy.rig_adapter import RigProxy

    # Scale is a process-lifetime Qt setting. LARGE on the footer writes
    # panel_large_ui and restarts; always assign (don't setdefault) so OFF
    # can undo a previous 1.33 inherited via execv.
    large = bool(Settings().get("panel_large_ui", True))
    os.environ["QT_SCALE_FACTOR"] = T.qt_scale_factor(large)
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    log.info("UI scale %s (LARGE=%s)", os.environ["QT_SCALE_FACTOR"], large)

    app_qt = QApplication(sys.argv)

    if args.no_web:
        from cyberrig.cat.ftdx10 import FTdx10
        from cyberrig.settings import Settings
        from cyberrig.tune import RemoteTuner

        base = FTdx10()
        rig = RigProxy(base)
        owns_rig = True
        # No web server to share with, so the panel owns the remote tuner too.
        tuner = RemoteTuner(base, Settings())
        # ...but no rigctld either, so nothing can arm WSPR mode: the whole
        # mechanism hangs off JTDX talking to the radio through that server.
        wspr = None
        log.info("Panel only — this window owns the CAT lifecycle")
    else:
        import cyberrig.web.app as webapp

        rig = RigProxy(webapp.rig)
        owns_rig = False
        # One tuner for both UIs: the web TUNE button and the panel's OPS TUNE
        # must share state or their interlocks are meaningless. Same for WSPR
        # mode, which is the rigctld server's gate — the panel must show the
        # phase the thing actually in the transmit path is in, not its own.
        tuner = webapp.remote_tuner
        wspr = webapp.wspr

        server = threading.Thread(target=_serve_web, args=(args.web_port,),
                                  name="uvicorn-server", daemon=True)
        server.start()
        # Let uvicorn run its startup handler (which opens the serial port and
        # starts polling) before the window paints, so it opens populated
        # rather than showing Disconnected and correcting itself a moment later.
        time.sleep(1.5)
        log.info("Embedded web UI on port %d — server owns the CAT lifecycle",
                 args.web_port)

    win = PanelWindow(rig=rig, owns_rig=owns_rig, tuner=tuner, wspr=wspr)
    win.show()
    return app_qt.exec()


if __name__ == "__main__":
    sys.exit(main())
