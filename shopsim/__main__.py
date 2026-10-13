"""Entry point: ``python -m shopsim <control|catalog|orders|payments>``."""
from __future__ import annotations

import os
import sys

import uvicorn

from shopsim.settings import PORTS, settings


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SHOPSIM_SERVICE", "")
    if name not in PORTS:
        sys.exit(f"usage: python -m shopsim <{'|'.join(PORTS)}>")
    port = int(os.environ.get("SHOPSIM_PORT", PORTS[name]))
    if name == "control":
        from shopsim.control.app import create_app
        app = create_app()
    else:
        from shopsim.services import create
        app = create(name)
    uvicorn.run(app, host=settings.bind, port=port, access_log=False, log_level="warning",
                server_header=False, timeout_graceful_shutdown=3)


if __name__ == "__main__":
    main()
