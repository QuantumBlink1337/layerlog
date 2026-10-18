"""Parse a sliced Orca/Bambu ``.gcode.3mf`` into the settings we log.

Only a few small members of the archive are read; ``plate_<N>.gcode`` is
never read past its header block (it can be hundreds of MB).
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any, BinaryIO
from xml.etree import ElementTree as ET

GCODE_HEADER_BYTES = 4096

# project_settings.config key holding the bed temperature, by curr_bed_type.
BED_TEMP_KEYS = {
    "Cool Plate": "cool_plate_temp",
    "Engineering Plate": "eng_plate_temp",
    "High Temp Plate": "hot_plate_temp",
    "Textured PEI Plate": "textured_plate_temp",
    "Textured Cool Plate": "textured_cool_plate_temp",
    "SuperTack Plate": "supertack_plate_temp",
}

# Per-object metadata keys in model_settings.config that are not settings.
_OBJECT_NON_SETTINGS = {"name", "extruder", "matrix", "source_file", "source_object_id",
                        "source_volume_id", "source_offset_x", "source_offset_y",
                        "source_offset_z", "source_in_inches", "source_in_meters"}


class ThreeMFError(ValueError):
    """The file is not a usable sliced 3MF."""


def nil(value: Any) -> Any:
    """Orca writes ``'nil'`` for "inherited / not set"."""
    return None if value == "nil" else value


def _num(value: Any) -> float | None:
    value = nil(value)
    if value in (None, ""):
        return None
    try:
        return float(str(value).rstrip("%"))
    except ValueError:
        return None


@dataclass
class FilamentUse:
    slot: int  # 1-based, matches slice_info <filament id>
    type: str | None
    color: str | None
    used_g: float | None
    used_m: float | None
    preset: str | None = None


@dataclass
class ThreeMFInfo:
    plate_index: int
    plate_name: str | None
    slicer: str | None
    slicer_version: str | None
    printer_preset: str | None
    process_preset: str | None
    variant: str | None  # active nozzle variant, e.g. "Direct Drive High Flow"
    filaments: list[FilamentUse]
    primary: FilamentUse | None  # heaviest used filament; drives the per-filament fields
    nozzle_mm: float | None
    layer_height: float | None
    first_layer_height: float | None
    nozzle_temp: float | None
    bed_temp: float | None
    plate_type: str | None
    pressure_advance: float | None
    flow_ratio: float | None
    max_volumetric_speed: float | None
    retraction: float | None
    z_hop: float | None
    wipe: bool | None
    supports: bool | None
    brim: str | None
    walls: int | None
    infill: str | None
    est_time_s: float | None
    weight_g: float | None
    layers: int | None
    max_z: float | None
    warnings: list[str]
    object_names: list[str]
    object_overrides: dict[str, dict[str, str]]
    changed_from_system: list[str]
    settings: dict[str, Any] = field(repr=False)  # full resolved settings, nil -> None
    thumbnail: bytes | None = field(default=None, repr=False)


def _normalize(value: Any) -> Any:
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    return nil(value)


def _metadata(node: ET.Element) -> dict[str, str]:
    return {m.get("key", ""): m.get("value", "") for m in node.findall("metadata")}


class _Resolver:
    """Picks the right entry out of Orca's per-filament / per-variant arrays."""

    def __init__(self, ps: dict[str, Any], variant: str | None, variant_idx: int):
        self.ps = ps
        self.variant = variant
        self.variant_idx = variant_idx
        self.self_index = [str(i) for i in ps.get("filament_self_index") or []]
        self.fil_variants = ps.get("filament_extruder_variant") or []
        self.n_filaments = len(ps.get("filament_settings_id") or [])

    def scalar(self, key: str) -> Any:
        v = self.ps.get(key)
        return v[0] if isinstance(v, list) and v else v

    def printer(self, key: str) -> Any:
        """Array keyed by printer extruder variant (e.g. retraction_length)."""
        v = self.ps.get(key)
        if not isinstance(v, list):
            return v
        if len(v) == len(self.ps.get("printer_extruder_variant") or []) and self.variant_idx < len(v):
            return v[self.variant_idx]
        return v[0] if v else None

    def filament(self, key: str, slot: int) -> Any:
        """Array keyed per filament, or per (filament, variant) pair."""
        v = self.ps.get(key)
        if not isinstance(v, list):
            return v
        if self.self_index and len(v) == len(self.self_index):
            matches = [i for i, s in enumerate(self.self_index) if s == str(slot)]
            for i in matches:
                if i < len(self.fil_variants) and self.fil_variants[i] == self.variant:
                    return v[i]
            if matches:
                return v[matches[0]]
        if 0 < slot <= len(v):
            return v[slot - 1]
        return None


