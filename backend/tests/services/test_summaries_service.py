"""Tests for SummariesService."""

from datetime import date, datetime, timezone
from logging import getLogger
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.schemas.enums import ProviderName
from app.services.summaries_service import SummariesService
from tests.factories import (
    DataPointSeriesFactory,
    DataSourceFactory,
    EventRecordFactory,
    PersonalRecordFactory,
    SeriesTypeDefinitionFactory,
    SleepDetailsFactory,
    UserFactory,
)


@pytest.fixture
def service() -> SummariesService:
    return SummariesService(log=getLogger(__name__))


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


# ---------------------------------------------------------------------------
# _filter_by_priority
# ---------------------------------------------------------------------------


class TestFilterByPriority:
    def test_returns_empty_for_empty_input(self, db: Session, service: SummariesService) -> None:
        result = service._filter_by_priority(db, uuid4(), [])
        assert result == []

    def test_single_entry_passes_through(self, db: Session, service: SummariesService) -> None:
        entry = {"activity_date": date(2026, 1, 1), "source": "garmin", "device_model": None}
        result = service._filter_by_priority(db, uuid4(), [entry])
        assert result == [entry]

    def test_picks_one_entry_per_date(self, db: Session, service: SummariesService) -> None:
        entries = [
            {"activity_date": date(2026, 1, 1), "source": "garmin", "device_model": None},
            {"activity_date": date(2026, 1, 1), "source": "apple_health_sdk", "device_model": None},
            {"activity_date": date(2026, 1, 2), "source": "garmin", "device_model": None},
        ]
        result = service._filter_by_priority(db, uuid4(), entries)
        assert len(result) == 2
        dates = {r["activity_date"] for r in result}
        assert dates == {date(2026, 1, 1), date(2026, 1, 2)}

    def test_uses_sleep_date_key(self, db: Session, service: SummariesService) -> None:
        entries = [
            {"sleep_date": date(2026, 1, 1), "source": "garmin", "device_model": None},
            {"sleep_date": date(2026, 1, 1), "source": "oura", "device_model": None},
        ]
        result = service._filter_by_priority(db, uuid4(), entries, date_key="sleep_date")
        assert len(result) == 1


# ---------------------------------------------------------------------------
# _collapse_activity_sources
# ---------------------------------------------------------------------------


def _activity(source: str, **metrics: Any) -> dict:
    """Fila de agregado diario tal como la devuelve el repositorio.

    Los campos ausentes toman el valor con que la query los rellena cuando la
    fuente no escribió esa métrica: 0 para los sumables, None para el resto.
    """
    base = {
        "activity_date": date(2026, 9, 15),
        "provider": "apple",
        "source": source,
        "device_model": "iPhone17,2",
        "device_type": "phone",
        "steps_sum": 0,
        "active_energy_sum": 0.0,
        "basal_energy_sum": 0.0,
        "hr_avg": None,
        "hr_max": None,
        "hr_min": None,
        "distance_sum": None,
        "flights_climbed_sum": None,
        "active_time_minutes": None,
    }
    base.update(metrics)
    return base


