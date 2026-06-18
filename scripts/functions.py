import geopandas as gpd
import os
import fiona
import rasterio.mask
from rasterio.fill import fillnodata
# from rasterstats import zonal_stats
import numpy as np
import tkinter as tk
from tkinter import filedialog, messagebox
import rasterio
import json
import pandas as pd
from osgeo import gdal, ogr, osr
import warnings
import alphashape
from shapely.geometry import Polygon, Point
from rasterio.mask import mask
from numba import jit
from rasterio.features import geometry_mask
from rasterio.windows import from_bounds, Window
import time
import rasterio
from rasterio.warp import calculate_default_transform
from rasterio.enums import Resampling
from exactextract import exact_extract
import gc
warnings.filterwarnings('ignore')

root = tk.Tk()
root.withdraw()
root.attributes("-topmost", True)


def calculate_degurba(gdf, density_raster):
    # Find grid cell size of resampled pop density raster
    with rasterio.open(density_raster) as src:
        # Read the source CRS and transform
        src_crs = src.crs
        src_transform = src.transform

        # Define the target CRS (EPSG:3395)
        dst_crs = 'EPSG:3395'

        # Calculate the transform and dimensions of the output raster
        dst_transform, width, height = calculate_default_transform(
            src_crs, dst_crs, src.width, src.height, *src.bounds)

    w_f = abs(dst_transform[0]) / 1000
    h_f = abs(dst_transform[4]) / 1000

    gdf['DensityKM'] = gdf['Density'] / w_f / h_f

    gdf.loc[(gdf['DensityKM'] < 50), 'DEGURBA'] = 'Very Low Density Rural'
    gdf.loc[(gdf['DensityKM'] >= 50), 'DEGURBA'] = 'Low Density Rural'
    gdf.loc[(gdf['DensityKM'] >= 1500) & (gdf['Population'] >= 50000), 'DEGURBA'] = 'Urban Centre'
    gdf.loc[(gdf['DensityKM'] >= 1500) & (gdf['Population'] >= 5000) & (
                gdf['Population'] < 50000), 'DEGURBA'] = 'Dense Urban Clusters'

    urban = gdf.loc[(gdf['DEGURBA'] == 'Urban Centre') | (gdf['DEGURBA'] == 'Dense Urban Clusters'), 'geometry']
    urban = urban.to_crs('epsg:3395')
    urban = urban.buffer(1000)
    urban = urban.to_crs('epsg:4326')

    gdf['Intersects'] = gdf.apply(lambda row: any(urban.intersects(row['geometry'])) if row['DensityKM'] > 300 else 0,
                                  axis=1)

    gdf.loc[(gdf['DensityKM'] >= 300) & (gdf['Intersects'] == 1) & (gdf['DEGURBA'] != 'Urban Centre')
            & (gdf['DEGURBA'] != 'Dense Urban Clusters'), 'DEGURBA'] = 'Suburban or Peri Urban'

    gdf.loc[(gdf['DensityKM'] >= 300) & (gdf['DensityKM'] < 1500) &
            (gdf['Population'] >= 5000), 'DEGURBA'] = 'Semi Dense Urban Clusters'

    gdf.loc[(gdf['DensityKM'] >= 300) & (gdf['Population'] >= 500) &
            (gdf['Population'] < 5000) & (gdf['DEGURBA'] != 'Suburban or Peri Urban'), 'DEGURBA'] = 'Rural Clusters'

    del gdf['Intersects']
    del gdf['Density']

    return gdf


import rasterio

def zonal_stats_exact(raster_path, gdf, method='sum', name='_'):

    with rasterio.open(raster_path) as raster:

        gdf = gdf.sort_values(by=['id'])

        results = exact_extract(
            raster,
            gdf,
            f"{name}={method}(coverage_weight=none)",
            include_cols='id',
            output='pandas'
        )

        results = results.sort_values(by=['id'])
        gdf[name] = results[name]

    return gdf


