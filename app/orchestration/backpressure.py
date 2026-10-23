"""Queue observation without modality imports or domain policy."""
import logging
import os

logger = logging.getLogger(__name__)


def broker_redis_client():
    from redis import Redis
    return Redis.from_url(
        os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0"),
        socket_connect_timeout=2, socket_timeout=2,
    )


def queue_backpressure(queue: str, *, limit: int, client_factory=None):
    try:
        client = (client_factory or broker_redis_client)()
        depth = int(client.llen(queue))
    except Exception as exc:
        logger.warning("Unable to inspect %s queue depth; backpressure disabled: %s", queue, exc)
        return False, None, limit
    return depth >= limit, depth, limit
