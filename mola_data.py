"""Helpers for resolving the bundled MOLA elevation dataset."""

import shutil
import tempfile
from pathlib import Path
from zipfile import BadZipFile, ZipFile


class MolaDataError(Exception):
    """Raised when the MOLA archive cannot provide the requested NetCDF file."""


def resolve_mola_file(elevation_file):
    """Use an existing NetCDF file or extract it from the matching ZIP archive."""
    path = Path(elevation_file)
    if path.suffix.lower() == '.zip':
        archive_path = path
        netcdf_path = path.with_suffix('.nc')
    elif path.suffix.lower() == '.nc':
        netcdf_path = path
        archive_path = path.with_suffix('.zip')
    else:
        return path

    if netcdf_path.is_file():
        return netcdf_path
    if not archive_path.is_file():
        return netcdf_path

    try:
        with ZipFile(archive_path) as archive:
            matches = [
                item for item in archive.infolist()
                if not item.is_dir()
                and Path(item.filename).name.casefold()
                == netcdf_path.name.casefold()
            ]
            if len(matches) != 1:
                raise MolaDataError(
                    f'{archive_path.name} must contain exactly one '
                    f'{netcdf_path.name} file'
                )

            temporary_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    dir=netcdf_path.parent,
                    prefix=f'.{netcdf_path.name}.',
                    suffix='.tmp',
                    delete=False,
                ) as temporary_file:
                    temporary_path = Path(temporary_file.name)
                    with archive.open(matches[0]) as archived_file:
                        shutil.copyfileobj(archived_file, temporary_file)
                temporary_path.replace(netcdf_path)
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
    except (BadZipFile, OSError, RuntimeError) as error:
        raise MolaDataError(
            f'Could not extract {netcdf_path.name} from '
            f'{archive_path.name}: {error}'
        ) from error

    return netcdf_path