def zonal_stat(raster_path, gdf, method='sum', name='_'):
    gdf.sort_index(inplace=True)

    # Open raster data
    with rasterio.open(raster_path) as src:
        transform = src.transform
        nodata = src.nodata

        # Initialize an array to hold sums for each polygon
        stats = np.zeros(len(gdf))

        # Iterate over each polygon and calculate the sum of raster values
        for idx, geom in gdf.iterrows():
            try:
                # Get the bounding box of the geometry
                minx, miny, maxx, maxy = geom['geometry'].bounds
                # Define the window to read
                window = from_bounds(minx, miny, maxx, maxy, transform=transform)
                if (window.width < 1) or (window.height < 1):
                    window = Window(col_off=window.col_off, row_off=window.row_off, width=1, height=1)
                # Read the data in the window
                data = src.read(1, window=window)

                # Adjust the transform for the window
                window_transform = src.window_transform(window)

                # Create a mask for the current polygon
                mask = geometry_mask([geom['geometry']], transform=window_transform,
                                     invert=True, all_touched=True, out_shape=data.shape)

                # Calculate the sum of values within the mask
                # sums[idx] = calculate_sum(data, mask, nodata)
                data = np.where(data == nodata, 0, data)
                data = np.where(mask, data, 0)
                if method == 'sum':
                    stats[idx] = np.nansum(data)
                elif method == 'max':
                    stats[idx] = np.nanmax(data)
                elif method == 'mean':
                    stats[idx] = np.nansum(data) / mask.sum()

            except:
                stats[idx] = np.nan
                # Add the results to the GeoDataFrame

    gdf[name] = stats

    return gdf


def clipRasterByExtent(output, raster, polygon, nodata):
    """
    Clipping a raster to the extent of a polygon layer

    Parameters
    ----------
    arg1 : output
        The path and file name of the clipped raster
    arg2 : raster
        Raster dataset to clip
    arg3 : polygon
        Polygon layer to clip by
    arg4 : nodata
        Value to be used as nodata in the clipped raster

    Returns
    ----------
    Two clipped raster layers. The layers are the same, but one of them is compatible with rasterio
    while the other one is compatible with GDAL
    """

    bbox = polygon.total_bounds
    bbox2 = [bbox[0], bbox[3], bbox[2], bbox[1]]
    gdal.Translate(output, raster, projWin=bbox2, noData=nodata)

    return output


def reclassifyRasters(raster_path, threshold):
    """
    Reclassify raster values:
    - values <= threshold -> 0
    - values > threshold and < 99999 -> 1

    Returns a GDAL in-memory raster dataset.
    """

    raster = gdal.Open(raster_path)
    if raster is None:
        raise ValueError(f"Cannot open raster: {raster_path}")

    band = raster.GetRasterBand(1)
    array = band.ReadAsArray()

    # Reclassification
    array = np.where(array <= threshold, 0, array)
    array = np.where((array > threshold) & (array < 99999), 1, array)

    driver = gdal.GetDriverByName("MEM")
    out = driver.Create(
        "",
        raster.RasterXSize,
        raster.RasterYSize,
        1,
        gdal.GDT_Float32
    )

    out.GetRasterBand(1).WriteArray(array)

    # Copy georeferencing
    out.SetProjection(raster.GetProjection())
    out.SetGeoTransform(raster.GetGeoTransform())

    out.FlushCache()

    return out


def resampleRaster(raster, factor, method="mode"):
    """
    Resample rasters by a specified factor

    Parameters
    ----------
    arg1 : raster
        Raster dataset to resample.
    arg2 : factor
        Factor used for the resampling.

    Returns
    ----------
    Resampled raster layer with the each side of the pixels being a specified factor larger than the original pixel
    """

    gt = raster.GetGeoTransform()
    xRes = factor * gt[1]
    yRes = factor * gt[1]
    kwargs1 = {'noData': '0'}
    resamp1 = gdal.Translate('', raster, format='MEM', **kwargs1)
    kwargs2 = {'xRes': xRes, 'yRes': yRes, 'resampleAlg': method}
    resampled = gdal.Translate('', resamp1, format='MEM', noData=0, **kwargs2)
    return resampled

