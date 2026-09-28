from datetime import date

import pytest
from travel_api.db.session import Database
from travel_api.db.uow import UnitOfWork
from travel_api.domain.models import Trip, TripProfile


def test_unit_of_work_rolls_back_partial_trip(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'rollback.db'}")
    trip = Trip(
        profile=TripProfile(
            destination="成都",
            start_date=date(2026, 10, 3),
            end_date=date(2026, 10, 7),
        )
    )
    database.create_all()

    with pytest.raises(RuntimeError, match="force rollback"):
        with UnitOfWork(database.session_factory) as uow:
            uow.trips.create(trip)
            raise RuntimeError("force rollback")

    with UnitOfWork(database.session_factory) as uow:
        assert uow.trips.get(trip.id) is None
    database.engine.dispose()
