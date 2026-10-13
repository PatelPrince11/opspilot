from fastapi.testclient import TestClient

from api.main import app


def test_health_returns_ok():
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    # `service` lets `make up` tell our app apart from another stack on the same port.
    assert response.json() == {"status": "ok", "service": "opspilot"}
