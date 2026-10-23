import os


RAG_CANCEL_TTL_SECONDS = 60 * 60


def get_rag_cancel_redis_url() -> str:
    return os.getenv(
        "RAG_REDIS_URL",
        os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0"),
    )


def get_rag_cancel_key(rag_job_id: int) -> str:
    return f"rag:cancel:{rag_job_id}"


def set_rag_cancel_signal(rag_job_id: int, *, ttl: int = RAG_CANCEL_TTL_SECONDS) -> bool:
    from redis import Redis

    client = Redis.from_url(get_rag_cancel_redis_url())
    return bool(client.set(get_rag_cancel_key(rag_job_id), "1", ex=ttl))


def is_rag_cancel_requested(rag_job_id: int, *, redis_client=None) -> bool:
    if redis_client is None:
        from redis import Redis

        redis_client = Redis.from_url(get_rag_cancel_redis_url())
    return bool(redis_client.exists(get_rag_cancel_key(rag_job_id)))


def clear_rag_cancel_signal(rag_job_id: int, *, redis_client=None) -> None:
    if redis_client is None:
        from redis import Redis

        redis_client = Redis.from_url(get_rag_cancel_redis_url())
    redis_client.delete(get_rag_cancel_key(rag_job_id))
