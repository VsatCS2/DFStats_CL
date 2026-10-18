"""Alternate entry point so BOTH start commands work:

    uvicorn main:app --host 0.0.0.0 --port $PORT      (what railway.json sets)
    uvicorn app:app  --host 0.0.0.0 --port $PORT      (a start command copied from another service)
"""
from main import app  # noqa: F401
