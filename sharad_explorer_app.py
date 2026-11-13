"""Interactive Mars map for finding and downloading SHARAD radargram tracks."""

import html
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError

import folium
from folium.plugins import Draw
import pandas as pd
import streamlit as st
from streamlit_folium import st_folium

from track_discovery import (
    clip_track_segment_to_roi,
    find_tracks_in_roi,
    format_track_list,
    load_track_catalog,
)


PROJECT_DIR = Path(__file__).resolve().parent
SHARAD_DIR = PROJECT_DIR / 'src' / 'sharad'
DOWNLOADER_SCRIPT = PROJECT_DIR / 'src' / 'sharad' / 'sharad_download.py'
MOLA_TILES = (
    'https://trek.nasa.gov/tiles/Mars/EQ/'
    'Mars_MGS_MOLA_ClrShade_merge_global_463m/'
    '1.0.0/default/default028mm/{z}/{y}/{x}.jpg'
)
VIKING_COLOR_TILES = (
    'https://trek.nasa.gov/tiles/Mars/EQ/'
    'Mars_Viking_MDIM21_ClrMosaic_global_232m/'
    '1.0.0/default/default028mm/{z}/{y}/{x}.jpg'
)
KNOWN_REGIONS = {
    'Arabia Terra': (20.0, 25.0, 10.0),
    'Arcadia Planitia': (46.7, 196.0, 10.0),
    'Eberswalde Crater': (-23.9, 326.8, 5.0),
    'Gale Crater': (-5.4, 137.8, 5.0),
    'Hellas Planitia': (-42.4, 70.5, 12.0),
    'Isidis Planitia': (12.9, 87.0, 10.0),
    'Jezero Crater': (18.4, 77.7, 5.0),
    'Mawrth Vallis': (23.1, 341.2, 7.0),
    'Meridiani Planum': (-2.0, 6.0, 8.0),
    'Olympus Mons': (18.65, 226.2, 8.0),
    'Tharsis': (0.0, 250.0, 15.0),
    'Valles Marineris': (-10.0, 290.0, 15.0),
}


@st.cache_data(ttl=86400, show_spinner=False)
def cached_track_catalog() -> pd.DataFrame:
    """Cache the public PDS catalog for one day."""
    return load_track_catalog()


def project_relative_path(path: Path) -> str:
    """Display a project path without exposing the machine's directory."""
    project_path = Path(path)
    if not project_path.is_absolute():
        project_path = PROJECT_DIR / project_path
    try:
        relative_path = project_path.resolve().relative_to(PROJECT_DIR.resolve())
    except ValueError:
        return project_path.name
    return relative_path.as_posix()


def resolve_project_path(path_value: str) -> Path:
    """Resolve a user-entered path relative to the project folder."""
    relative_path = Path(path_value.strip())
    if not path_value.strip() or relative_path.is_absolute():
        raise ValueError('Enter a path relative to the project folder.')

    project_root = PROJECT_DIR.resolve()
    resolved_path = (project_root / relative_path).resolve()
    try:
        resolved_path.relative_to(project_root)
    except ValueError as error:
        raise ValueError(
            'Choose a path inside the project folder.'
        ) from error
    return resolved_path


def redact_local_paths(text: str) -> str:
    """Replace local project and user-folder prefixes in displayed console output."""
    roots = sorted(
        {PROJECT_DIR.resolve(), Path.home().resolve()},
        key=lambda root: len(str(root)),
        reverse=True,
    )
    for root in roots:
        for root_text in (str(root), str(root).replace('\\', '\\\\')):
            text = text.replace(root_text, '.')
    return text


def add_clicked_track_to_selection() -> None:
    """Add the clicked candidate track when the map reports a new interaction."""
    map_key = st.session_state.get(
        'track_map_component_key',
        'sharad-track-map',
    )
    map_state = st.session_state.get(map_key, {})
    clicked_tooltip = map_state.get('last_object_clicked_tooltip')
    clicked_track = re.search(r'\b\d{8}\b', str(clicked_tooltip or ''))
    tracks = st.session_state.get('candidate_tracks')
    if not clicked_track or not isinstance(tracks, pd.DataFrame):
        return

    track_id = clicked_track.group()
    if track_id not in set(tracks['track_id']):
        return

    add_track_to_selection(track_id)


def add_track_to_selection(track_id: str) -> None:
    """Add a valid candidate to the shared selection."""
    tracks = st.session_state.get('candidate_tracks')
    if not isinstance(tracks, pd.DataFrame):
        return
    if track_id not in set(tracks['track_id']):
        return

    selected = list(st.session_state.get('selected_track_ids', []))
    if track_id not in selected:
        st.session_state['selected_track_ids'] = selected + [track_id]


def roi_from_drawn_feature(feature: dict) -> tuple[float, float, float]:
    """Return center latitude, east longitude, and square width from a drawn rectangle."""
    geometry = feature.get('geometry', {})
    coordinates = geometry.get('coordinates')
    if geometry.get('type') != 'Polygon' or not coordinates or not coordinates[0]:
        raise ValueError('Draw a rectangle on the map to define the ROI.')

    vertices = coordinates[0]
    latitudes = [float(vertex[1]) for vertex in vertices]
    longitudes = [float(vertex[0]) % 360 for vertex in vertices]
    reference_longitude = longitudes[0]
    unwrapped_longitudes = [
        reference_longitude + ((longitude - reference_longitude + 180) % 360) - 180
        for longitude in longitudes
    ]

    south, north = min(latitudes), max(latitudes)
    west, east = min(unwrapped_longitudes), max(unwrapped_longitudes)
    width = max(north - south, east - west)
    if width <= 0 or width > 180:
        raise ValueError('Draw a rectangle with a width between 0 and 180 degrees.')

    center_latitude = (south + north) / 2
    center_longitude = ((west + east) / 2) % 360
    return center_latitude, center_longitude, width


