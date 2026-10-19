"""Turn the messy names Orca/Bambuddy produce into a stable model key.

Orca exports ``<name>_<filament>_<time>.gcode.3mf`` and the suffix often
doubles up (``.gcode.3mf.gcode.3mf``) or arrives with the dots flattened
(``_gcode_3mf_gcode.3mf``).
"""

from __future__ import annotations

import re

# Any trailing run of these is container noise, applied until nothing changes.
_EXT_SUFFIX = re.compile(r"(?:\.gcode\.3mf|\.3mf|\.gcode|_gcode|_3mf)$", re.IGNORECASE)

# Orca's estimated-time suffix: 5h34m, 1d8h37m, 8m7s, 4h0m ... (needs at least one unit).
_TIME_SUFFIX = re.compile(r"_(?=\d+[dhms])(?:\d+d)?(?:\d+h)?(?:\d+m)?(?:\d+s)?$")


def model_key(name: str) -> str:
    """``Assembly_PLA_5h34m.gcode.3mf.gcode.3mf`` -> ``Assembly_PLA``."""
    prev = None
    while prev != name:
        prev = name
        name = _EXT_SUFFIX.sub("", name)
    return _TIME_SUFFIX.sub("", name)


def is_mini(name: str) -> bool:
    """Minis are marked by ``_mini`` anywhere in the file name."""
    return "_mini" in name.lower()
