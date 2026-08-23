"""Run the Family Hub web app.

    python serve.py            # http://0.0.0.0:8000
    HUB_PORT=8080 python serve.py
"""

import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "hub.app:app",
        host=os.environ.get("HUB_HOST", "0.0.0.0"),
        port=int(os.environ.get("HUB_PORT", "8000")),
    )