def track_map_component_key(
    latitude: float,
    longitude: float,
    roi_size: float,
) -> str:
    """Remount the map only when the ROI changes, so it recenters on new coordinates."""
    signature = f'{latitude:.8f},{longitude:.8f},{roi_size:.8f}'
    digest = hashlib.sha1(signature.encode('ascii')).hexdigest()[:12]
    return f'sharad-track-map-{digest}'


def display_longitude(longitude: float) -> float:
    """Convert 0-360 east longitude to the -180 to 180 map convention."""
    return (longitude + 180) % 360 - 180


def split_track_at_dateline(
    start_lat: float,
    start_lon: float,
    stop_lat: float,
    stop_lon: float,
) -> list[list[list[float]]]:
    """Create Leaflet line segments without drawing across the map at the seam."""
    start_map_lon = display_longitude(start_lon)
    stop_map_lon = display_longitude(stop_lon)
    longitude_delta = (stop_map_lon - start_map_lon + 180) % 360 - 180
    unwrapped_stop_lon = start_map_lon + longitude_delta

    if unwrapped_stop_lon > 180:
        fraction = (180 - start_map_lon) / longitude_delta
        crossing_lat = start_lat + fraction * (stop_lat - start_lat)
        return [
            [[start_lat, start_map_lon], [crossing_lat, 180]],
            [[crossing_lat, -180], [stop_lat, unwrapped_stop_lon - 360]],
        ]
    if unwrapped_stop_lon < -180:
        fraction = (-180 - start_map_lon) / longitude_delta
        crossing_lat = start_lat + fraction * (stop_lat - start_lat)
        return [
            [[start_lat, start_map_lon], [crossing_lat, -180]],
            [[crossing_lat, 180], [stop_lat, unwrapped_stop_lon + 360]],
        ]
    return [[[start_lat, start_map_lon], [stop_lat, unwrapped_stop_lon]]]


def track_feature_collection(
    tracks: pd.DataFrame,
    center_latitude: float,
    center_longitude: float,
    roi_size: float,
    show_full_tracks: bool,
) -> dict:
    """Build map features, clipping candidates to the ROI unless expanded."""
    features = []
    for track in tracks.itertuples(index=False):
        if show_full_tracks:
            segments = split_track_at_dateline(
                track.start_lat,
                track.start_lon,
                track.stop_lat,
                track.stop_lon,
            )
        else:
            clipped = clip_track_segment_to_roi(
                track.start_lat,
                track.start_lon,
                track.stop_lat,
                track.stop_lon,
                center_latitude,
                center_longitude,
                roi_size,
            )
            if clipped is None:
                continue
            start_lat, start_lon, stop_lat, stop_lon = clipped
            segments = split_track_at_dateline(
                start_lat,
                start_lon,
                stop_lat,
                stop_lon,
            )

        coordinates = [
            [[longitude, latitude] for latitude, longitude in segment]
            for segment in segments
        ]
        features.append(
            {
                'type': 'Feature',
                'properties': {
                    'track_id': track.track_id,
                    'orbit': track.orbit,
                },
                'geometry': {
                    'type': 'MultiLineString',
                    'coordinates': coordinates,
                },
            }
        )

    return {'type': 'FeatureCollection', 'features': features}


def selected_track_feature_group(
    tracks: pd.DataFrame,
    selected_track_ids: list[str],
    center_latitude: float,
    center_longitude: float,
    roi_size: float,
    show_full_tracks: bool,
) -> folium.FeatureGroup | None:
    """Build the selected-track overlay that updates without rebuilding the map."""
    selected_tracks = tracks[tracks['track_id'].isin(selected_track_ids)]
    if selected_tracks.empty:
        return None

    feature_group = folium.FeatureGroup(
        name='Selected SHARAD tracks',
        overlay=True,
        control=False,
    )
    folium.GeoJson(
        track_feature_collection(
            selected_tracks,
            center_latitude,
            center_longitude,
            roi_size,
            show_full_tracks,
        ),
        pane='track-pane',
        style_function=lambda _: {
            'color': '#ffd740',
            'weight': 7,
            'opacity': 1,
        },
        highlight_function=lambda _: {
            'color': '#ffffff',
            'weight': 8,
            'opacity': 1,
        },
    ).add_to(feature_group)
    return feature_group


def add_roi(
    map_view: folium.Map,
    latitude: float,
    longitude: float,
    size: float,
    region_name: str,
) -> None:
    """Draw a high-contrast ROI border and a readable map label."""
    half_size = size / 2
    south = max(-90, latitude - half_size)
    north = min(90, latitude + half_size)
    center_map_lon = display_longitude(longitude)
    west = center_map_lon - half_size
    east = center_map_lon + half_size

    if west < -180:
        bounds = [(-180, east), (west + 360, 180)]
    elif east > 180:
        bounds = [(west, 180), (-180, east - 360)]
    else:
        bounds = [(west, east)]

    for left, right in bounds:
        folium.Rectangle(
            bounds=[[south, left], [north, right]],
            color='#ff6b35',
            weight=4,
            opacity=1,
            fill=True,
            fill_color='#ffb000',
            fill_opacity=0,
            pane='roi-pane',
            interactive=False,
            tooltip='Selected square ROI',
        ).add_to(map_view)

    folium.CircleMarker(
        location=[latitude, center_map_lon],
        radius=7,
        color='#ffffff',
        weight=3,
        fill=True,
        fill_color='#ff6b35',
        fill_opacity=1,
        pane='roi-pane',
        interactive=False,
        tooltip='ROI center',
    ).add_to(map_view)
    folium.Marker(
        location=[latitude, center_map_lon],
        pane='markerPane',
        interactive=False,
        icon=folium.DivIcon(
            icon_size=(240, 32),
            icon_anchor=(-12, 38),
            html=(
                '<div style="display:inline-block;padding:5px 9px;'
                'border:1px solid #ff6b35;border-radius:5px;'
                'background:rgba(255,255,255,.96);color:#18212b;'
                'font:600 13px sans-serif;white-space:nowrap;'
                'box-shadow:0 1px 5px #0006">'
                f'{html.escape(region_name)}</div>'
            ),
        ),
    ).add_to(map_view)