def resample_raster_sum(input_raster_path, output_raster_path, scale_factor):
    with rasterio.open(input_raster_path) as src:
        # Read the input raster data
        input_data = src.read(1)  # Read the first band

        input_data = np.where(input_data == src.meta['nodata'], 0, input_data)

        # Calculate new dimensions
        input_height, input_width = input_data.shape
        output_height = input_height // scale_factor
        output_width = input_width // scale_factor

        # Initialize the output array
        output_data = np.zeros((output_height, output_width), dtype=np.float32)

        # Resample using sum method
        for i in range(output_height):
            for j in range(output_width):
                output_data[i, j] = np.sum(
                    input_data[
                        i * scale_factor:(i + 1) * scale_factor,
                        j * scale_factor:(j + 1) * scale_factor
                    ]
                )

        # Update the metadata
        transform = src.transform
        new_transform = rasterio.transform.Affine(
            transform.a * scale_factor,
            transform.b,
            transform.c,
            transform.d,
            transform.e * scale_factor,
            transform.f
        )
        new_meta = src.meta.copy()
        new_meta.update({
            "height": output_height,
            "width": output_width,
            "transform": new_transform,
            "dtype": 'float32'
        })

        # Write the resampled data to a new file
        with rasterio.open(output_raster_path, 'w', **new_meta) as dst:
            dst.write(output_data, 1)

        return input_data


def rasterize(vector, vector_path, raster, output):
    """
    Converts vector layer to raster.

    Parameters
    ----------
    arg1 : vector
        Vector layer to convert to raster
    arg2 : vector_path
        The path of the vector layer that you is to be converted
    arg3 : raster
        Raster to use as template for the rasterization
    arg4 : output
        Path to the output raster

    Returns
    ----------
    Rasterized vector layer
    """

    vector["id"] = np.arange(len(vector)) + 1
    vector.to_file(vector_path)

    geo_transform = raster.GetGeoTransform()
    x_min = geo_transform[0]
    y_max = geo_transform[3]
    x_max = x_min + geo_transform[1] * raster.RasterXSize
    y_min = y_max + geo_transform[5] * raster.RasterYSize
    x_res = raster.RasterXSize
    y_res = raster.RasterYSize
    mb_v = ogr.Open(vector_path)
    mb_l = mb_v.GetLayer()
    pixel_width = geo_transform[1]
    target_ds = gdal.GetDriverByName('GTiff').Create(output, x_res, y_res, 1, gdal.GDT_Byte)
    target_ds.SetGeoTransform((x_min, pixel_width, 0, y_max, 0, -pixel_width))
    target_dsSRS = osr.SpatialReference()
    target_dsSRS.ImportFromEPSG(4326)
    target_ds.SetProjection(target_dsSRS.ExportToWkt())
    band = target_ds.GetRasterBand(1)
    NoData_value = -999999
    band.SetNoDataValue(NoData_value)
    band.FlushCache()
    gdal.RasterizeLayer(target_ds, [1], mb_l, options=["ATTRIBUTE=id"])
    target_ds = None


def rasterMultiplication(rstpth1, rstpth2, output, filetype=gdal.GDT_Float32):
    """
    Multiplies raster layers.

    Parameters
    ----------
    arg1 : rstpth1
        Path of the first raster
    arg2 : rstpth2
        Path of the second raster
    arg3 : output
        Path to the output raster
    arg4 : filetype
        Raster filetype

    Returns
    ----------
    Rasterproduct of two rasters
    """

    rst1 = gdal.Open(rstpth1)
    band_data1 = rst1.GetRasterBand(1)
    a = band_data1.ReadAsArray()

    rst2 = gdal.Open(rstpth2)
    band_data2 = rst2.GetRasterBand(1)
    b = band_data2.ReadAsArray()

    f = b * a

    ref = gdal.Open(rstpth1)
    band = ref.GetRasterBand(1)
    proj = ref.GetProjection()
    geotransform = ref.GetGeoTransform()
    xsize = band.XSize
    ysize = band.YSize

    driver = gdal.GetDriverByName('GTiff')
    out = driver.Create(output, xsize, ysize, 1, filetype)
    out.GetRasterBand(1).WriteArray(f)

    out.SetProjection(proj)
    out.SetGeoTransform(geotransform)
    out.FlushCache()
    out = None
    x = gdal.Open(output)

    return x

def finished():
    print('Clusters created and saved in the data/outputs folder!')


