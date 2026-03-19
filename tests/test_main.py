import asyncio

from tests.http_client import make_client


def test_read_root():
    async def scenario():
        async with make_client() as client:
            response = await client.get("/")
            assert response.status_code == 200
            data = response.json()
            assert data["name"] == "Chat2API"
            assert data["status"] == "running"
            assert "version" in data

    asyncio.run(scenario())


def test_read_health():
    async def scenario():
        async with make_client() as client:
            response = await client.get("/health")
            assert response.status_code == 200
            assert response.json() == {"status": "healthy"}

    asyncio.run(scenario())
