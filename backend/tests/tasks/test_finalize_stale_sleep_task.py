"""
Tests for finalize_stale_sleeps Celery task.

Tests the background task that finalizes sleep sessions that have been
inactive in Redis for longer than the configured threshold.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

from sqlalchemy.orm import Session

from app.integrations.celery.tasks.finalize_stale_sleep_task import finalize_stale_sleeps
from app.schemas.providers.mobile_sdk import SleepState


class TestFinalizeStaleSleepsTask:
    """Test suite for finalize_stale_sleeps task."""

    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.get_redis_client")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.SessionLocal")
    def test_finalize_stale_sleeps_no_active_users(
        self,
        mock_session_local: MagicMock,
        mock_redis_client_func: MagicMock,
        db: Session,
        mock_celery_app: MagicMock,
    ) -> None:
        """Verify task completes successfully when no active sleep sessions exist."""
        # Arrange
        mock_session_local.return_value.__enter__ = MagicMock(return_value=db)
        mock_session_local.return_value.__exit__ = MagicMock(return_value=None)

        mock_redis = MagicMock()
        mock_redis.smembers.return_value = []  # No active users
        mock_redis_client_func.return_value = mock_redis

        # Act
        result = finalize_stale_sleeps()

        # Assert
        assert result is None  # Task should complete without errors
        mock_redis.smembers.assert_called_once()

    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.finish_sleep")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.load_sleep_state")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.get_redis_client")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.SessionLocal")
    def test_finalize_stale_sleeps_with_stale_session(
        self,
        mock_session_local: MagicMock,
        mock_redis_client_func: MagicMock,
        mock_load_state: MagicMock,
        mock_finish_sleep: MagicMock,
        db: Session,
        mock_celery_app: MagicMock,
    ) -> None:
        """Verify task finalizes sleep sessions older than threshold."""
        # Arrange
        user_id = str(uuid4())
        now = datetime.now(timezone.utc)
        stale_timestamp = now - timedelta(hours=2)  # Older than 1 hour threshold

        mock_session_local.return_value.__enter__ = MagicMock(return_value=db)
        mock_session_local.return_value.__exit__ = MagicMock(return_value=None)

        mock_redis = MagicMock()
        mock_redis.smembers.return_value = [user_id]
        mock_redis_client_func.return_value = mock_redis

        # Mock sleep state with old timestamp
        mock_sleep_state = SleepState(
            uuid=str(uuid4()),
            source_name="Apple Watch",
            device_model=None,
            provider=None,
            start_time=stale_timestamp,
            end_time=stale_timestamp,
            last_start_timestamp=stale_timestamp,
            last_end_timestamp=stale_timestamp,
            in_bed_seconds=900,
            awake_seconds=300,
            light_seconds=3600,
            deep_seconds=1800,
            rem_seconds=1200,
            stages=[],
        )
        mock_load_state.return_value = mock_sleep_state
        # This test only cares about finish_sleep being invoked, not about
        # the sync-status announcement finding 2 adds on top of it.
        mock_finish_sleep.return_value = None

        # Act
        finalize_stale_sleeps()

        # Assert
        mock_load_state.assert_called_once_with(user_id)
        mock_finish_sleep.assert_called_once_with(db, user_id, mock_sleep_state)

    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.finish_sleep")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.load_sleep_state")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.get_redis_client")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.SessionLocal")
    def test_finalize_stale_sleeps_skips_recent_session(
        self,
        mock_session_local: MagicMock,
        mock_redis_client_func: MagicMock,
        mock_load_state: MagicMock,
        mock_finish_sleep: MagicMock,
        db: Session,
        mock_celery_app: MagicMock,
    ) -> None:
        """Verify task does not finalize recent sleep sessions."""
        # Arrange
        user_id = str(uuid4())
        now = datetime.now(timezone.utc)
        recent_timestamp = now - timedelta(minutes=30)  # Less than 1 hour threshold

        mock_session_local.return_value.__enter__ = MagicMock(return_value=db)
        mock_session_local.return_value.__exit__ = MagicMock(return_value=None)

        mock_redis = MagicMock()
        mock_redis.smembers.return_value = [user_id]
        mock_redis_client_func.return_value = mock_redis

        # Mock recent sleep state
        mock_sleep_state = SleepState(
            uuid=str(uuid4()),
            source_name="Apple Watch",
            device_model=None,
            provider=None,
            start_time=recent_timestamp,
            end_time=recent_timestamp,
            last_start_timestamp=recent_timestamp,
            last_end_timestamp=recent_timestamp,
            in_bed_seconds=900,
            awake_seconds=0,
            light_seconds=900,
            deep_seconds=0,
            rem_seconds=0,
            stages=[],
        )
        mock_load_state.return_value = mock_sleep_state

        # Act
        finalize_stale_sleeps()

        # Assert
        mock_load_state.assert_called_once_with(user_id)
        mock_finish_sleep.assert_not_called()  # Should not finalize recent sessions

    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.load_sleep_state")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.get_redis_client")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.SessionLocal")
    def test_finalize_stale_sleeps_handles_missing_state(
        self,
        mock_session_local: MagicMock,
        mock_redis_client_func: MagicMock,
        mock_load_state: MagicMock,
        db: Session,
        mock_celery_app: MagicMock,
    ) -> None:
        """Verify task gracefully handles users with no sleep state."""
        # Arrange
        user_id = str(uuid4())

        mock_session_local.return_value.__enter__ = MagicMock(return_value=db)
        mock_session_local.return_value.__exit__ = MagicMock(return_value=None)

        mock_redis = MagicMock()
        mock_redis.smembers.return_value = [user_id]
        mock_redis_client_func.return_value = mock_redis

        mock_load_state.return_value = None  # State not found

        # Act
        result = finalize_stale_sleeps()

        # Assert
        assert result is None
        mock_load_state.assert_called_once_with(user_id)

    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.finish_sleep")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.load_sleep_state")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.get_redis_client")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.SessionLocal")
    def test_finalize_stale_sleeps_handles_multiple_users(
        self,
        mock_session_local: MagicMock,
        mock_redis_client_func: MagicMock,
        mock_load_state: MagicMock,
        mock_finish_sleep: MagicMock,
        db: Session,
        mock_celery_app: MagicMock,
    ) -> None:
        """Verify task processes multiple users with stale sessions."""
        # Arrange
        user_id_1 = str(uuid4())
        user_id_2 = str(uuid4())
        user_id_3 = str(uuid4())
        now = datetime.now(timezone.utc)
        stale_timestamp = now - timedelta(hours=2)

        mock_session_local.return_value.__enter__ = MagicMock(return_value=db)
        mock_session_local.return_value.__exit__ = MagicMock(return_value=None)

        mock_redis = MagicMock()
        mock_redis.smembers.return_value = [user_id_1, user_id_2, user_id_3]
        mock_redis_client_func.return_value = mock_redis

        # User 1: Stale session (should finalize)
        state_1 = SleepState(
            uuid=str(uuid4()),
            source_name="Apple Watch",
            device_model=None,
            provider=None,
            start_time=stale_timestamp,
            end_time=stale_timestamp,
            last_start_timestamp=stale_timestamp,
            last_end_timestamp=stale_timestamp,
            in_bed_seconds=900,
            awake_seconds=0,
            light_seconds=3600,
            deep_seconds=1800,
            rem_seconds=1200,
            stages=[],
        )

        # User 2: No state (should skip)
        state_2 = None

        # User 3: Stale session (should finalize)
        state_3 = SleepState(
            uuid=str(uuid4()),
            source_name="iPhone",
            device_model=None,
            provider=None,
            start_time=stale_timestamp,
            end_time=stale_timestamp,
            last_start_timestamp=stale_timestamp,
            last_end_timestamp=stale_timestamp,
            in_bed_seconds=600,
            awake_seconds=600,
            light_seconds=0,
            deep_seconds=0,
            rem_seconds=0,
            stages=[],
        )

        mock_load_state.side_effect = [state_1, state_2, state_3]
        # This test only cares about finish_sleep being invoked per stale
        # user, not about the sync-status announcement finding 2 adds.
        mock_finish_sleep.return_value = None

        # Act
        finalize_stale_sleeps()

        # Assert
        assert mock_load_state.call_count == 3
        assert mock_finish_sleep.call_count == 2  # Only user 1 and 3

        mock_finish_sleep.assert_any_call(db, user_id_1, state_1)
        mock_finish_sleep.assert_any_call(db, user_id_3, state_3)

    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.log_and_capture_error")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.finish_sleep")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.load_sleep_state")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.get_redis_client")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.SessionLocal")
    def test_finalize_stale_sleeps_handles_finish_error(
        self,
        mock_session_local: MagicMock,
        mock_redis_client_func: MagicMock,
        mock_load_state: MagicMock,
        mock_finish_sleep: MagicMock,
        mock_log_error: MagicMock,
        db: Session,
        mock_celery_app: MagicMock,
    ) -> None:
        """Verify task continues processing other users if one finalization fails."""
        # Arrange
        user_id_1 = str(uuid4())
        user_id_2 = str(uuid4())
        now = datetime.now(timezone.utc)
        stale_timestamp = now - timedelta(hours=2)

        mock_session_local.return_value.__enter__ = MagicMock(return_value=db)
        mock_session_local.return_value.__exit__ = MagicMock(return_value=None)

        mock_redis = MagicMock()
        mock_redis.smembers.return_value = [user_id_1, user_id_2]
        mock_redis_client_func.return_value = mock_redis

        state_1 = SleepState(
            uuid=str(uuid4()),
            source_name="Apple Watch",
            device_model=None,
            provider=None,
            start_time=stale_timestamp,
            end_time=stale_timestamp,
            last_start_timestamp=stale_timestamp,
            last_end_timestamp=stale_timestamp,
            in_bed_seconds=900,
            awake_seconds=0,
            light_seconds=3600,
            deep_seconds=0,
            rem_seconds=0,
            stages=[],
        )

        state_2 = SleepState(
            uuid=str(uuid4()),
            source_name="iPhone",
            device_model=None,
            provider=None,
            start_time=stale_timestamp,
            end_time=stale_timestamp,
            last_start_timestamp=stale_timestamp,
            last_end_timestamp=stale_timestamp,
            in_bed_seconds=0,
            awake_seconds=0,
            light_seconds=0,
            deep_seconds=0,
            rem_seconds=1800,
            stages=[],
        )

        mock_load_state.side_effect = [state_1, state_2]

        # User 1 finalization fails
        mock_finish_sleep.side_effect = [Exception("Database error"), None]

        # Act
        finalize_stale_sleeps()

        # Assert
        assert mock_finish_sleep.call_count == 2
        mock_log_error.assert_called_once()  # Error should be logged for user 1

        # Verify user 2 was still processed despite user 1 error
        mock_finish_sleep.assert_any_call(db, user_id_2, state_2)

    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.load_sleep_state")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.get_redis_client")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.SessionLocal")
    def test_finalize_stale_sleeps_handles_malformed_state(
        self,
        mock_session_local: MagicMock,
        mock_redis_client_func: MagicMock,
        mock_load_state: MagicMock,
        db: Session,
        mock_celery_app: MagicMock,
    ) -> None:
        """Verify task skips users whose sleep state cannot be parsed from Redis."""
        # Arrange
        user_id = str(uuid4())

        mock_session_local.return_value.__enter__ = MagicMock(return_value=db)
        mock_session_local.return_value.__exit__ = MagicMock(return_value=None)

        mock_redis = MagicMock()
        mock_redis.smembers.return_value = [user_id]
        mock_redis_client_func.return_value = mock_redis

        # Simulate load_sleep_state returning None (as it does when state is malformed/unparseable)
        mock_load_state.return_value = None

        # Act - should not crash
        result = finalize_stale_sleeps()

        # Assert
        assert result is None  # Task completes and skips the malformed user
        mock_load_state.assert_called_once_with(user_id)

    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.completed")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.finish_sleep")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.load_sleep_state")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.get_redis_client")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.SessionLocal")
    def test_finalize_stale_sleeps_announces_the_finalized_record(
        self,
        mock_session_local: MagicMock,
        mock_redis_client_func: MagicMock,
        mock_load_state: MagicMock,
        mock_finish_sleep: MagicMock,
        mock_completed: MagicMock,
        db: Session,
        mock_celery_app: MagicMock,
    ) -> None:
        """finding 2: a session written out of band by this task must be
        announced with a sync-status COMPLETED event carrying that record's
        own window — otherwise a consumer that already pulled the event
        window (while the session was still invisible in Redis) never finds
        out the data later became available."""
        from app.schemas.sync_status import SyncSource, SyncStatus

        user_id = str(uuid4())
        now = datetime.now(timezone.utc)
        stale_timestamp = now - timedelta(hours=2)

        mock_session_local.return_value.__enter__ = MagicMock(return_value=db)
        mock_session_local.return_value.__exit__ = MagicMock(return_value=None)

        mock_redis = MagicMock()
        mock_redis.smembers.return_value = [user_id]
        mock_redis_client_func.return_value = mock_redis

        mock_sleep_state = SleepState(
            uuid=str(uuid4()),
            source_name="Apple Watch",
            device_model=None,
            provider="apple",
            start_time=stale_timestamp,
            end_time=stale_timestamp,
            last_start_timestamp=stale_timestamp,
            last_end_timestamp=stale_timestamp,
            in_bed_seconds=900,
            awake_seconds=300,
            light_seconds=3600,
            deep_seconds=1800,
            rem_seconds=1200,
            stages=[],
        )
        mock_load_state.return_value = mock_sleep_state

        finalized_record = MagicMock()
        finalized_record.start_datetime = stale_timestamp - timedelta(hours=8)
        finalized_record.end_datetime = stale_timestamp
        finalized_record.provider = "apple"
        mock_finish_sleep.return_value = finalized_record

        # Act
        finalize_stale_sleeps()

        # Assert — same metadata shape process_sdk_upload_task produces
        # (records_saved / workouts_saved / sleep_saved / types / window_*),
        # since that is what the consumer's parser was built against.
        mock_completed.assert_called_once()
        call = mock_completed.call_args
        assert call.args[0] == user_id
        assert call.args[1] == "apple"
        assert call.args[2] == SyncSource.SDK
        assert call.kwargs["status"] == SyncStatus.SUCCESS
        metadata = call.kwargs["metadata"]
        assert metadata["sleep_saved"] == 1
        assert metadata["records_saved"] == 0
        assert metadata["workouts_saved"] == 0
        assert metadata["types"] == []
        assert metadata["window_start"] == finalized_record.start_datetime
        assert metadata["window_end"] == finalized_record.end_datetime

    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.completed")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.finish_sleep")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.load_sleep_state")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.get_redis_client")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.SessionLocal")
    def test_finalize_stale_sleeps_does_not_announce_a_failed_write(
        self,
        mock_session_local: MagicMock,
        mock_redis_client_func: MagicMock,
        mock_load_state: MagicMock,
        mock_finish_sleep: MagicMock,
        mock_completed: MagicMock,
        db: Session,
        mock_celery_app: MagicMock,
    ) -> None:
        """finish_sleep returns None when it swallowed a write failure; that
        must never be announced as a completed sync."""
        user_id = str(uuid4())
        now = datetime.now(timezone.utc)
        stale_timestamp = now - timedelta(hours=2)

        mock_session_local.return_value.__enter__ = MagicMock(return_value=db)
        mock_session_local.return_value.__exit__ = MagicMock(return_value=None)

        mock_redis = MagicMock()
        mock_redis.smembers.return_value = [user_id]
        mock_redis_client_func.return_value = mock_redis

        mock_sleep_state = SleepState(
            uuid=str(uuid4()),
            source_name="Apple Watch",
            device_model=None,
            provider="apple",
            start_time=stale_timestamp,
            end_time=stale_timestamp,
            last_start_timestamp=stale_timestamp,
            last_end_timestamp=stale_timestamp,
            in_bed_seconds=900,
            awake_seconds=300,
            light_seconds=3600,
            deep_seconds=1800,
            rem_seconds=1200,
            stages=[],
        )
        mock_load_state.return_value = mock_sleep_state
        mock_finish_sleep.return_value = None

        # Act
        finalize_stale_sleeps()

        # Assert
        mock_completed.assert_not_called()

    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.log_and_capture_error")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.completed")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.finish_sleep")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.load_sleep_state")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.get_redis_client")
    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.SessionLocal")
    def test_finalize_stale_sleeps_survives_a_broken_announcement(
        self,
        mock_session_local: MagicMock,
        mock_redis_client_func: MagicMock,
        mock_load_state: MagicMock,
        mock_finish_sleep: MagicMock,
        mock_completed: MagicMock,
        mock_log_error: MagicMock,
        db: Session,
        mock_celery_app: MagicMock,
    ) -> None:
        """A failure while emitting the sync-status event must never break
        finalization: the sleep row is already safely written by the time
        this runs, and a broken notification must not undo or block that,
        nor stop the next user in the loop from being processed."""
        user_id_1 = str(uuid4())
        user_id_2 = str(uuid4())
        now = datetime.now(timezone.utc)
        stale_timestamp = now - timedelta(hours=2)

        mock_session_local.return_value.__enter__ = MagicMock(return_value=db)
        mock_session_local.return_value.__exit__ = MagicMock(return_value=None)

        mock_redis = MagicMock()
        mock_redis.smembers.return_value = [user_id_1, user_id_2]
        mock_redis_client_func.return_value = mock_redis

        def make_state() -> SleepState:
            return SleepState(
                uuid=str(uuid4()),
                source_name="Apple Watch",
                device_model=None,
                provider="apple",
                start_time=stale_timestamp,
                end_time=stale_timestamp,
                last_start_timestamp=stale_timestamp,
                last_end_timestamp=stale_timestamp,
                in_bed_seconds=900,
                awake_seconds=300,
                light_seconds=3600,
                deep_seconds=1800,
                rem_seconds=1200,
                stages=[],
            )

        mock_load_state.side_effect = [make_state(), make_state()]

        finalized_record_1 = MagicMock()
        finalized_record_1.start_datetime = stale_timestamp - timedelta(hours=8)
        finalized_record_1.end_datetime = stale_timestamp
        finalized_record_1.provider = "apple"

        finalized_record_2 = MagicMock()
        finalized_record_2.start_datetime = stale_timestamp - timedelta(hours=8)
        finalized_record_2.end_datetime = stale_timestamp
        finalized_record_2.provider = "apple"

        mock_finish_sleep.side_effect = [finalized_record_1, finalized_record_2]
        mock_completed.side_effect = [RuntimeError("redis is down"), None]

        # Act - should not crash despite the first announcement failing
        result = finalize_stale_sleeps()

        # Assert
        assert result is None
        assert mock_finish_sleep.call_count == 2  # both users still got finalized
        assert mock_completed.call_count == 2  # both announcements were attempted
        mock_log_error.assert_called_once()  # the broken announcement was logged, not raised