def calibrateUrban(clusters, urban_current, workspace):
    """
    Calibrate urban population. Classifies clusters to either urban(2), peri-urban(1) or rural(0).

    Parameters
    ----------
    arg1 : clusters
        Population clusters with population column
    arg2 : urban_current
        Urban ration defined by the user
    arg3 : workspace
        Output folder in which the clusters as saved after the urban classification

    Returns
    ----------
    Population clusters with an ubran-rural classifcation column
    """

    urban_modelled = 2
    factor = 1
    pop_tot = clusters["Population"].sum()
    i = 0
    while abs(urban_modelled - urban_current) > 0.01:
        clusters["IsUrban"] = 0
        clusters.loc[(clusters["Population"] > 5000 * factor) & (
                clusters["Population"] / clusters["Area"] > 300 * factor), "IsUrban"] = 1
        clusters.loc[(clusters["Population"] > 50000 * factor) & (
                clusters["Population"] / clusters["Area"] > 1500 * factor), "IsUrban"] = 2
        pop_urb = clusters.loc[clusters["IsUrban"] > 1, "Population"].sum()

        urban_modelled = pop_urb / pop_tot

        if urban_modelled > urban_current:
            factor *= 1.1
        else:
            factor *= 0.9
        i = i + 1
        if i > 500:
            break
            print(i)

    clusters.to_file(workspace + r"/clusters.shp")

    print("Modelled urban ratio is " + str(round(urban_modelled, 3)) + "% in comparision to the actual ratio of " + str(
        urban_current) + "% after " + str(i) + " iterations.")


def saveRaster(input_file, output_file):
    """
    Saving memory raster.

    Parameters
    ----------
    arg1 : input_file
        Memory raster to save to disc
    arg2 : output_file
        The path to the save raster
    """

    kwargs = {'creationOptions': ['COMPRESS=LZW']}
    gdal.Warp(output_file, input_file, **kwargs)


def toPolygon(Raster, output):
    """
    Polygonizes a raster layer.

    Parameters
    ----------
    arg1 : Raster
        Raster to convert to polygon
    arg2 : opt
        Options that will add an buffer to the NTL polygon
    arg3 : crs
        Path to the polygon output

    Returns
    ----------
    Polygonized raster
    """

    if type(Raster) == str:
        Raster = gdal.Open(Raster)

    band = Raster.GetRasterBand(1)
    bandArray = band.ReadAsArray()

    outShapefile = output

    driver = ogr.GetDriverByName("ESRI Shapefile")
    if os.path.exists(outShapefile + ".shp"):
        driver.DeleteDataSource(outShapefile + ".shp")
    outDatasource = driver.CreateDataSource(outShapefile + ".shp")

    spat_ref = osr.SpatialReference()
    proj = Raster.GetProjectionRef()
    spat_ref.ImportFromWkt(proj)

    outLayer = outDatasource.CreateLayer(outShapefile + ".shp", srs=spat_ref)
    newField = ogr.FieldDefn('PLACEHOLDE', ogr.OFTInteger)
    outLayer.CreateField(newField)

    gdal.Polygonize(band, band, outLayer, 0, ["GROUPBY=PLACEHOLDE"], callback=None)
    outDatasource.Destroy()
    sourceRaster = None

    out = gpd.read_file(outShapefile + ".shp")

    return out


def clipRasterByMask(raster_path, mask_path, crs, output):
    """
    Clipping raster by a polygon mask.

    Parameters
    ----------
    arg1 : raster_path
        Raster to clip
    arg2 : mask_path
        Polygon vector to clip by
    arg3 : crs
        Coordinate reference system of the clipped raster
    arg4 : output
        Output raster path for clipped raster

    Returns
    ----------
    Clipped raster dataset
    """

    with fiona.open(mask_path, "r") as shapefile:
        shapes = [feature["geometry"] for feature in shapefile]
    with rasterio.open(raster_path) as src:
        out_image, out_transform = rasterio.mask.mask(src, shapes, crop=True)
        out_image[out_image < 0] = np.nan
        mask = (out_image != 0)
        out_meta = src.meta
    out_meta.update({"driver": "GTiff",
                     "height": out_image.shape[1],
                     "width": out_image.shape[2],
                     "transform": out_transform,
                     "crs": crs})

    out_meta.update(compress='lzw')

    with rasterio.open(output, "w", **out_meta) as dest:
        dest.write(out_image)
    out = rasterio.open(output)
    return out, output


