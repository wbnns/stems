""" CF conventions for referencing xarray/NetCDF data

Includes functions useful for managing CF conventions (variable and
coordinating naming, grid mapping variables, etc)
"""
from collections import OrderedDict
import logging

from affine import Affine
import numpy as np
from rasterio.crs import CRS
from rasterio.coords import BoundingBox
import xarray as xr

from . import projections, utils


logger = logging.getLogger(__name__)


# =============================================================================
# DATA
# Names of x/y dimensions, ordered with some preference
_X_DIMENSIONS = ['x', 'longitude', 'lon', 'long']
_Y_DIMENSIONS = ['y', 'latitude', 'lat']

#: dict: CF coordinate attribute metadata
# http://cfconventions.org/cf-conventions/v1.6.0/cf-conventions.html#coordinate-types
COORD_DEFS = {
    'longitude': {
        'standard_name': 'longitude',
        'long_name': 'longitude',
        'units': 'degrees_east',
    },
    'latitude': {
        'standard_name': 'latitude',
        'long_name': 'latitude',
        'units': 'degrees_north',
    },
    'x': {
        'standard_name': 'projection_x_coordinate',
        'long_name': 'x coordinate of projection',
    },
    'y': {
        'standard_name': 'projection_y_coordinate',
        'long_name': 'y coordinate of projection',
    },
    'time': {
        'standard_name': 'time',
        'long_name': 'Time, unix time-stamp',
        'axis': 'T',
        'calendar': 'standard'
    }
}

#: dict: CF NetCDF attributes
# http://cfconventions.org/cf-conventions/v1.6.0/cf-conventions.html#identification-of-conventions
CF_NC_ATTRS = OrderedDict((
    ('Conventions', 'CF-1.7'),
))


# ============================================================================
# Georeferencing
def georeference(xarr, crs, transform, grid_mapping='crs', inplace=False):
    """ Georeference XArray data with the CRS and Affine transform

    Parameters
    ----------
    xarr : xarray.DataArray or xarray.Dataset
        XArray data to georeference
    crs : rasterio.crs.CRS
        Rasterio CRS
    transform : affine.Affine
        Affine transform of the data
    grid_mapping : str, optional
        Name to use for grid mapping variable
    inplace : bool, optional
        If ``False``, returns a modified shallow copy of ``xarr``

    Returns
    -------
    xarray.DataArray or xarray.Dataset
        Georeferenced data (type depending on input)
    """
    assert isinstance(xarr, (xr.DataArray, xr.Dataset))
    assert isinstance(crs, CRS)
    assert isinstance(transform, Affine)
    assert isinstance(grid_mapping, str)

    # Copy as needed
    xarr = xarr if inplace else xarr.copy()

    # Create grid mapping
    xarr.coords[grid_mapping] = create_grid_mapping(crs, transform,
                                                    grid_mapping=grid_mapping)

    # "Georeference" 2D data (variables)
    dim_x, dim_y = projections.cf_xy_coord_names(crs)

    if isinstance(xarr, xr.DataArray):
        xarr = _georef(xarr, dim_x, dim_y, grid_mapping)
    elif isinstance(xarr, xr.Dataset):
        for var in xarr.data_vars:
            xarr[var] = _georef(xarr[var], dim_x, dim_y, grid_mapping)

    # Add additional CF related attributes
    xarr.attrs.update(CF_NC_ATTRS)

    return xarr


def is_georeferenced(xarr, grid_mapping='crs', required_gdal=False):
    """ Determine if XArray data is georeferenced

    Parameters
    ----------
    xarr : xarray.DataArray or xarray.Dataset
        XArray data to check for georeferencing
    grid_mapping : str, optional
        Name to use for grid mapping variable
    require_gdal : bool, optional
        Require presence of attributes GDAL uses to georeference
        as backup ("spatial_ref" and "GeoTransform")

    Returns
    -------
    bool
        True if is georeferenced
    """
    assert isinstance(xarr, (xr.DataArray, xr.Dataset))

    cf_attrs = ('grid_mapping_name', )
    gdal_attrs = ('spatial_ref', 'GeoTransform', )
    gridmap_attrs = ('grid_mapping', )

    # Retrieve grid_mapping
    try:
        var_grid_mapping = get_grid_mapping(xarr)
    except KeyError as e:
        return False
    else:
        # Needs to have require information
        cf_ok = _check_georef(var_grid_mapping, cf_attrs)
        gdal_ok = _check_georef(var_grid_mapping, gdal_attrs)

        if not cf_ok:
            return False
        if not gdal_ok and require_gdal:
            return False

        if isinstance(xarr, xr.DataArray):
            if not _check_georef(xarr, gridmap_attrs):
                return False
        else:
            any_georef = False
            for name, dv in xarr.data_vars.items():
                if _check_georef(dv, gridmap_attrs):
                    any_georef = True
            if not any_georef:
                return False

        return True