def roi_map_zoom(roi_size: float) -> int:
    """Choose a map zoom with extra space around the selected ROI."""
    zoom = round(math.log2(250 * 360 / (roi_size * 256))) - 1
    return max(2, min(8, zoom))


def build_track_map(
    latitude: float,
    longitude: float,
    roi_size: float,
    region_name: str,
    feature_collection_json: str | None,
    show_candidate_tracks: bool,
) -> folium.Map:
    """Build a fresh map because st_folium mutates the Folium object when rendering."""
    map_view = folium.Map(
        location=[latitude, display_longitude(longitude)],
        zoom_start=roi_map_zoom(roi_size),
        crs='EPSG4326',
        tiles=None,
        control_scale=True,
        world_copy_jump=True,
        scrollWheelZoom=False,
    )
    folium.TileLayer(
        tiles=MOLA_TILES,
        attr='NASA MOLA shaded relief via NASA Trek',
        name='MOLA shaded relief',
        min_zoom=0,
        max_zoom=8,
        overlay=False,
        control=True,
        show=True,
        no_wrap=False,
    ).add_to(map_view)
    folium.TileLayer(
        tiles=VIKING_COLOR_TILES,
        attr='NASA Viking MDIM 2.1 color mosaic via NASA Trek',
        name='Viking natural-color mosaic',
        min_zoom=0,
        max_zoom=8,
        overlay=False,
        control=True,
        show=False,
        no_wrap=False,
    ).add_to(map_view)
    folium.map.CustomPane('track-pane', z_index=450).add_to(map_view)
    folium.map.CustomPane(
        'roi-pane',
        z_index=650,
        pointer_events=False,
    ).add_to(map_view)

    if show_candidate_tracks and feature_collection_json:
        feature_collection = json.loads(feature_collection_json)
        if feature_collection['features']:
            folium.GeoJson(
                feature_collection,
                name='Available SHARAD tracks',
                pane='track-pane',
                style_function=lambda _: {
                    'color': '#00e5e5',
                    'weight': 2.5,
                    'opacity': 0.45,
                },
                highlight_function=lambda _: {
                    'color': '#ffffff',
                    'weight': 4,
                    'opacity': 0.95,
                },
                tooltip=folium.GeoJsonTooltip(
                    fields=['track_id', 'orbit'],
                    aliases=['Track ID', 'Orbit'],
                    sticky=True,
                ),
            ).add_to(map_view)

    Draw(
        export=False,
        draw_options={
            'polyline': False,
            'polygon': False,
            'circle': False,
            'circlemarker': False,
            'marker': False,
            'rectangle': True,
        },
        edit_options={'edit': False, 'remove': True},
    ).add_to(map_view)
    add_roi(map_view, latitude, longitude, roi_size, region_name)
    folium.LayerControl(
        position='topright',
        collapsed=False,
    ).add_to(map_view)
    return map_view


def safe_file_stem(name: str) -> str:
    """Create a filesystem-friendly filename stem from a region name."""
    stem = re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')
    return stem or 'mars_region'


def missing_processor_inputs(
    track_ids: list[str],
    generate_simulations: bool,
) -> list[Path]:
    """Return required local files that are missing for the selected tracks."""
    required_suffixes = ['_rgram.img', '_rgram.lbl', '_geom.tab']
    if generate_simulations:
        required_suffixes.extend(['_sim.img', '_sim.xml'])

    missing = []
    for track_id in track_ids:
        for suffix in required_suffixes:
            data_dir = (
                SHARAD_DIR / 'cluttergrams'
                if suffix.startswith('_sim')
                else SHARAD_DIR / 'radargrams'
            )
            path = data_dir / f's_{track_id}{suffix}'
            if not path.is_file():
                missing.append(path)
    return missing


def collect_run_previews(
    region_name: str,
    track_ids: list[str],
    run_started_ns: int,
) -> dict[str, list[Path]]:
    """Find selected-track PNG products created or updated during this run."""
    output_root = PROJECT_DIR / 'output' / safe_file_stem(region_name)
    product_folders = (
        'radargrams',
        'filtered',
        'simulations',
        'maps',
        'power_plots',
    )
    previews = {}
    for folder in product_folders:
        folder_path = output_root / folder
        if not folder_path.is_dir():
            continue
        images = [
            path
            for path in folder_path.glob('*.png')
            if any(track_id in path.name for track_id in track_ids)
            and path.stat().st_mtime_ns >= run_started_ns
        ]
        if images:
            previews[folder] = sorted(images, key=lambda path: path.name)
    return previews