class TestCollapseActivitySources:
    """Un día con varias fuentes de HealthKit tiene que dar un día completo.

    Caso real de QA: el 14/09 el iPhone empezó a grabar frecuencia cardíaca de
    un accesorio BLE, que aparece como una fuente propia ("Dispositivo
    Bluetooth") sin un solo paso. Las tres fuentes viven en el mismo teléfono,
    así que empatan en provider y en tipo de dispositivo, y quedarse con una
    sola devolvía el día en 0 pasos.
    """

    def test_merges_sources_that_tie_on_priority(self, db: Session, service: SummariesService) -> None:
        entries = [
            _activity("Dispositivo Bluetooth", hr_avg=72, hr_max=118, hr_min=54),
            _activity("iPhone de Manuel", steps_sum=644, active_energy_sum=37.0),
            _activity("Zepp", steps_sum=488, active_energy_sum=41.0, hr_avg=80, hr_max=131, hr_min=49),
        ]

        result = service._collapse_activity_sources(db, uuid4(), entries)

        assert len(result) == 1
        day = result[0]
        # Máximo y no suma: el teléfono y el reloj cuentan los mismos pasos.
        assert day["steps_sum"] == 644
        assert day["active_energy_sum"] == 41.0
        assert day["hr_max"] == 131
        assert day["hr_min"] == 49

    def test_a_source_without_steps_no_longer_zeroes_the_day(self, db: Session, service: SummariesService) -> None:
        """La regresión exacta: la fila sin pasos ganaba el desempate."""
        entries = [
            _activity("Dispositivo Bluetooth", hr_avg=72),
            _activity("iPhone de Manuel", steps_sum=644),
        ]

        result = service._collapse_activity_sources(db, uuid4(), entries)

        assert result[0]["steps_sum"] == 644
        assert result[0]["hr_avg"] == 72

    def test_is_deterministic_regardless_of_row_order(self, db: Session, service: SummariesService) -> None:
        """La query sólo ordena por fecha, así que el orden dentro del día no está definido."""
        entries = [
            _activity("Dispositivo Bluetooth", hr_avg=72),
            _activity("iPhone de Manuel", steps_sum=644),
            _activity("Zepp", steps_sum=488, hr_avg=80),
        ]

        forward = service._collapse_activity_sources(db, uuid4(), list(entries))
        backward = service._collapse_activity_sources(db, uuid4(), list(reversed(entries)))

        assert forward == backward

    def test_does_not_mix_across_providers(self, db: Session, service: SummariesService) -> None:
        """Fusionar es para fuentes del mismo dispositivo, no entre proveedores.

        Garmin y Apple midiendo el mismo día son dos relatos del mismo cuerpo:
        combinarlos mezclaría dos mediciones distintas. Ahí sigue ganando el de
        mayor prioridad, como hasta ahora.
        """
        entries = [
            _activity("Garmin", provider="garmin", device_model="fenix7", device_type="watch", steps_sum=9000),
            _activity("iPhone de Manuel", steps_sum=644),
        ]

        result = service._collapse_activity_sources(db, uuid4(), entries)

        assert len(result) == 1
        assert result[0]["steps_sum"] in (9000, 644)
        # El ganador conserva su identidad: no hereda métricas del otro proveedor.
        winner = result[0]
        assert (winner["steps_sum"] == 9000) == (winner["provider"] == "garmin")

    def test_keeps_one_row_per_date(self, db: Session, service: SummariesService) -> None:
        entries = [
            _activity("iPhone de Manuel", steps_sum=644),
            _activity("Zepp", steps_sum=488),
            _activity("iPhone de Manuel", activity_date=date(2026, 9, 14), steps_sum=100),
        ]

        result = service._collapse_activity_sources(db, uuid4(), entries)

        assert {r["activity_date"] for r in result} == {date(2026, 9, 14), date(2026, 9, 15)}


# ---------------------------------------------------------------------------
# _get_user_max_hr
# ---------------------------------------------------------------------------


class TestGetUserMaxHr:
    def test_falls_back_to_default_when_no_user(self, db: Session, service: SummariesService) -> None:
        result = service._get_user_max_hr(db, uuid4(), datetime(2026, 1, 1, tzinfo=timezone.utc))
        assert result == 190

    def test_falls_back_to_default_when_no_birth_date(self, db: Session, service: SummariesService) -> None:
        user = UserFactory()
        PersonalRecordFactory(user=user, birth_date=None)
        result = service._get_user_max_hr(db, user.id, datetime(2026, 1, 1, tzinfo=timezone.utc))
        assert result == 190

    def test_calculates_from_birth_date(self, db: Session, service: SummariesService) -> None:
        user = UserFactory()
        PersonalRecordFactory(user=user, birth_date=date(1990, 6, 1))
        ref = datetime(2026, 6, 26, tzinfo=timezone.utc)  # age = 36
        result = service._get_user_max_hr(db, user.id, ref)
        assert result == 220 - 36

    def test_adjusts_when_birthday_not_yet_this_year(self, db: Session, service: SummariesService) -> None:
        user = UserFactory()
        PersonalRecordFactory(user=user, birth_date=date(1990, 12, 31))
        ref = datetime(2026, 6, 26, tzinfo=timezone.utc)  # birthday hasn't happened yet -> age 35
        result = service._get_user_max_hr(db, user.id, ref)
        assert result == 220 - 35


# ---------------------------------------------------------------------------
# get_sleep_summaries
# ---------------------------------------------------------------------------


