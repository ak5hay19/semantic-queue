import httpx
import redis as sync_redis

from tests.conftest import unique_client_id, clear_cache_entry

# Empirically verified against the real model before writing these tests
# (see phases/phase-5.md) — Phase 3 already showed assumed-similar
# phrasing can land either side of the 0.92 threshold, so these numbers
# are measured, not guessed:
#   0.9750  PARAPHRASE_A <-> PARAPHRASE_B   (comfortably above threshold)
#   0.0027  DISSIMILAR_A <-> DISSIMILAR_B   (comfortably below)
PARAPHRASE_A = "Can you tell me what the weather is like today"
PARAPHRASE_B = "Could you tell me what today weather is like"
DISSIMILAR_A = "The chef prepared a three course meal for the guests"
DISSIMILAR_B = "The submarine descended into the deep ocean trench"


def _predict(client: httpx.Client, text: str) -> httpx.Response:
    return client.post(
        "/v1/predict",
        json={"text": text},
        headers={"X-Client-ID": unique_client_id("cache")},
    )


def test_exact_match_hit(client: httpx.Client, redis_client: sync_redis.Redis):
    text = "exact match cache test sentence, pytest"
    clear_cache_entry(redis_client, text)

    first = _predict(client, text)
    assert first.status_code == 200
    assert first.json()["cache_status"] == "MISS"

    second = _predict(client, text)
    assert second.status_code == 200
    body = second.json()
    assert body["cache_status"] == "HIT"
    # Only the exact-match path can produce a HIT on byte-identical text —
    # cosine-similarity is only reached when the exact check misses.
    assert body["task_id"] is None
    assert body["result"] == first.json()["result"]


def test_cosine_similarity_hit_above_threshold(
    client: httpx.Client, redis_client: sync_redis.Redis
):
    clear_cache_entry(redis_client, PARAPHRASE_A)
    clear_cache_entry(redis_client, PARAPHRASE_B)

    first = _predict(client, PARAPHRASE_A)
    assert first.status_code == 200
    assert first.json()["cache_status"] == "MISS"

    # Different wording, never seen verbatim before — can only become a
    # HIT via the cosine-similarity path, not the exact-match path.
    second = _predict(client, PARAPHRASE_B)
    assert second.status_code == 200
    body = second.json()
    assert body["cache_status"] == "HIT"
    assert body["result"] == first.json()["result"]


def test_dissimilar_text_misses(client: httpx.Client, redis_client: sync_redis.Redis):
    clear_cache_entry(redis_client, DISSIMILAR_A)
    clear_cache_entry(redis_client, DISSIMILAR_B)

    first = _predict(client, DISSIMILAR_A)
    assert first.status_code == 200
    assert first.json()["cache_status"] == "MISS"

    second = _predict(client, DISSIMILAR_B)
    assert second.status_code == 200
    assert second.json()["cache_status"] == "MISS"