def _georef(x, dim_x, dim_y, grid_mapping):
    if dim_x in x.dims and dim_y in x.dims:
        x.attrs['grid_mapping'] = grid_mapping
    else:
        logger.debug(f'Not georeferencing "{x.name}" because it lacks x/y '
                     f'dimensions ("{dim_x}" and "{dim_y}")')
    return x


def _check_georef(xarr, attrs):
    ok = [a in xarr.attrs for a in attrs]
    if not all(ok):
        quote = lambda s: f'"{s}"'
        missing = ", ".join([quote(a) for ok_, a in zip(ok, attrs) if not ok_])
        logger.debug('Cannot find required grid mapping attributes on '
                     f'"{xarr.name}": {missing}')
        return False
    return True


# =============================================================================
# Projection
def get_grid_mapping(xarr, grid_mapping='crs'):
    """ Return grid mapping variable

    Parameters
    ----------
    xarr : xarray.Dataset or xarray.DataArray
        XArray data

    Returns
    -------
    xarray.Variable
        XArray grid mapping variable

    Raises
    ------
    KeyError
        Raised if grid mapping variable does not exist
    """
    var_gm = xarr.coords.get(grid_mapping, None)
    if var_gm is None:
        raise KeyError('No grid mapping variable found')
    else:
        return var_gm


def create_grid_mapping(crs, transform, grid_mapping='crs'):
    """ Return an :py:class:`xarray.DataArray` of CF-compliant CRS info

    Parameters
    ----------
    crs : rasterio.crs.CRS
        Coordinate reference system information
    transform : affine.Affine
        Affine transform
    grid_mapping : str, optional
        Name of grid mapping variable. Defaults to 'crs'

    Returns
    -------
    xarray.DataArray
        "crs" variable holding CRS information
    """
    name = projections.cf_crs_name(crs)

    # This part is entirely unnecessary!
    try:
        epsg_code = projections.epsg_code(crs) or 0
    except ValueError as ve:
        logger.debug(f'Could not determine EPSG code for CRS ("{crs.wkt}")')
        epsg_code = 0
    else:
        if epsg_code:
            epsg_auth, epsg_code = epsg_code.split(':')
        epsg_code = np.array(int(epsg_code), dtype=np.int32)

    da = xr.DataArray(epsg_code, name=grid_mapping)
    da.attrs['grid_mapping_name'] = name

    da.attrs.update(projections.cf_crs_attrs(crs))
    da.attrs.update(projections.cf_proj_params(crs))
    da.attrs.update(projections.cf_ellps_params(crs))

    # TODO: enable turning this off? add other "compat_attrs"?
    # For GDAL in case CF doesn't work
    # http://www.gdal.org/frmt_netcdf.html
    for attr, value in _georeference_attrs_gdal(crs, transform).items():
        da.attrs[attr] = value

    # Fixup - every list/tuple should be np.ndarray to look like CRS variables
    # that have been written to disk (otherwise comparisons fail)
    for attr, value in da.attrs.items():
        if isinstance(value, (list, tuple)):
            da.attrs[attr] = np.asarray(value)

    return da


# =============================================================================
# Coordinates
def create_coordinates(y, x, crs):
    """ Return ``y`` and ``x`` as coordinates variables given the ``crs``

    Parameters
    ----------
    y : np.ndarray
        Y coordinate
    x : np.ndarray
        X coordinate
    crs : rasterio.crs.CRS
        Coordinate reference system of ``y`` and ``x``

    Returns
    -------
    xr.Variable : y_coord
        X coordinate
    xr.Variable : x_coord
        Y coordinate

    References
    ----------
    .. [1] http://cfconventions.org/cf-conventions/v1.6.0/cf-conventions.html#coordinate-types
    """
    x_var, y_var = projections.cf_xy_coord_names(crs)
    y_attrs = COORD_DEFS[y_var].copy()
    x_attrs = COORD_DEFS[x_var].copy()

    if crs.is_projected:
        crs_osr = utils.crs2osr(crs)
        units = crs_osr.GetLinearUnitsName().lower()
        y_attrs['units'], x_attrs['units'] = units, units

    y = xr.Variable((y_var, ), y, attrs=y_attrs)
    x = xr.Variable((x_var, ), x, attrs=x_attrs)

    return y, x


def _georeference_attrs_gdal(crs, transform):
    """ GDAL will look for these attributes if parsing CF fails
    """
    return OrderedDict((
        ('spatial_ref', crs.wkt),
        ('GeoTransform', transform.to_gdal())
    ))