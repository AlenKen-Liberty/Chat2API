from fastapi.testclient import TestClient

from chat2api.main import app

client = TestClient(app)

def test_list_models():
    response = client.get("/v1/models")
    assert response.status_code == 200
    data = response.json()
    assert data["object"] == "list"
    assert isinstance(data["data"], list)
    if len(data["data"]) > 0:
        assert "id" in data["data"][0]

def test_get_model_exists():
    response = client.get("/v1/models")
    assert response.status_code == 200
    data = response.json()
    if len(data["data"]) > 0:
        first_model_id = data["data"][0]["id"]
        resp_single = client.get(f"/v1/models/{first_model_id}")
        assert resp_single.status_code == 200
        assert resp_single.json()["id"] == first_model_id

def test_get_model_not_found():
    response = client.get("/v1/models/invalid-model-name")
    assert response.status_code == 404