def _gcode_header(z: zipfile.ZipFile, name: str) -> str:
    if name not in z.namelist():
        return ""
    with z.open(name) as f:
        return f.read(GCODE_HEADER_BYTES).decode("utf-8", errors="replace")


def parse(source: str | bytes | BinaryIO) -> ThreeMFInfo:
    """Parse a sliced 3MF given a path, raw bytes, or a binary file object."""
    if isinstance(source, bytes):
        source = io.BytesIO(source)
    try:
        z = zipfile.ZipFile(source)
    except zipfile.BadZipFile as e:
        raise ThreeMFError("not a zip archive") from e
    with z:
        names = set(z.namelist())
        for required in ("Metadata/slice_info.config", "Metadata/project_settings.config"):
            if required not in names:
                raise ThreeMFError(f"missing {required} (unsliced project?)")

        slice_root = ET.fromstring(z.read("Metadata/slice_info.config"))
        plate = slice_root.find("plate")
        if plate is None:
            raise ThreeMFError("slice_info has no <plate>")
        meta = _metadata(plate)
        plate_index = int(meta.get("index", "1"))

        ps = {k: _normalize(v) for k, v in json.loads(z.read("Metadata/project_settings.config")).items()}

        variants = ps.get("printer_extruder_variant") or []
        variant_idx = int(meta.get("nozzle_volume_type", "0") or 0)
        variant = variants[variant_idx] if variant_idx < len(variants) else None
        r = _Resolver(ps, variant, variant_idx)

        # Filaments actually used on this plate.
        presets = ps.get("filament_settings_id") or []
        filaments = []
        for f in plate.findall("filament"):
            slot = int(f.get("id", "0"))
            filaments.append(FilamentUse(
                slot=slot, type=f.get("type"), color=f.get("color"),
                used_g=_num(f.get("used_g")), used_m=_num(f.get("used_m")),
                preset=presets[slot - 1] if 0 < slot <= len(presets) else None,
            ))
        primary = max(filaments, key=lambda f: f.used_g or 0, default=None)
        slot = primary.slot if primary else 1

        # Plate name and per-object overrides from model_settings.config.
        plate_name = None
        overrides: dict[str, dict[str, str]] = {}
        if "Metadata/model_settings.config" in names:
            ms = ET.fromstring(z.read("Metadata/model_settings.config"))
            for p in ms.findall("plate"):
                pm = _metadata(p)
                if pm.get("plater_id") == str(plate_index):
                    plate_name = pm.get("plater_name") or None
            for obj in ms.findall("object"):
                om = _metadata(obj)
                changed = {k: v for k, v in om.items() if k not in _OBJECT_NON_SETTINGS}
                if changed and om.get("name"):
                    overrides[om["name"]] = changed

        object_names = [o.get("name", "") for o in plate.findall("object")]
        # Overlay overrides onto the headline fields only when every object on
        # the plate agrees; otherwise the global value is reported and the
        # per-object detail stays in object_overrides.
        plate_overrides = [overrides.get(n, {}) for n in object_names]

        def eff(key: str, default: Any) -> Any:
            vals = [o.get(key) for o in plate_overrides]
            if vals and all(v is not None for v in vals) and len(set(vals)) == 1:
                return vals[0]
            return default

        # Slicer identity: the gcode header is truthful; 3dmodel.model says BambuStudio.
        header = _gcode_header(z, f"Metadata/plate_{plate_index}.gcode")
        m = re.search(r"generated by (\S+) (\S+) on", header)
        slicer, slicer_version = (m.group(1), m.group(2)) if m else (None, None)
        m = re.search(r"total layer number:\s*(\d+)", header)
        layers = int(m.group(1)) if m else None
        m = re.search(r"max_z_height:\s*([\d.]+)", header)
        max_z = float(m.group(1)) if m else None

        # Bed temperature depends on the plate in use.
        plate_type = nil(r.scalar("curr_bed_type"))
        bed_key = BED_TEMP_KEYS.get(plate_type or "")
        bed_temp = _num(r.filament(bed_key, slot)) if bed_key else None

        nozzle = _num(meta.get("nozzle_diameters")) or _num(r.scalar("nozzle_diameter"))

        # filament_* overrides fall back to the printer-level setting when 'nil'.
        retraction = nil(r.filament("filament_retraction_length", slot))
        if retraction is None:
            retraction = r.printer("retraction_length")
        z_hop = nil(r.filament("filament_z_hop", slot))
        if z_hop is None:
            z_hop = r.printer("z_hop")
        wipe = nil(r.filament("filament_wipe", slot))
        if wipe is None:
            wipe = r.printer("wipe")

        pa_on = r.filament("enable_pressure_advance", slot)
        pa = _num(r.filament("pressure_advance", slot)) if pa_on in ("1", 1, True) else None

        support = eff("enable_support", r.scalar("enable_support"))

        thumb_name = f"Metadata/plate_{plate_index}.png"
        thumbnail = z.read(thumb_name) if thumb_name in names else None

        # A list of ';'-joined key strings: process first, then one per filament.
        diff = ps.get("different_settings_to_system") or []
        diff = [diff] if isinstance(diff, str) else diff
        changed = sorted({k for entry in diff for k in (entry or "").split(";") if k})

        return ThreeMFInfo(
            plate_index=plate_index,
            plate_name=plate_name,
            slicer=slicer,
            slicer_version=slicer_version,
            printer_preset=nil(r.scalar("printer_settings_id")),
            process_preset=nil(r.scalar("print_settings_id")),
            variant=variant,
            filaments=filaments,
            primary=primary,
            nozzle_mm=nozzle,
            layer_height=_num(eff("layer_height", r.scalar("layer_height"))),
            first_layer_height=_num(r.scalar("initial_layer_print_height")),
            nozzle_temp=_num(r.filament("nozzle_temperature", slot)),
            bed_temp=bed_temp,
            plate_type=plate_type,
            pressure_advance=pa,
            flow_ratio=_num(r.filament("filament_flow_ratio", slot)),
            max_volumetric_speed=_num(r.filament("filament_max_volumetric_speed", slot)),
            retraction=_num(retraction),
            z_hop=_num(z_hop),
            wipe=None if wipe is None else str(wipe) in ("1", "true"),
            supports=None if support is None else str(support) in ("1", "true"),
            brim=nil(eff("brim_type", r.scalar("brim_type"))),
            walls=(int(w) if (w := _num(eff("wall_loops", r.scalar("wall_loops")))) is not None else None),
            infill=nil(eff("sparse_infill_density", r.scalar("sparse_infill_density"))),
            est_time_s=_num(meta.get("prediction")),
            weight_g=_num(meta.get("weight")),
            layers=layers,
            max_z=max_z,
            warnings=[w.get("msg", "") for w in plate.findall("warning")],
            object_names=object_names,
            object_overrides=overrides,
            changed_from_system=changed,
            settings=ps,
            thumbnail=thumbnail,
        )
