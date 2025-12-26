import collections
import gzip
from pathlib import Path
from typing import List

import gpxpy

from .gpmf import GPSFix
from .point import Point
from .timeseries import Timeseries, Entry

GPX = collections.namedtuple("GPX", "time lat lon alt hr cad atemp power speed")


def fudge(gpx):
    """
    Parse GPX points including extension fields.

    Known extension tags (atemp/hr/cad/power/speed) are mapped to dedicated fields.
    All other extension values are collected into `extras` for downstream handling.
    """
    WLINQ_URI = "https://wunderlinq.local/ns/1"

    def _split_tag(tag: str):
        # Tag comes as "{uri}local" or "local"
        if tag.startswith("{") and "}" in tag:
            uri, local = tag[1:].split("}", 1)
            return uri, local
        return None, tag

    for track in gpx.tracks:
        for segment in track.segments:
            for point in segment.points:
                data = {
                    "time": point.time,
                    "lat": point.latitude,
                    "lon": point.longitude,
                    "alt": point.elevation,
                    "atemp": None,
                    "hr": None,
                    "cad": None,
                    "power": None,
                    "speed": None,
                    "extras": {},
                }
                # `point.extensions` is a list of XML elements (often container nodes).
                for extension in point.extensions:
                    for element in extension.iter():
                        # Skip container nodes with no value
                        if element.text is None:
                            continue
                        text = element.text.strip()
                        if text == "":
                            continue

                        uri, local = _split_tag(element.tag)

                        # Preserve legacy GPX extension fields if present
                        if local in ("atemp", "hr", "cad", "power", "speed"):
                            try:
                                data[local] = float(text)
                            except ValueError:
                                # ignore non-numeric
                                pass
                            continue

                        # Store everything else in extras
                        # For WunderLINQ namespace, prefix with "wlinq_"
                        if uri == WLINQ_URI:
                            key = f"wlinq_{local}"
                            # Gear may be "N" etc; keep it as a string.
                            if local == "gear":
                                data["extras"][key] = text   # keep "3", "4", "N"
                                continue
                        else:
                            # Unknown namespace: keep local name only (safe fallback)
                            key = local

                        # Try numeric conversion; otherwise keep string (gear/vin etc)
                        try:
                            val = float(text)
                        except ValueError:
                            val = text

                        data["extras"][key] = val
                yield GPX(**data)


def with_unit(gpx, units):
    """
    Convert base fields to pint quantities.
    Convert extras as best-effort quantities.
    Anything unknown remains as raw number/string for later use.
    """

    # NOTE:
    # Unit support here is intentionally conservative.
    # At the moment we normalise only units we are certain about based on suffixes
    # (_kmh, _km, _m, _c, _deg, _bar, _kpa, _v, _rpm, _pct).
    #
    # Support for additional units (e.g. mph, miles, Fahrenheit) depends on what
    # the active `units` registry provides. Once confirmed, handling can be added
    # by extending the suffix mapping below in a consistent way.


    def q_number(v):
        return units.Quantity(v, units.number)

    extras_q = {}

    for k, v in (gpx.extras or {}).items():
        # Keep strings (gear/vin etc) as-is
        if isinstance(v, str):
            extras_q[k] = v
            continue

        # v is numeric (float)
        # Apply a few safe conversions that we KNOW won't break existing units.
        # If you want richer units later (volt, kpa, etc), we can extend this.
        if k.endswith("_kmh"):
            # store as m/s so layout can convert to mph/kph with Converters "speed"
            extras_q[k] = units.Quantity(v / 3.6, units.mps)

        elif k.endswith("_m"):
            extras_q[k] = units.Quantity(v, units.m)

        elif k.endswith("_km"):
            extras_q[k] = units.Quantity(v * 1000.0, units.m)

        elif k.endswith("_c"):
            extras_q[k] = units.Quantity(v, units.celsius)

        elif k.endswith("_deg"):
            extras_q[k] = units.Quantity(v, units.degree)

        elif k.endswith("_kpa"):
            extras_q[k] = units.Quantity(v, units.kPa)

        elif k.endswith("_bar"):
            extras_q[k] = units.Quantity(v, units.bar)

        elif k.endswith("_v"):
            extras_q[k] = units.Quantity(v, units.volt)

        elif k.endswith("_rpm") or k == "wlinq_rpm":
            # your base.py currently emits <wlinq:rpm>...</wlinq:rpm> -> key "wlinq_rpm"
            extras_q[k] = units.Quantity(v, units.rpm)

        elif k.endswith("_pct"):
            extras_q[k] = q_number(v)

        else:
            extras_q[k] = q_number(v)
    return GPX(
        gpx.time,
        gpx.lat,
        gpx.lon,
        units.Quantity(gpx.alt, units.m) if gpx.alt is not None else None,
        units.Quantity(gpx.hr, units.bpm) if gpx.hr is not None else None,
        units.Quantity(gpx.cad, units.rpm) if gpx.cad is not None else None,
        units.Quantity(gpx.atemp, units.celsius) if gpx.atemp is not None else None,
        units.Quantity(gpx.power, units.watt) if gpx.power is not None else None,
        units.Quantity(gpx.speed, units.mps) if gpx.speed is not None else None,
        extras_q,
    )


def load(filepath: Path, units):
    if filepath.suffix == ".gz":
        with gzip.open(filepath, 'rb') as gpx_file:
            return load_xml(gpx_file, units)
    else:
        with filepath.open('r') as gpx_file:
            return load_xml(gpx_file, units)


def load_xml(file_or_str, units) -> List[GPX]:
    gpx = gpxpy.parse(file_or_str)

    return [with_unit(p, units) for p in fudge(gpx)]


def gpx_to_timeseries(gpx: List[GPX], units):
    gpx_timeseries = Timeseries()

    points = [
        Entry(
            point.time,
            point=Point(point.lat, point.lon),
            alt=point.alt,
            hr=point.hr,
            cad=point.cad,
            atemp=point.atemp,
            power=point.power,
            speed=point.speed,
            packet=units.Quantity(index),
            packet_index=units.Quantity(0),
            # we should set the gps fix or Journey.accept() will skip the point:
            gpsfix=GPSFix.LOCK_3D.value,
            gpslock=units.Quantity(GPSFix.LOCK_3D.value),
            **(point.extras or {}),
        )
        for index, point in enumerate(gpx)
    ]

    gpx_timeseries.add(*points)

    return gpx_timeseries


def load_timeseries(filepath: Path, units) -> Timeseries:
    return gpx_to_timeseries(load(filepath, units), units)
