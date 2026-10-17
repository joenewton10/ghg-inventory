import pandas as pd
import pytest

from ghg_inventory.config import config_from_dict
from ghg_inventory.excel_io import read_input
from ghg_inventory.ui_state import (
    data_from_frames, empty_frames, frames_equal, frames_from_data, frames_from_workbook, save_workbook,
)


@pytest.fixture
def project(loaded):
    config, factors, base_dir = loaded
    return config, factors, read_input(base_dir / config.input, config), base_dir


def test_round_trip_through_grids(project):
    config, _, data, _ = project
    back, problems = data_from_frames(frames_from_data(data), config)
    assert problems == []
    assert [(a.source_id, a.month, a.quantity) for a in back.activities] == \
           [(a.source_id, a.month, a.quantity) for a in data.activities]
    assert back.activities[0].row_ref == "Activity row 1"
    assert back.metrics == data.metrics
    assert len(back.refrigerants) == 3


def test_grid_errors_name_rows(project):
    config, _, data, _ = project
    frames = frames_from_data(data)
    frames["activity"].loc[2, "quantity"] = -1
    frames["activity"].loc[4, "source_id"] = None
    frames["refrigerants"].loc[0, "annual_leak_rate"] = None
    result, problems = data_from_frames(frames, config)
    assert result is None
    text = "\n".join(problems)
    assert "Activity row 3, quantity" in text
    assert "Activity row 5, source_id" in text
    assert "Refrigerants row 1" in text and "annual_leak_rate" in text


def test_blank_rows_from_the_grid_are_ignored(project):
    config, _, data, _ = project
    frames = frames_from_data(data)
    frames["activity"] = pd.concat([frames["activity"], pd.DataFrame([{}])], ignore_index=True)
    _, problems = data_from_frames(frames, config)
    assert problems == []


def test_empty_grids_ask_for_data(project):
    config, *_ = project
    _, problems = data_from_frames(empty_frames(), config)
    assert problems == ["There are no activity rows yet."]


def test_workbook_upload_keeps_rows_with_errors(project, tmp_path):
    config, factors, data, base_dir = project
    frames = frames_from_workbook(base_dir / config.input)
    assert frames_equal(frames, frames_from_data(data))


def test_save_workbook_backs_up_and_reads_back(project, tmp_path):
    config, factors, data, _ = project
    path = tmp_path / "input.xlsx"
    assert save_workbook(config, factors, data, path) is None
    backup = save_workbook(config, factors, data, path)
    assert backup.exists() and backup.name.startswith("input.") and backup.suffix == ".xlsx"
    assert len(read_input(path, config).activities) == len(data.activities)


def test_settings_dict_validates(project):
    config, factors, *_ = project
    raw = config.model_dump(mode="json")
    raw["sources"][3]["market"]["eac_covered_share"] = 0.5
    assert config_from_dict(raw, factors).sources[3].market.eac_covered_share == 0.5
    raw["sources"][3]["market"]["eac_covered_share"] = 1.5
    with pytest.raises(Exception, match="eac_covered_share"):
        config_from_dict(raw, factors)


def test_plausibility_flags_typos_and_duplicates(project):
    from ghg_inventory.ui_state import plausibility_warnings
    config, _, data, _ = project
    frames = frames_from_data(data)
    assert plausibility_warnings(frames["activity"]) == []        # seasonal gas is not flagged
    act = frames["activity"]
    act.loc[0, "quantity"] = 176_470.0                             # missing decimal point
    act = pd.concat([act, act.iloc[[5]]], ignore_index=True)       # same bill twice
    warnings = plausibility_warnings(act)
    assert any(w.startswith("Activity row 1:") and "times the next highest" in w for w in warnings)
    assert any("rows 6, 41 repeat grid_electricity" in w for w in warnings)