def render_run_previews(previews: dict[str, list[str]]) -> None:
    """Render output images grouped by processor product."""
    if not previews:
        st.info(
            'Processing completed without producing preview images for the selected '
            'tracks. Check the console and processing log for skipped products.'
        )
        return

    st.subheader('Preview the generated images')
    tabs = st.tabs([f'{folder.replace("_", " ").title()} ({len(images)})'
                    for folder, images in previews.items()])
    for tab, (_, image_paths) in zip(tabs, previews.items()):
        with tab:
            for start in range(0, len(image_paths), 2):
                columns = st.columns(2)
                for column, image_path in zip(
                    columns,
                    image_paths[start:start + 2],
                ):
                    path = Path(image_path)
                    if path.is_file():
                        column.image(
                            str(path),
                            caption=path.stem,
                            use_container_width=True,
                        )


def build_processing_config(
    *,
    region_name: str,
    center_latitude: float,
    center_longitude: float,
    roi_size: float,
    track_file: Path,
    elevation_source: str,
    elevation_file: Path,
    generate_radargrams: bool,
    apply_filters: list[str],
    generate_maps: bool,
    generate_profiles: bool,
    generate_simulations: bool,
    roi_filter: bool,
    log_gabor_params: dict[str, float | int] | None,
    mask_vertical_noise: bool = False,
    noise_channel_min_excess_db: float = 8.0,
    noise_channel_min_persistence: float = 0.7,
    noise_channel_width: int = 1,
) -> dict[str, object]:
    """Build the existing processor's configuration from the explorer controls."""
    return {
        'region_name': safe_file_stem(region_name),
        'roi_center': [center_latitude, center_longitude],
        'roi_size': roi_size,
        'elevation_source': elevation_source,
        'elevation_file': str(elevation_file),
        'track_file': str(track_file),
        'filepath': str(SHARAD_DIR / 'radargrams'),
        'output_root': str(PROJECT_DIR / 'output'),
        'generate_radargrams': generate_radargrams,
        'apply_filters': apply_filters,
        'generate_maps': generate_maps,
        'generate_profiles': generate_profiles,
        'generate_simulations': generate_simulations,
        'roi_filter': roi_filter,
        'log_gabor_params': log_gabor_params,
        'mask_vertical_noise': mask_vertical_noise,
        'noise_channel_min_excess_db': noise_channel_min_excess_db,
        'noise_channel_min_persistence': noise_channel_min_persistence,
        'noise_channel_width': noise_channel_width,
    }


def run_command_with_console(
    command: list[str],
    working_directory: Path,
    status_label: str,
    output_state_key: str,
) -> tuple[int, str] | None:
    """Run a child process and stream its combined terminal output into the app."""
    try:
        process = subprocess.Popen(
            command,
            cwd=working_directory,
            env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1,
        )
    except OSError as error:
        st.error(f'Could not start the process: {error}')
        return None

    messages = []
    terminal = st.empty()
    with st.status(status_label, expanded=True) as status:
        if process.stdout is not None:
            pending_line = ''
            for character in iter(lambda: process.stdout.read(1), ''):
                if character in ('\r', '\n'):
                    if pending_line:
                        messages.append(redact_local_paths(pending_line))
                        terminal.code(
                            '\n'.join(messages[-40:]),
                            language='text',
                        )
                        pending_line = ''
                else:
                    pending_line += character
            if pending_line:
                messages.append(redact_local_paths(pending_line))
                terminal.code('\n'.join(messages[-40:]), language='text')
        return_code = process.wait()
        output = '\n'.join(messages)
        st.session_state[output_state_key] = output
        if return_code == 0:
            status.update(label='Process completed.', state='complete')
        else:
            status.update(
                label=f'Process exited with code {return_code}.',
                state='error',
            )
    return return_code, output