def addAttributes(clusters, crs, study_area):
    """
    Adds country name and area to the clusters

    Parameters
    ----------
    arg1 : clusters
        Population clusters
    arg2 : crs
        User selected coordinate reference system to reporject the clusters to
    arg3 : country
        Study area

    Returns
    ----------
    Population clusters with a study area column and an area column given in square kilometers
    """



    clusters['id'] = np.arange(len(clusters))
    clusters.crs = {'init': 'epsg:4326'}
    clusters_proj = clusters.to_crs({'init': crs})
    clusters_proj["Area"] = clusters_proj.area / 1000000
    clusters_proj["Country"] = study_area
    clusters = clusters_proj.to_crs({'init': 'epsg:4326'})
    clusters = clusters.drop(['PLACEHOLDE'], axis=1)
    return clusters


def populatingClusters(clusters, raster, column, method):
    """
    Adding raster data to clusters

    Parameters
    ----------
    arg1 : clusters
        Population clusters
    arg2 : raster
        Raster to add the clusters
    arg3 : column
        Name of column with added raster statitics
    arg4 : method
        Method used in order to aggregate the raster data for each raster

    Returns
    ----------
    Population clusters with raster statistics added
    """

    clusters = zonal_stats(
        clusters,
        raster.name,
        stats=[method],
        prefix=column, geojson_out=True, all_touched=True)

    return clusters


def calibratePop(clusters, workspace, act_pop):
    """
    Adding raster data to clusters

    Parameters
    ----------
    arg1 : clusters
        Population clusters
    arg2 : workspace
        Output path for the output clusters
    arg3 : act_pop
        The actual population in the study area given by the user

    Returns
    ----------
    Final population clusters
    """

    output = workspace + r'\placeholder.geojson'
    with open(output, "w") as dst:
        collection = {
            "type": "FeatureCollection",
            "features": list(clusters)}
        dst.write(json.dumps(collection))

    clusters = gpd.read_file(output)
    os.remove(output)

    clusters = clusters.rename(columns={"Popsum": "Population"})
    clusters = clusters.rename(columns={"NTLmax": "NightLight"})
    clusters = clusters.rename(columns={"ElecPopsum": "ElecPop"})

    clusters["Population"].fillna(0, inplace=True)
    clusters["NightLight"].fillna(0, inplace=True)
    clusters["ElecPop"].fillna(0, inplace=True)
    pop_tot = clusters["Population"].sum()

    ratio = act_pop / pop_tot
    clusters["Population"] = clusters["Population"] * ratio
    clusters["ElecPop"] = clusters["ElecPop"] * ratio
    clusters_filter = clusters[clusters['Population'] > 0]
    clusters_filter.loc[clusters_filter["NightLight"] == 0, ["ElecPop"]] = 0

    clusters_filter.to_file(workspace + r"\clusters.shp")
    clusters = gpd.read_file(workspace + r"\clusters.shp")

    return clusters


def beautyfing_cluster_single(gep_cluster, spacing=25, alpha=0.01):
    """
    Refines a single GeoPandas polygon (cluster) to create a visually smoother representation.

    Args:
        gep_cluster (GeoPandas GeoDataFrame): The input cluster polygon.
        spacing (float, optional): The spacing between grid points used for alpha shape creation. Defaults to 25.
        alpha (float, optional): The alpha parameter for the alpha shape algorithm, controlling the level of simplification. Defaults to 0.01.

    Returns:
        GeoPandas GeoDataFrame: A new GeoDataFrame containing the refined polygon as a single geometry.

    Exceptions:
         If the original cluster has a minimum width or height less than or equal to 105 meters, its geometry will not be processed.
    """

    minx, miny, maxx, maxy = gep_cluster.total_bounds

    gep_cluster_no_index = gep_cluster.reset_index()

    min_width = maxx - minx
    min_height = maxy - miny

    if min_width <= 105 or min_height <= 105:
        # Copy all columns except index to alphashape_gpd_new
        alphashape_gpd_new = gep_cluster.iloc[:, 0:]  # Select all rows and columns from 1 onwards (excluding index)
        alphashape_gpd_new.crs = gep_cluster.crs  # Copy CRS information

        return alphashape_gpd_new

    x_points = np.arange(minx, maxx, spacing)
    y_points = np.arange(miny, maxy, spacing)
    grid_points = np.array([Point(x, y) for x in x_points for y in y_points])

    list_points = []
    for points_par in grid_points:
        list_points.append(Point(points_par))

    grid_points_gpd = gpd.GeoDataFrame(geometry=grid_points, crs=gep_cluster.crs)

    points_in_polygons = gpd.clip(grid_points_gpd, gep_cluster)

    points_in_polygons_list = points_in_polygons.geometry.to_list()

    puntos_en_poligono = []
    for puntos in points_in_polygons_list:
        puntos_en_poligono.append([puntos.x, puntos.y])

    concave_hull = alphashape.alphashape(puntos_en_poligono, alpha=alpha)

    alphashape_gpd = gpd.GeoDataFrame(geometry=[concave_hull], crs=gep_cluster.crs)

    return alphashape_gpd


