"""Discover SHARAD radargram tracks from the public PDS archive index."""

from io import BytesIO
from urllib.request import urlopen

import pandas as pd


PDS_INDEX_URL = (
    'https://pds-geosciences.wustl.edu/mro/'
    'mro-m-sharad-5-radargram-v2/mrosh_2101/index/index.tab'
)

_INDEX_COLUMNS = [
    'volume_id',
    'file_specification_name',
    'product_id',
    'product_version',
    'product_creation_time',
    'orbit',
    'start_time',
    'stop_time',
    'start_lat',
    'stop_lat',
    'start_lon',
    'stop_lon',
]

_INDEX_USECOLS = [
    'product_id',
    'orbit',
    'start_time',
    'stop_time',
    'start_lat',
    'stop_lat',
    'start_lon',
    'stop_lon',
]


def load_track_catalog() -> pd.DataFrame:
    """Fetch and parse the PDS index entries for SHARAD radargrams."""
    with urlopen(PDS_INDEX_URL, timeout=120) as response:
        return parse_track_catalog(response.read())


def parse_track_catalog(index_data: bytes) -> pd.DataFrame:
    """Parse selected radargram fields from the PDS comma-separated index."""
    catalog = pd.read_csv(
        BytesIO(index_data),
        header=None,
        names=_INDEX_COLUMNS,
        usecols=_INDEX_USECOLS,
        quotechar='"',
        skipinitialspace=True,
        dtype=str,
    )
    catalog['product_id'] = catalog['product_id'].str.strip().str.upper()
    catalog = catalog[catalog['product_id'].str.endswith('_RGRAM', na=False)].copy()
    catalog['track_id'] = catalog['product_id'].str.extract(r'^S_(\d{8})_RGRAM$')

    for column in ('start_lat', 'stop_lat', 'start_lon', 'stop_lon'):
        catalog[column] = pd.to_numeric(catalog[column], errors='coerce')

    catalog = catalog.dropna(
        subset=['track_id', 'start_lat', 'stop_lat', 'start_lon', 'stop_lon']
    )
    catalog = catalog.drop_duplicates(subset='track_id', keep='last')
    return catalog.sort_values(['orbit', 'track_id']).reset_index(drop=True)


def _longitude_offset(longitude: float, center_longitude: float) -> float:
    """Return the shortest signed longitude offset in degrees."""
    return (longitude - center_longitude + 180) % 360 - 180


def _segment_intersects_rectangle(
    x_start: float,
    y_start: float,
    x_stop: float,
    y_stop: float,
    x_min: float,
    x_max: float,
    lat_min: float,
    lat_max: float,
) -> bool:
    """Test whether a line segment intersects an axis-aligned ROI rectangle."""
    dx = x_stop - x_start
    dy = y_stop - y_start
    lower = 0.0
    upper = 1.0

    for p, q in (
        (-dx, x_start - x_min),
        (dx, x_max - x_start),
        (-dy, y_start - lat_min),
        (dy, lat_max - y_start),
    ):
        if p == 0:
            if q < 0:
                return False
            continue

        ratio = q / p
        if p < 0:
            if ratio > upper:
                return False
            lower = max(lower, ratio)
        else:
            if ratio < lower:
                return False
            upper = min(upper, ratio)

    return lower <= upper


def clip_track_segment_to_roi(
    start_latitude: float,
    start_longitude: float,
    stop_latitude: float,
    stop_longitude: float,
    center_latitude: float,
    center_longitude: float,
    roi_size: float,
) -> tuple[float, float, float, float] | None:
    """Clip an indexed track segment to an ROI, returning lat/lon endpoints."""
    half_width = roi_size / 2
    lat_min = max(-90, center_latitude - half_width)
    lat_max = min(90, center_latitude + half_width)
    x_start = _longitude_offset(start_longitude, center_longitude)
    x_stop = _longitude_offset(stop_longitude, center_longitude)

    if x_stop - x_start > 180:
        x_stop -= 360
    elif x_stop - x_start < -180:
        x_stop += 360

    dx = x_stop - x_start
    dy = stop_latitude - start_latitude
    lower = 0.0
    upper = 1.0

    for p, q in (
        (-dx, x_start + half_width),
        (dx, half_width - x_start),
        (-dy, start_latitude - lat_min),
        (dy, lat_max - start_latitude),
    ):
        if p == 0:
            if q < 0:
                return None
            continue

        ratio = q / p
        if p < 0:
            if ratio > upper:
                return None
            lower = max(lower, ratio)
        else:
            if ratio < lower:
                return None
            upper = min(upper, ratio)

    if lower > upper:
        return None

    return (
        start_latitude + lower * dy,
        (center_longitude + x_start + lower * dx) % 360,
        start_latitude + upper * dy,
        (center_longitude + x_start + upper * dx) % 360,
    )


def find_tracks_in_roi(
    catalog: pd.DataFrame,
    center_latitude: float,
    center_longitude: float,
    roi_size: float,
) -> pd.DataFrame:
    """Return catalog tracks whose indexed endpoint segment crosses a square ROI."""
    if not -90 <= center_latitude <= 90:
        raise ValueError('Center latitude must be between -90 and 90 degrees.')
    if not 0 <= center_longitude < 360:
        raise ValueError('Center longitude must be between 0 and 360 degrees.')
    if not 0 < roi_size <= 180:
        raise ValueError('ROI width must be greater than 0 and at most 180 degrees.')

    half_width = roi_size / 2
    lat_min = max(-90, center_latitude - half_width)
    lat_max = min(90, center_latitude + half_width)
    matches = []

    for row_index, track in enumerate(catalog.itertuples(index=False)):
        x_start = _longitude_offset(track.start_lon, center_longitude)
        x_stop = _longitude_offset(track.stop_lon, center_longitude)

        if x_stop - x_start > 180:
            x_stop -= 360
        elif x_stop - x_start < -180:
            x_stop += 360

        if _segment_intersects_rectangle(
            x_start,
            track.start_lat,
            x_stop,
            track.stop_lat,
            -half_width,
            half_width,
            lat_min,
            lat_max,
        ):
            matches.append(row_index)

    return catalog.iloc[matches].reset_index(drop=True)


def format_track_list(
    center_latitude: float,
    center_longitude: float,
    roi_size: float,
    track_ids: list[str],
) -> str:
    """Format ROI parameters and selected IDs for the downloader and processor."""
    if not track_ids:
        raise ValueError('Select at least one track to create a track list.')

    header = f'{center_latitude:.6f},{center_longitude:.6f},{roi_size:.6f}'
    return header + '\n' + '\n'.join(track_ids) + '\n'
