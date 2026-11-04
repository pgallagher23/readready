# SPDX-License-Identifier: AGPL-3.0-or-later
"""Simple queue monitoring for the PDF Accessibility Pipeline.

Provides queue depth, active task count, and estimated wait time
for display in the Gradio UI.
"""

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


def get_queue_status() -> Dict[str, Any]:
    """Return current queue metrics.

    Returns:
        {
            "queue_length": int,          # pending tasks in queue
            "active_tasks": int,          # currently executing tasks
            "estimated_wait_seconds": float,
            "celery_available": bool,
        }
    """
    try:
        from tasks.task_runner import is_celery_available
        if not is_celery_available():
            return {
                "queue_length": 0,
                "active_tasks": 0,
                "estimated_wait_seconds": 0,
                "celery_available": False,
            }
    except Exception:
        return {
            "queue_length": 0,
            "active_tasks": 0,
            "estimated_wait_seconds": 0,
            "celery_available": False,
        }

    queue_length = 0
    active_tasks = 0

    try:
        from tasks.celery_app import celery_app

        # Get queue length from Redis
        broker_url = celery_app.conf.broker_url
        try:
            import redis
            conn = redis.Redis.from_url(broker_url, socket_timeout=2)
            # Celery default queue name
            queue_length = conn.llen("celery") or 0
            conn.close()
        except Exception:
            pass

        # Get active tasks from worker inspection
        try:
            inspector = celery_app.control.inspect(timeout=2)
            active = inspector.active()
            if active:
                for worker_tasks in active.values():
                    active_tasks += len(worker_tasks)
        except Exception:
            pass

    except Exception as e:
        logger.debug("Queue status check failed: %s", e)

    # Estimate wait time (rough: 45 seconds per document)
    avg_per_doc = 45
    try:
        from utils.config_loader import get_config
        cfg = get_config()
        avg_per_doc = cfg.get("queue", {}).get("avg_seconds_per_doc", 45)
    except Exception:
        pass

    # Count total workers
    worker_count = 1
    try:
        from tasks.celery_app import celery_app
        inspector = celery_app.control.inspect(timeout=2)
        stats = inspector.stats()
        if stats:
            worker_count = max(1, len(stats))
    except Exception:
        pass

    estimated_wait = (queue_length * avg_per_doc) / worker_count

    return {
        "queue_length": queue_length,
        "active_tasks": active_tasks,
        "estimated_wait_seconds": round(estimated_wait, 0),
        "celery_available": True,
    }
