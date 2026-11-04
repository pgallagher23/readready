# SPDX-License-Identifier: AGPL-3.0-or-later
"""Celery application factory.

The Celery app is created lazily — importing this module does NOT require
Redis to be running. The connection is only made when tasks are sent or
a worker starts.

Usage (worker):
    celery -A tasks.celery_app:celery_app worker --loglevel=info --concurrency=2
"""

import logging
import os

logger = logging.getLogger(__name__)


def make_celery_app():
    """Create and configure the Celery application."""
    from celery import Celery

    from utils.secrets import get_secret

    # Redis URL: REDIS_URL env var > build from REDIS_PASSWORD > config.yaml > default
    redis_url = os.environ.get("REDIS_URL")

    if not redis_url:
        redis_password = get_secret("REDIS_PASSWORD")
        if redis_password:
            redis_url = f"redis://:{redis_password}@redis:6379/0"
        else:
            try:
                from utils.config_loader import load_config
                config = load_config()
                redis_url = config.get("queue", {}).get("redis_url", "redis://localhost:6379/0")
            except Exception:
                redis_url = "redis://localhost:6379/0"

    app = Celery(
        "pdf_accessibility",
        broker=redis_url,
        backend=redis_url,
        include=["tasks.pipeline_task"],
    )

    # Load queue config for time limits
    task_time_limit = 600
    task_soft_time_limit = 540
    result_expires = 3600
    # Recycle the prefork child after every task. A long-lived child keeps the
    # heap a large document drove it to (prod: 9.2GB idle under a 10GB cap),
    # so the next small job is OOM-killed. Docling is rebuilt per task anyway,
    # so a fresh child only costs imports. Defaulted in code because prod's
    # config.yaml is never overwritten on deploy.
    worker_max_tasks_per_child = 1

    try:
        from utils.config_loader import load_config
        config = load_config()
        queue_cfg = config.get("queue", {})
        task_time_limit = queue_cfg.get("task_time_limit", 600)
        task_soft_time_limit = queue_cfg.get("task_soft_time_limit", 540)
        result_expires = queue_cfg.get("result_expires", 3600)
        worker_max_tasks_per_child = queue_cfg.get(
            "worker_max_tasks_per_child", worker_max_tasks_per_child)
    except Exception:
        pass

    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        task_time_limit=task_time_limit,
        task_soft_time_limit=task_soft_time_limit,
        result_expires=result_expires,
        task_track_started=True,
        worker_prefetch_multiplier=1,
        task_acks_late=True,
        # A task whose worker is OOM-killed fails instead of being redelivered forever.
        task_reject_on_worker_lost=False,
        worker_max_tasks_per_child=worker_max_tasks_per_child,
    )

    return app


celery_app = make_celery_app()
