import logging
import os
import sys
import threading

import controller
import uvicorn
from web import app


def main():
    logging.basicConfig(
        stream=sys.stdout,
        format="%(asctime)s: %(message)s",
        datefmt="%d-%m-%YT%H:%M:%S%z",
        level=logging.INFO,
    )

    threading.Thread(
        target=controller.run_forever, name="controller", daemon=True
    ).start()

    uvicorn.run(
        app,
        host=os.environ.get("KJELLER_HOST", "0.0.0.0"),
        port=int(os.environ.get("KJELLER_PORT", "8000")),
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