class TestGetSleepSummaries:
    def _make_sleep_record(self, user: Any, start: str, end: str) -> Any:
        ds = DataSourceFactory(user=user, provider=ProviderName.GARMIN, source="garmin")
        return EventRecordFactory(
            data_source=ds,
            category="sleep",
            type="sleep",
            start_datetime=_dt(start),
            end_datetime=_dt(end),
            duration_seconds=int((_dt(end) - _dt(start)).total_seconds()),
            zone_offset="+00:00",
        )

    def test_sleep_date_uses_the_users_timezone_when_the_record_has_none(
        self, db: Session, service: SummariesService
    ) -> None:
        """Una noche que termina 02:00Z es la noche del día anterior en UTC-3.

        La fecha de sueño se toma del fin de la sesión. Sin zona en el registro
        el fork asumía UTC, y para un usuario en UTC-3 una noche que termina a
        las 02:00Z (23:00 locales del día anterior) caía en el día equivocado.
        """
        user = UserFactory(timezone_offset="-03:00")
        ds = DataSourceFactory(user=user, provider="apple")
        record = EventRecordFactory(
            data_source=ds,
            category="sleep",
            type="sleep",
            start_datetime=_dt("2026-01-01T18:00:00+00:00"),
            end_datetime=_dt("2026-01-02T02:00:00+00:00"),
            duration_seconds=8 * 3600,
            zone_offset=None,
        )
        SleepDetailsFactory(event_record=record)

        result = service.get_sleep_summaries(
            db, user.id, _dt("2025-12-31T00:00:00+00:00"), _dt("2026-01-04T00:00:00+00:00"), cursor=None, limit=10
        )

        # 02:00Z del 2 son las 23:00 del 1 en UTC-3: el sueño es del día 1.
        assert [item.date.isoformat() for item in result.data] == ["2026-01-01"]

    def test_returns_empty_when_no_data(self, db: Session, service: SummariesService) -> None:
        user = UserFactory()
        result = service.get_sleep_summaries(
            db,
            user.id,
            _dt("2026-01-01T00:00:00+00:00"),
            _dt("2026-01-07T00:00:00+00:00"),
            cursor=None,
            limit=10,
        )
        assert result.data == []
        assert result.pagination.has_more is False

    def test_returns_sleep_record(self, db: Session, service: SummariesService) -> None:
        user = UserFactory()
        record = self._make_sleep_record(user, "2026-01-01T23:00:00+00:00", "2026-01-02T07:00:00+00:00")
        SleepDetailsFactory(event_record=record)

        result = service.get_sleep_summaries(
            db,
            user.id,
            _dt("2026-01-01T00:00:00+00:00"),
            _dt("2026-01-03T00:00:00+00:00"),
            cursor=None,
            limit=10,
        )
        assert len(result.data) == 1
        summary = result.data[0]
        assert summary.duration_minutes == 8 * 60
        assert summary.source.provider == "garmin"

    def test_does_not_return_other_users_data(self, db: Session, service: SummariesService) -> None:
        user_a = UserFactory()
        user_b = UserFactory()
        self._make_sleep_record(user_b, "2026-01-01T23:00:00+00:00", "2026-01-02T07:00:00+00:00")

        result = service.get_sleep_summaries(
            db,
            user_a.id,
            _dt("2026-01-01T00:00:00+00:00"),
            _dt("2026-01-03T00:00:00+00:00"),
            cursor=None,
            limit=10,
        )
        assert result.data == []

    def test_physio_averages_within_sleep_window(self, db: Session, service: SummariesService) -> None:
        """avg_heart_rate_bpm/avg_hrv_sdnn_ms are computed from data_point_series
        samples within [min_start_time, max_end_time), independently per series
        type, and samples outside the window are excluded."""
        user = UserFactory()
        ds = DataSourceFactory(user=user, provider=ProviderName.GARMIN, source="garmin")
        record = EventRecordFactory(
            data_source=ds,
            category="sleep",
            type="sleep",
            start_datetime=_dt("2026-01-01T23:00:00+00:00"),
            end_datetime=_dt("2026-01-02T07:00:00+00:00"),
            duration_seconds=8 * 3600,
            zone_offset="+00:00",
        )
        SleepDetailsFactory(event_record=record)

        hr_type = SeriesTypeDefinitionFactory.get_or_create_heart_rate()
        hrv_type = SeriesTypeDefinitionFactory.get_or_create_heart_rate_variability_sdnn()

        for i, val in enumerate([50, 60, 70]):
            DataPointSeriesFactory(
                data_source=ds,
                series_type=hr_type,
                value=val,
                recorded_at=_dt(f"2026-01-02T0{i}:00:00+00:00"),
            )
        DataPointSeriesFactory(
            data_source=ds,
            series_type=hrv_type,
            value=45,
            recorded_at=_dt("2026-01-02T02:00:00+00:00"),
        )
        # Outside the sleep window - must not affect the average
        DataPointSeriesFactory(
            data_source=ds,
            series_type=hr_type,
            value=200,
            recorded_at=_dt("2026-01-02T12:00:00+00:00"),
        )

        result = service.get_sleep_summaries(
            db,
            user.id,
            _dt("2026-01-01T00:00:00+00:00"),
            _dt("2026-01-03T00:00:00+00:00"),
            cursor=None,
            limit=10,
        )
        assert len(result.data) == 1
        summary = result.data[0]
        assert summary.avg_heart_rate_bpm == 60
        assert summary.avg_hrv_sdnn_ms == 45

    def test_physio_averages_none_without_physio_data(self, db: Session, service: SummariesService) -> None:
        user = UserFactory()
        record = self._make_sleep_record(user, "2026-01-01T23:00:00+00:00", "2026-01-02T07:00:00+00:00")
        SleepDetailsFactory(event_record=record)

        result = service.get_sleep_summaries(
            db,
            user.id,
            _dt("2026-01-01T00:00:00+00:00"),
            _dt("2026-01-03T00:00:00+00:00"),
            cursor=None,
            limit=10,
        )
        assert len(result.data) == 1
        summary = result.data[0]
        assert summary.avg_heart_rate_bpm is None
        assert summary.avg_hrv_sdnn_ms is None
        assert summary.avg_hrv_rmssd_ms is None
        assert summary.avg_respiratory_rate is None
        assert summary.avg_spo2_percent is None

    def test_has_more_flag_and_pagination(self, db: Session, service: SummariesService) -> None:
        user = UserFactory()
        ds = DataSourceFactory(user=user, provider=ProviderName.GARMIN, source="garmin")
        for day in range(1, 6):
            EventRecordFactory(
                data_source=ds,
                category="sleep",
                type="sleep",
                start_datetime=_dt(f"2026-01-{day:02d}T23:00:00+00:00"),
                end_datetime=_dt(f"2026-01-{day + 1:02d}T07:00:00+00:00"),
                duration_seconds=8 * 3600,
                zone_offset="+00:00",
            )

        result = service.get_sleep_summaries(
            db,
            user.id,
            _dt("2026-01-01T00:00:00+00:00"),
            _dt("2026-01-10T00:00:00+00:00"),
            cursor=None,
            limit=3,
        )
        assert len(result.data) == 3
        assert result.pagination.has_more is True
        assert result.pagination.next_cursor is not None


