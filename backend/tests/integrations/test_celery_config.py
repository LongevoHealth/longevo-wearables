"""Tests for Celery reliability configuration.

A worker that dies mid-task must not lose the message: the broker only gets the
ack once the task finished. See the infra spec, section 6.
"""

from app.integrations.celery.core import create_celery
from app.integrations.celery.tasks.process_s3_sdk_upload_task import process_s3_sdk_upload
from app.integrations.celery.tasks.process_sdk_upload_task import process_sdk_upload


def test_tasks_are_acknowledged_after_completion() -> None:
    conf = create_celery().conf

    assert conf.task_acks_late is True
    assert conf.worker_prefetch_multiplier == 1


def test_broker_keepalive_options_are_preserved() -> None:
    """Regression guard: the keepalive settings that fixed a stuck consumer upstream
    must survive any change to this config block."""
    options = create_celery().conf.broker_transport_options

    assert options["socket_keepalive"] is True
    assert options["health_check_interval"] == 30


def test_sdk_upload_tasks_are_bounded_below_the_visibility_timeout() -> None:
    """With `task_acks_late`, a task still running when the broker's visibility timeout
    expires has its message redelivered to a second worker, which imports the same batch
    concurrently. The SDK tasks must therefore cap themselves below that window."""
    visibility_timeout = create_celery().conf.broker_transport_options["visibility_timeout"]

    for task in (process_sdk_upload, process_s3_sdk_upload):
        assert task.soft_time_limit < task.time_limit < visibility_timeout


def test_sdk_upload_tasks_run_on_the_bulk_queue() -> None:
    """Both SDK import paths must land on `sdk_sync`, the queue the dedicated bulk
    worker serves (infra spec, sections 5 and 8). A task left on `default` would
    compete with interactive syncs on the general worker."""
    celery_app = create_celery()

    for task in (process_sdk_upload, process_s3_sdk_upload):
        assert celery_app.tasks[task.name].queue == "sdk_sync"
