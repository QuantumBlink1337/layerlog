"""Shrink a sliced .gcode.3mf into a test fixture: keep metadata, cut the gcode to its header.

usage: python tools/trim_fixture.py SRC.gcode.3mf DEST.3mf
"""
import sys
import zipfile

KEEP_HEADER = 4096
src, dest = sys.argv[1:3]
with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zout:
    for item in zin.infolist():
        name = item.filename
        if name.endswith(".gcode"):
            with zin.open(name) as f:
                data = f.read(KEEP_HEADER)
        elif name.endswith(".png") and "plate_" not in name.split("/")[-1].replace("plate_no_light", "x"):
            continue  # drop top/pick/small images
        elif name.endswith(("_small.png", "plate_no_light_1.png")):
            continue
        else:
            data = zin.read(name)
        zout.writestr(name, data)
