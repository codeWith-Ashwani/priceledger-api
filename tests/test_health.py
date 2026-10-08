from fastapi.testclient import TestClient

from app.main import app


def test_health_and_explicit_demo_status():
    response = TestClient(app).get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert "demo_mode" in response.json()