def beautyfing_clusters(gep_clusters, distance, alpha, crs):
    """
    Iterates a collection of GeoPandas polygons (clusters) to create visually smoother representations.

    Args:
        gep_clusters (GeoPandas GeoDataFrame): The input GeoDataFrame containing cluster polygons.
        distance (float): The spacing between grid points used for alpha shape creation in each cluster.
        alpha (float): The alpha parameter for the alpha shape algorithm, controlling the level of simplification in each cluster.
        crs (str): The target Coordinate Reference System (CRS) for the output GeoDataFrame.

    Returns:
        GeoPandas GeoDataFrame: A new GeoDataFrame containing refined cluster polygons as individual geometries.

    """

    gep_clusters_crs = gep_clusters.to_crs(crs)

    new_GEP_Clusters = gpd.GeoDataFrame(geometry=[], crs=gep_clusters_crs.crs)

    for index, row in gep_clusters_crs.iterrows():
        single_gpd = gpd.GeoDataFrame([row], columns=row.index)
        single_gpd.crs = gep_clusters_crs.crs

        new_alpha = beautyfing_cluster_single(single_gpd, distance, alpha)

        new_GEP_Clusters = pd.concat([new_GEP_Clusters, new_alpha])

    output_partial = new_GEP_Clusters.dissolve()

    output_final = output_partial.explode(index_parts=False).to_crs({'init': 'epsg:4326'})

    output_final['PLACEHOLDE'] = 1

    output_final = output_final.reset_index()
    del output_final['index']

    return output_final


import numpy as np
import geopandas as gpd
import pandas as pd
from shapely.geometry import Point
from shapely.ops import unary_union
import alphashape


def sample_points_in_polygon(polygon, spacing):
    """Efficiently sample grid points within a polygon."""
    minx, miny, maxx, maxy = polygon.bounds
    x_coords = np.arange(minx, maxx, spacing)
    y_coords = np.arange(miny, maxy, spacing)

    xv, yv = np.meshgrid(x_coords, y_coords)
    coords = np.vstack((xv.ravel(), yv.ravel())).T

    # Only keep points within the polygon
    points = [Point(x, y) for x, y in coords if polygon.contains(Point(x, y))]
    return points


def beautyfing_cluster_single_fast(geom, spacing=25, alpha=0.01):
    bounds = geom.bounds
    if (bounds[2] - bounds[0]) <= 105 or (bounds[3] - bounds[1]) <= 105:
        return geom  # Skip small polygons

    points = sample_points_in_polygon(geom, spacing)
    if len(points) < 4:
        return geom  # Not enough points for an alpha shape

    coords = [(p.x, p.y) for p in points]
    try:
        alpha_shape = alphashape.alphashape(coords, alpha)
    except:
        return geom

    return alpha_shape


def beautify_clusters_fast(gep_clusters, distance, alpha, crs):
    # Project to target CRS
    gep_clusters = gep_clusters.to_crs(crs)

    new_geoms = []
    for geom in gep_clusters.geometry.values:
        new_geom = beautyfing_cluster_single_fast(geom, distance, alpha)
        new_geoms.append(new_geom)

    output_gdf = gpd.GeoDataFrame(geometry=new_geoms, crs=gep_clusters.crs)
    output_gdf = output_gdf.dissolve().explode(index_parts=False).to_crs(epsg=4326)
    output_gdf["PLACEHOLDE"] = 1
    output_gdf = output_gdf.reset_index(drop=True)
    return output_gdf
