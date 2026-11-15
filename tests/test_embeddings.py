from __future__ import annotations


def test_embeddings_single_input(test_app):
    client, _ = test_app
    resp = client.post(
        "/v1/embeddings",
        json={"model": "text-embedding-3-small", "input": "Hello world from SentinelGate"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "list"
    assert len(data["data"]) == 1
    assert data["data"][0]["object"] == "embedding"
    assert isinstance(data["data"][0]["embedding"], list)
    assert len(data["data"][0]["embedding"]) > 0
    assert "usage" in data
    assert "X-Request-ID" in resp.headers


def test_embeddings_batch_input(test_app):
    client, _ = test_app
    resp = client.post(
        "/v1/embeddings",
        json={
            "model": "text-embedding-3-small",
            "input": ["Sentence one", "Sentence two", "Sentence three"],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["data"]) == 3
    assert data["data"][1]["index"] == 1
    assert data["usage"]["total_tokens"] > 0
