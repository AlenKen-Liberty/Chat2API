import asyncio

from tests.http_client import make_client


def test_list_models():
    async def scenario():
        async with make_client() as client:
            response = await client.get("/v1/models")
            assert response.status_code == 200
            data = response.json()
            assert data["object"] == "list"
            assert isinstance(data["data"], list)
            if len(data["data"]) > 0:
                assert "id" in data["data"][0]

    asyncio.run(scenario())


def test_get_model_exists():
    async def scenario():
        async with make_client() as client:
            response = await client.get("/v1/models")
            assert response.status_code == 200
            data = response.json()
            if len(data["data"]) > 0:
                first_model_id = data["data"][0]["id"]
                resp_single = await client.get(f"/v1/models/{first_model_id}")
                assert resp_single.status_code == 200
                assert resp_single.json()["id"] == first_model_id

    asyncio.run(scenario())


def test_get_model_not_found():
    async def scenario():
        async with make_client() as client:
            response = await client.get("/v1/models/invalid-model-name")
            assert response.status_code == 404

    asyncio.run(scenario())
