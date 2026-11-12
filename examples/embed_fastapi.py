"""Mount Overtone inside an existing FastAPI application.

    python examples/embed_fastapi.py            # http://127.0.0.1:8000
    curl http://127.0.0.1:8000/overtone/health

Your routes and Overtone's share one process and one port. Put your own auth
dependency or middleware in front of the mount for multi-user products.
"""

import os

if not os.environ.get("ANTHROPIC_API_KEY"):
    os.environ.setdefault("OVERTONE_MOCK", "1")

from fastapi import FastAPI

from overtone import create_engine
from overtone.api import create_app

engine = create_engine(data_dir=os.environ.get("OVERTONE_DATA_DIR", ".overtone-example"))

app = FastAPI(title="My product")


@app.get("/")
def home() -> dict[str, str]:
    return {"product": "My product", "notebooks": "/overtone/notebooks", "model": engine.capabilities().model}


# Everything Overtone serves now lives under /overtone. Pass api_key="..." to require a bearer token.
app.mount("/overtone", create_app(engine))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
