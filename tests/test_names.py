import pytest

from layerlog.names import is_mini, model_key


@pytest.mark.parametrize("raw, key", [
    ("Assembly_PLA_5h34m.gcode.3mf.gcode.3mf", "Assembly_PLA"),
    ("Assembly_PLA_5h34m.gcode.3mf", "Assembly_PLA"),
    ("Apricity_v1_1d8h37m_gcode_3mf_gcode.3mf", "Apricity_v1"),
    ("SpeedTestStructure_PLA_8m7s.gcode.3mf.gcode.3mf", "SpeedTestStructure_PLA"),
    ("Earring 1_PLA_4h0m.gcode.3mf.gcode.3mf", "Earring 1_PLA"),
    ("Part 1_PETG_1m4s.gcode.3mf.gcode.3mf", "Part 1_PETG"),
    ("prototype_shorter_walls.gcode.3mf", "prototype_shorter_walls"),
    ("Plain", "Plain"),
])
def test_model_key(raw, key):
    assert model_key(raw) == key


def test_model_key_keeps_non_time_numeric_tails():
    assert model_key("Bracket_v2.3mf") == "Bracket_v2"
    assert model_key("Bin_3x1x5_PLA_2h26m.gcode.3mf") == "Bin_3x1x5_PLA"


def test_is_mini():
    assert is_mini("Dragon_MINI_PLA_1h2m.gcode.3mf")
    assert not is_mini("Dragon_PLA_1h2m.gcode.3mf")
