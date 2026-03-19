from fastapi.testclient import TestClient

from chat2api.main import app

client = TestClient(app)

def test_read_root():
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Chat2API"
    assert data["status"] == "running"
    assert "version" in data

def test_read_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}
