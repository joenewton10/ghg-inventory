from pathlib import Path

import pytest

from ghg_inventory.config import load_config
from ghg_inventory.models import ActivityData, ActivityRecord, RefrigerantRecord

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def loaded():
    """(config, factors, base_dir) from the project's real config.yaml."""
    return load_config(ROOT / "config.yaml")


def activity(source_id, quantity, unit, month="2025-01", dq=1, row="Activity!2"):
    return ActivityRecord(row_ref=row, source_id=source_id, month=month, quantity=quantity, unit=unit, data_quality=dq)


def refrigerant(refrigerant="R-32", charge=3.0, leak=0.05, row="Refrigerants!2", **kw):
    return RefrigerantRecord(row_ref=row, equipment="Office AC", refrigerant=refrigerant, charge_kg=charge,
                             method=kw.pop("method", "screening"), annual_leak_rate=leak, data_quality=4, **kw)


def data(*records, refrigerants=(), metrics=None):
    return ActivityData(activities=list(records), refrigerants=list(refrigerants), metrics=metrics or {})