# ---------------------------------------------------------------------------
# get_recovery_summaries
# ---------------------------------------------------------------------------


class TestGetRecoverySummaries:
    def test_rmssd_input_is_exposed_as_rmssd_not_sdnn(
        self, service: SummariesService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        row = {
            "recovery_date": date(2026, 1, 2),
            "provider": "whoop",
            "source": "whoop",
            "device_model": "WHOOP",
            "device_type": "band",
            "record_id": uuid4(),
            "recorded_at": _dt("2026-01-02T00:00:00+00:00"),
            "recovery_score": 74,
            "resting_heart_rate": 51,
            "hrv_rmssd_milli": 63.2,
            "spo2_percentage": 98.4,
        }
        monkeypatch.setattr(service.health_score_repo, "get_recovery_summaries", lambda *_: [row])
        monkeypatch.setattr(service, "_filter_by_priority", lambda *_args, **_kwargs: [row])

        result = service.get_recovery_summaries(
            db_session=None,
            user_id=uuid4(),
            start_date=_dt("2026-01-01T00:00:00+00:00"),
            end_date=_dt("2026-01-03T00:00:00+00:00"),
            cursor=None,
            limit=10,
        )

        summary = result.data[0]
        assert summary.avg_hrv_sdnn_ms is None
        assert summary.avg_hrv_rmssd_ms == 63.2


# ---------------------------------------------------------------------------
# get_activity_summaries
# ---------------------------------------------------------------------------


class TestGetActivitySummaries:
    def test_returns_empty_when_no_data(self, db: Session, service: SummariesService) -> None:
        user = UserFactory()
        result = service.get_activity_summaries(
            db,
            user.id,
            _dt("2026-01-01T00:00:00+00:00"),
            _dt("2026-01-07T00:00:00+00:00"),
            cursor=None,
            limit=10,
        )
        assert result.data == []

    def test_aggregates_steps_for_user(self, db: Session, service: SummariesService) -> None:
        user = UserFactory()
        ds = DataSourceFactory(user=user, provider=ProviderName.APPLE, source="apple_health_sdk")
        steps_type = SeriesTypeDefinitionFactory.get_or_create_steps()

        for i in range(3):
            DataPointSeriesFactory(
                data_source=ds,
                series_type=steps_type,
                value=1000,
                recorded_at=_dt(f"2026-01-01T10:0{i}:00+00:00"),
            )

        result = service.get_activity_summaries(
            db,
            user.id,
            _dt("2026-01-01T00:00:00+00:00"),
            _dt("2026-01-02T00:00:00+00:00"),
            cursor=None,
            limit=10,
        )
        assert len(result.data) == 1
        assert result.data[0].steps == 3000

    def test_does_not_return_other_users_data(self, db: Session, service: SummariesService) -> None:
        user_a = UserFactory()
        user_b = UserFactory()
        ds = DataSourceFactory(user=user_b)
        steps_type = SeriesTypeDefinitionFactory.get_or_create_steps()
        DataPointSeriesFactory(
            data_source=ds, series_type=steps_type, value=5000, recorded_at=_dt("2026-01-01T10:00:00+00:00")
        )

        result = service.get_activity_summaries(
            db,
            user_a.id,
            _dt("2026-01-01T00:00:00+00:00"),
            _dt("2026-01-02T00:00:00+00:00"),
            cursor=None,
            limit=10,
        )
        assert result.data == []
