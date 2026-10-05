"""CyberRig — FTDX10 web control server.

Run:
    pip install -r requirements.txt
    python main.py

Then open http://localhost:8000 in a browser on the same LAN/VPN as this host.
"""

import logging
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


if __name__ == "__main__":
    uvicorn.run(
        "cyberrig.web.app:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )
