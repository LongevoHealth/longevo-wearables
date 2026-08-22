"""Tests for Celery reliability configuration.

A worker that dies mid-task must not lose the message: the broker only gets the
ack once the task finished. See the infra spec, section 6.
"""

from app.integrations.celery.core import create_celery


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