def main() -> None:
    st.set_page_config(page_title='Mars SHARAD Track Explorer', layout='wide')
    if st.session_state.get('explorer_ui_version') != 3:
        st.session_state['selected_track_ids'] = []
        st.session_state['explorer_ui_version'] = 3
    st.markdown(
        """
        <style>
        .main .block-container {max-width: 1500px; padding-top: 1.8rem;}
        .sharad-hero {
            padding: 1.8rem 2rem;
            border-radius: 18px;
            margin-bottom: 1.25rem;
            color: #f7faf9;
            background: linear-gradient(115deg, #17323c 0%, #28565d 60%, #9a653e 140%);
            box-shadow: 0 8px 26px rgba(17, 44, 51, .16);
        }
        .sharad-hero .eyebrow {
            color: #a9d8d1; font-size: .75rem; font-weight: 700;
            letter-spacing: .14em; text-transform: uppercase; margin-bottom: .5rem;
        }
        .sharad-hero h1 {
            color: #fff; font-size: 2.25rem; line-height: 1.15;
            margin: 0 0 .45rem 0;
        }
        .sharad-hero p {
            color: #e2eeec; font-size: 1rem; line-height: 1.55;
            margin: .55rem 0 0; max-width: 1000px;
        }
        .workflow-card {
            height: 100%; padding: .8rem 1rem; border-radius: 12px;
            background: rgba(32, 99, 99, .07);
            border: 1px solid rgba(32, 99, 99, .14);
        }
        .workflow-card strong {color: #176a68; font-size: 1rem;}
        .workflow-card p {margin: .25rem 0 0; color: #687781; font-size: .88rem;}
        [data-testid="stIFrame"] {border-radius: 14px; overflow: hidden;}
        </style>
        <div class="sharad-hero">
            <div class="eyebrow">Mars Reconnaissance Orbiter · SHARAD radar</div>
            <h1>Mars SHARAD Track Explorer</h1>
            <p>
                Discover where SHARAD radar observations cross a Mars region,
                select tracks, and turn their archived measurements into
                radargram, clutter-comparison, and terrain products.
            </p>
            <p>
                This workspace connects the public PDS SHARAD USRDR V2
                radargram archive with its complementary SHARAD Clutter
                Simulations archive. It prepares the selected observations for
                download and processing in three guided steps.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.subheader('Data library and processing inputs')
    data_columns = st.columns(3)
    data_cards = (
        (
            'Observation catalog',
            'PDS USRDR V2 index records include observation IDs, orbit and '
            'time metadata, and approximate endpoints for finding candidate '
            'crossings of your region.',
        ),
        (
            'Measured radar observations',
            'Per-track USRDR V2 radargram (<code>_rgram.img/.lbl</code>), '
            'sample geometry (<code>_geom.tab/.lbl</code>), and TIFF browse '
            'files. Measurements and geometry support processing.',
        ),
        (
            'MOLA-based clutter simulations',
            'Precomputed simulations using MOLA topography: '
            '<code>_sim</code>, <code>_emap</code>, and <code>_rtrn</code> '
            'products, plus browse hillshade. Available simulation data '
            'supports comparison with measured echoes.',
        ),
    )
    for column, (heading, detail) in zip(data_columns, data_cards):
        column.markdown(
            f'<div class="workflow-card"><strong>{heading}</strong>'
            f'<p>{detail}</p></div>',
            unsafe_allow_html=True,
        )
    st.caption(
        'Sources: '
        '[PDS SHARAD USRDR V2 archive]'
        '(https://pds-geosciences.wustl.edu/mro/mro-m-sharad-5-radargram-v2/) · '
        '[PDS SHARAD Clutter Simulations archive]'
        '(https://pds-geosciences.wustl.edu/mro/mro_sharad_simulations/). '
        'HRSC or MOLA elevation rasters are separate optional inputs for maps '
        'and profiles; the MOLA shaded-relief basemap is for map context.'
    )
    st.header('Step 1 · Select tracks')
    st.caption(
        'Choose an area in the sidebar, then search the PDS catalog. Cyan lines '
        'are approximate catalog paths, not detailed radar footprints.'
    )

    pending_region = st.session_state.pop('pending_region', None)
    if pending_region:
        name, latitude, longitude, size = pending_region
        st.session_state['region_name'] = name
        st.session_state['center_latitude'] = latitude
        st.session_state['center_longitude'] = longitude
        st.session_state['roi_size'] = size

    pending_center = st.session_state.pop('pending_center', None)
    if pending_center:
        st.session_state['center_latitude'], st.session_state['center_longitude'] = (
            pending_center
        )
        st.session_state['region_name'] = 'Custom area'
    pending_roi = st.session_state.pop('pending_roi', None)
    if pending_roi:
        (
            st.session_state['center_latitude'],
            st.session_state['center_longitude'],
            st.session_state['roi_size'],
        ) = pending_roi
        st.session_state['region_name'] = 'Custom area'
    if 'region_name' not in st.session_state:
        st.session_state['region_name'] = 'Mawrth Vallis'

    with st.sidebar:
        st.header('Find a region')
        region_choice = st.selectbox(
            'Search well-known Mars regions',
            options=['Choose a region'] + list(KNOWN_REGIONS),
            help='Type a region name to search the curated list.',
        )
        if st.button(
            'Go to region',
            disabled=region_choice == 'Choose a region',
        ):
            latitude, longitude, size = KNOWN_REGIONS[region_choice]
            st.session_state['pending_region'] = (
                region_choice,
                latitude,
                longitude,
                size,
            )
            st.rerun()
        st.caption(
            'Preset centers and widths are approximate starting areas. '
            'Adjust the ROI to suit your study.'
        )

        st.header('Region of interest')
        st.text_input(
            'Area name',
            key='region_name',
            help='Used on the map, in the track-list filename, and for the output folder.',
        )
        st.number_input(
            'Center latitude (degrees)',
            min_value=-90.0,
            max_value=90.0,
            value=23.1,
            step=0.01,
            key='center_latitude',
            help='Type a latitude from -90° to 90°. The map recenters when it changes.',
        )
        st.number_input(
            'Center east longitude (0-360 degrees)',
            min_value=0.0,
            max_value=359.999999,
            value=341.2,
            step=0.01,
            key='center_longitude',
            help='Type east longitude from 0° to 360°. The map recenters when it changes.',
        )
        st.number_input(
            'ROI width (degrees)',
            min_value=0.1,
            max_value=180.0,
            value=7.0,
            step=0.5,
            key='roi_size',
        )
        st.caption(
            'Set the center and width here, click the map to choose a center, '
            'or draw a rectangle. The app turns it into a square ROI.'
        )
        with st.expander('Catalog options'):
            st.caption('The public catalog is cached for one day.')
            if st.button('Refresh catalog now'):
                cached_track_catalog.clear()
                st.success('Catalog cache cleared. Search again to load the latest index.')

    latitude = st.session_state['center_latitude']
    longitude = st.session_state['center_longitude']
    roi_size = st.session_state['roi_size']
    region_name = st.session_state['region_name'].strip() or 'Custom area'
    search_parameters = (latitude, longitude, roi_size)
    tracks = st.session_state.get('candidate_tracks')
    saved_parameters = st.session_state.get('search_parameters')
    search_is_current = (
        isinstance(tracks, pd.DataFrame)
        and saved_parameters == search_parameters
    )
    show_tracks = False
    show_full_tracks = False
    if search_is_current:
        show_tracks = True
        show_full_tracks = st.checkbox(
            'Show full tracks outside this area',
            value=False,
            help='By default, only candidate segments inside the ROI are shown.',
        )
    feature_collection_json = None
    if search_is_current and show_tracks:
        feature_collection_json = json.dumps(
            track_feature_collection(
                tracks,
                latitude,
                longitude,
                roi_size,
                show_full_tracks,
            ),
            sort_keys=True,
            separators=(',', ':'),
        )
    map_view = build_track_map(
        latitude,
        longitude,
        roi_size,
        region_name,
        feature_collection_json,
        show_tracks,
    )
    selected_track_group = None
    selected_track_ids = st.session_state.get('selected_track_ids', [])
    if search_is_current and selected_track_ids:
        selected_track_group = selected_track_feature_group(
            tracks,
            selected_track_ids,
            latitude,
            longitude,
            roi_size,
            show_full_tracks,
        )
    map_component_key = track_map_component_key(
        latitude,
        longitude,
        roi_size,
    )
    st.session_state['track_map_component_key'] = map_component_key
    map_result = st_folium(
        map_view,
        height=620,
        use_container_width=True,
        feature_group_to_add=selected_track_group,
        returned_objects=[
            'last_clicked',
            'last_object_clicked_tooltip',
            'last_active_drawing',
        ],
        key=map_component_key,
        on_change=add_clicked_track_to_selection,
    )

    drawn_shape = (
        map_result.get('last_active_drawing')
        if map_result else None
    )
    if drawn_shape:
        try:
            st.session_state['pending_roi'] = roi_from_drawn_feature(drawn_shape)
            st.rerun()
        except (TypeError, ValueError, IndexError) as error:
            st.warning(f'Could not use the drawn shape: {error}')

    clicked = map_result.get('last_clicked') if map_result else None
    if clicked:
        clicked_latitude = float(clicked['lat'])
        clicked_longitude = float(clicked['lng']) % 360
        st.caption(
            f"Map point: {clicked_latitude:.4f}° latitude, "
            f"{clicked_longitude:.4f}° east longitude."
        )
        if st.button('Use map point as ROI center'):
            st.session_state['pending_center'] = (
                clicked_latitude,
                clicked_longitude,
            )
            st.rerun()

    if st.button('Find tracks crossing this ROI', type='primary'):
        try:
            with st.spinner('Loading the public PDS radargram catalog...'):
                catalog = cached_track_catalog()
                matches = find_tracks_in_roi(
                    catalog,
                    latitude,
                    longitude,
                    roi_size,
                )
            st.session_state['candidate_tracks'] = matches
            st.session_state['search_parameters'] = search_parameters
            st.session_state['selected_track_ids'] = []
            st.rerun()
        except (URLError, TimeoutError) as error:
            st.error(f'Could not load the PDS catalog: {error}')
        except (ValueError, pd.errors.ParserError) as error:
            st.error(f'Could not read the PDS catalog or ROI: {error}')

    if isinstance(tracks, pd.DataFrame):
        if saved_parameters != search_parameters:
            st.info('ROI settings changed. Search again to update the displayed tracks.')
        else:
            st.subheader(
                'No tracks found in this ROI'
                if tracks.empty
                else f'{len(tracks)} tracks cross this ROI'
            )
            if not tracks.empty:
                st.caption(
                    'Select tracks from the list or click map lines. Selected '
                    'tracks are highlighted in yellow.'
                )
            track_ids = tracks['track_id'].tolist()
            st.multiselect(
                'Selected tracks',
                options=track_ids,
                key='selected_track_ids',
                placeholder='Choose track IDs to add',
                help='Click the × on a selected ID to remove it.',
            )
            selected_ids = list(st.session_state.get('selected_track_ids', []))

            if selected_ids:
                st.header('Step 2 · Download selected tracks')
                track_list_text = format_track_list(
                    latitude,
                    longitude,
                    roi_size,
                    selected_ids,
                )
                file_stem = safe_file_stem(region_name)
                with st.expander('Preview or save the track-list file'):
                    st.caption(
                        'Includes this ROI followed by the selected track IDs.'
                    )
                    st.code(track_list_text, language='text')
                    st.download_button(
                        'Save track list',
                        data=track_list_text,
                        file_name=f'{file_stem}_tracks.txt',
                        mime='text/plain',
                    )
                st.caption(
                    'Files are saved automatically to '
                    '`src/sharad/radargrams/` and `src/sharad/cluttergrams/`.'
                )
                missing_downloads = missing_processor_inputs(selected_ids, True)
                if missing_downloads:
                    st.info(
                        f'{len(missing_downloads)} radargram, geometry, or cluttergram '
                        'files are not present yet.'
                    )
                else:
                    st.success('All selected-track data files are already available.')

                if st.button(
                    f'Download {len(selected_ids)} selected tracks',
                    type='primary',
                ):
                    with tempfile.TemporaryDirectory(
                        prefix='sharad_download_'
                    ) as temporary_directory:
                        track_file = Path(temporary_directory) / 'selected_tracks.txt'
                        track_file.write_text(track_list_text, encoding='utf-8')
                        result = run_command_with_console(
                            [
                                sys.executable,
                                '-u',
                                str(DOWNLOADER_SCRIPT),
                                str(track_file),
                            ],
                            SHARAD_DIR,
                            'Downloading selected SHARAD tracks...',
                            'download_console',
                        )
                    if result is not None:
                        return_code, output = result
                        failed_files = output.count('❌')
                        unavailable_files = output.count('⚠')
                        if return_code:
                            st.error(
                                f'Downloader exited with code {return_code}. '
                                'Review its console and log.'
                            )
                        elif failed_files:
                            st.warning(
                                f'The downloader reported {failed_files} failed files.'
                            )
                        elif unavailable_files:
                            st.info(
                                f'{unavailable_files} archive products were unavailable.'
                            )
                        else:
                            st.success('SHARAD download completed.')
                if st.session_state.get('download_console'):
                    with st.expander('Downloader console output'):
                        st.code(st.session_state['download_console'], language='text')

                st.header('Step 3 · Customize and run the processor')
                st.caption(
                    'Choose the products to create. Terrain products use the '
                    'selected elevation source; results go to the region folder.'
                )
                product_columns = st.columns(2)
                with product_columns[0]:
                    generate_radargrams = st.checkbox(
                        'Original radargrams',
                        value=False,
                        key='processing_generate_radargrams',
                        help='Create radargram images from the measured echoes.',
                    )
                    generate_maps = st.checkbox(
                        'Topographic maps',
                        value=False,
                        key='processing_generate_maps',
                        help='Create track-aligned terrain maps.',
                    )
                    generate_profiles = st.checkbox(
                        'Elevation profiles',
                        value=False,
                        key='processing_generate_profiles',
                        help='Sample terrain elevation along each selected track.',
                    )
                with product_columns[1]:
                    generate_simulations = st.checkbox(
                        'Cluttergram comparisons',
                        value=True,
                        key='processing_generate_simulations',
                        help='Process the precomputed MOLA-based simulation products.',
                    )
                    roi_filter = st.checkbox(
                        'Skip tracks with no data in this area',
                        value=True,
                        key='processing_roi_filter',
                        help='Keeps processing focused on selected ROI crossings.',
                    )
                    apply_filters = st.multiselect(
                        'Filtered radargrams',
                        options=['gaussian', 'lee', 'log_gabor'],
                        default=['log_gabor'],
                        key='processing_filters',
                        help='Choose one or more filters, or leave empty to skip filtering.',
                    )

                needs_elevation = generate_maps or generate_profiles
                elevation_source = 'mola'
                default_elevation_file = Path('src') / 'mola_dem' / 'mola32.nc'
                elevation_path = default_elevation_file
                elevation_path_error = None
                elevation_missing = False
                if needs_elevation:
                    st.markdown('**Terrain source**')
                    elevation_source = st.selectbox(
                        'Elevation source',
                        options=['mola', 'hrsc'],
                        format_func=str.upper,
                        index=0,
                        key='processing_elevation_source',
                        help='MOLA is included. HRSC requires a local raster.',
                    )
                    default_elevation_file = (
                        Path('src') / 'hrsc' / 'mawrth_vallis_hrsc.tif'
                        if elevation_source == 'hrsc'
                        else Path('src') / 'mola_dem' / 'mola32.nc'
                    )
                    st.caption(
                        'MOLA uses the included global DEM. HRSC requires an '
                        'elevation raster placed in `src/hrsc/`.'
                    )
                    elevation_file_value = st.text_input(
                        'Terrain file (project-relative path)',
                        value=default_elevation_file.as_posix(),
                        key=f'processing_elevation_relative_path_{elevation_source}',
                    )
                    try:
                        elevation_path = resolve_project_path(elevation_file_value)
                    except ValueError as error:
                        elevation_path_error = str(error)
                        st.warning(elevation_path_error)
                    elevation_missing = (
                        elevation_path_error is not None
                        or not (PROJECT_DIR / elevation_path).is_file()
                    )

                with st.expander('Advanced radargram cleanup'):
                    mask_vertical_noise = st.checkbox(
                        'Mask persistent bright vertical channels',
                        value=True,
                        key='processing_mask_vertical_noise',
                        help=(
                            'The same channel mask is used in original and '
                            'filtered radargram images.'
                        ),
                    )
                    noise_channel_min_excess_db = 8.0
                    noise_channel_min_persistence = 0.7
                    noise_channel_width = 1
                    if mask_vertical_noise:
                        detection_columns = st.columns(3)
                        with detection_columns[0]:
                            noise_channel_min_excess_db = st.number_input(
                                'Brightness threshold (dB)',
                                min_value=1.0,
                                max_value=30.0,
                                value=8.0,
                                step=1.0,
                                key='noise_channel_min_excess_db',
                                help='Higher values detect only stronger channels.',
                            )
                        with detection_columns[1]:
                            noise_channel_min_persistence = st.slider(
                                'Rows affected',
                                min_value=0.5,
                                max_value=1.0,
                                value=0.7,
                                step=0.05,
                                key='noise_channel_min_persistence',
                                help='Fraction of radargram rows where the channel must stay bright.',
                            )
                        with detection_columns[2]:
                            noise_channel_width = st.number_input(
                                'Extra channels on each side',
                                min_value=0,
                                max_value=5,
                                value=1,
                                step=1,
                                key='noise_channel_width',
                                help='Number of adjacent channels masked on each side.',
                            )
                log_gabor_params = None
                if 'log_gabor' in apply_filters:
                    customize_log_gabor = st.checkbox(
                        'Adjust Log-Gabor settings',
                        value=False,
                        key='customize_log_gabor',
                    )
                else:
                    customize_log_gabor = False
                if customize_log_gabor:
                    parameter_columns = st.columns(5)
                    with parameter_columns[0]:
                        log_gabor_k = st.number_input(
                            'k', min_value=0.1, value=5.0, step=0.1,
                            key='log_gabor_k',
                        )
                    with parameter_columns[1]:
                        log_gabor_nscale = st.number_input(
                            'Scales', min_value=1, max_value=10, value=6, step=1,
                            key='log_gabor_nscale',
                        )
                    with parameter_columns[2]:
                        log_gabor_mult = st.number_input(
                            'Multiplier', min_value=1.0, value=3.0, step=0.1,
                            key='log_gabor_mult',
                        )
                    with parameter_columns[3]:
                        log_gabor_norient = st.number_input(
                            'Orientations', min_value=1, max_value=16, value=6, step=1,
                            key='log_gabor_norient',
                        )
                    with parameter_columns[4]:
                        log_gabor_softness = st.number_input(
                            'Softness', min_value=0.0, max_value=1.0, value=0.6,
                            step=0.05, key='log_gabor_softness',
                        )
                    log_gabor_params = {
                        'k': log_gabor_k,
                        'nscale': log_gabor_nscale,
                        'mult': log_gabor_mult,
                        'norient': log_gabor_norient,
                        'softness': log_gabor_softness,
                    }

                processor_missing = missing_processor_inputs(
                    selected_ids,
                    generate_simulations,
                )
                if processor_missing:
                    st.warning(
                        f'{len(processor_missing)} required input files are missing. '
                        'Run the download step first.'
                    )
                    with st.expander('Show missing processor inputs'):
                        st.code(
                            '\n'.join(
                                project_relative_path(path)
                                for path in processor_missing
                            ),
                            language='text',
                        )
                if elevation_missing:
                    st.warning(
                        'The selected terrain file was not found. Enter a valid '
                        'project-relative path or turn off terrain products.'
                    )

                selected_products = (
                    generate_radargrams
                    or bool(apply_filters)
                    or generate_maps
                    or generate_profiles
                    or generate_simulations
                )
                region_slug = safe_file_stem(region_name)
                st.caption(
                    'Output folder: '
                    f'`{project_relative_path(PROJECT_DIR / "output" / region_slug)}`'
                )
                if st.button(
                    'Run SHARAD processor',
                    type='primary',
                    disabled=(
                        not selected_products
                        or bool(processor_missing)
                        or elevation_missing
                    ),
                ):
                    try:
                        with tempfile.TemporaryDirectory(
                            prefix='sharad_processing_'
                        ) as temporary_directory:
                            track_file = Path(temporary_directory) / 'selected_tracks.txt'
                            track_file.write_text(track_list_text, encoding='utf-8')
                            processor_config = build_processing_config(
                                region_name=region_slug,
                                center_latitude=latitude,
                                center_longitude=longitude,
                                roi_size=roi_size,
                                track_file=track_file,
                                elevation_source=elevation_source,
                                elevation_file=PROJECT_DIR / elevation_path,
                                generate_radargrams=generate_radargrams,
                                apply_filters=apply_filters,
                                generate_maps=generate_maps,
                                generate_profiles=generate_profiles,
                                generate_simulations=generate_simulations,
                                roi_filter=roi_filter,
                                log_gabor_params=log_gabor_params,
                                mask_vertical_noise=mask_vertical_noise,
                                noise_channel_min_excess_db=noise_channel_min_excess_db,
                                noise_channel_min_persistence=noise_channel_min_persistence,
                                noise_channel_width=noise_channel_width,
                            )
                            processor_entry = (
                                'import json, sys; '
                                'from sharad_processor import run_batch_processing; '
                                'run_batch_processing(json.loads(sys.argv[1]))'
                            )
                            run_started_ns = time.time_ns()
                            st.session_state.pop('processor_preview_signature', None)
                            st.session_state.pop('processor_previews', None)
                            result = run_command_with_console(
                                [
                                    sys.executable,
                                    '-u',
                                    '-c',
                                    processor_entry,
                                    json.dumps(processor_config),
                                ],
                                PROJECT_DIR,
                                'Running the SHARAD processor...',
                                'processor_console',
                            )
                        if result is not None:
                            return_code, output = result
                            if (
                                return_code
                                or '❌ Error during processing:' in output
                                or 'ERROR - Error processing track ' in output
                                or 'ERROR - Processing failed for track ' in output
                            ):
                                st.error(
                                    'The processor reported an error. Review its console '
                                    'and processing log.'
                                )
                            else:
                                st.success('SHARAD processing completed.')
                                previews = collect_run_previews(
                                    region_slug,
                                    selected_ids,
                                    run_started_ns,
                                )
                                st.session_state['processor_preview_signature'] = (
                                    region_slug,
                                    tuple(sorted(selected_ids)),
                                )
                                st.session_state['processor_previews'] = {
                                    folder: [str(path) for path in paths]
                                    for folder, paths in previews.items()
                                }
                    except OSError as error:
                        st.error(f'Could not prepare the SHARAD processing run: {error}')
                if st.session_state.get('processor_console'):
                    with st.expander('Processor console output'):
                        st.code(
                            st.session_state['processor_console'],
                            language='text',
                        )
                preview_signature = (
                    region_slug,
                    tuple(sorted(selected_ids)),
                )
                if (
                    st.session_state.get('processor_preview_signature')
                    == preview_signature
                ):
                    render_run_previews(
                        st.session_state.get('processor_previews', {})
                    )
            elif not tracks.empty:
                st.info(
                    'Add at least one track from the map or candidate ID list. '
                    'The download and processing steps will appear here.'
                )

    st.divider()
    st.caption(
        'Track catalog: NASA PDS Geosciences Node, '
        '[SHARAD Radargram V2 index]('
        'https://pds-geosciences.wustl.edu/mro/mro-m-sharad-5-radargram-v2/'
        'mrosh_2101/index/index.tab). '
        'Basemap: [NASA Trek MOLA shaded relief](https://trek.nasa.gov/).'
    )


if __name__ == '__main__':
    main()
