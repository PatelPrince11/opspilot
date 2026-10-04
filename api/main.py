from fastapi import FastAPI

app = FastAPI(title="OpsPilot")


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness only: the process is up and serving. DB readiness is the compose healthcheck."""
    return {"status": "ok", "service": "opspilot"}
