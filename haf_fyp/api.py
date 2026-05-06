"""
FastAPI overlay for haf_fyp.

Phase 5+: adds rate limiting, pg_notify force-refresh, score explainer,
and POST /v1/fyp/interests cold-start endpoint on top of PostgREST.
"""

from fastapi import FastAPI

app = FastAPI(title="haf_fyp", version="1.0.0")


@app.get("/health")
def health():
    return {"status": "ok"}
