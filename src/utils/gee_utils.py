"""Google Earth Engine utility functions."""

# pylint: disable=no-member

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import ee
from google.cloud import storage
from google.cloud.exceptions import NotFound

from src.utils.log_utils import setup_logger


def get_ic(
    product: str,
    date_start: Optional[str] = None,
    date_end: Optional[str] = None,
    bands: Optional[list] = None,
    bounds: Optional[ee.Geometry] = None,
) -> ee.ImageCollection:
    """
    Get an ee.ImageCollection.

    Args:
        product (str): The name of the product.
        date_start (str, optional): The start date in 'YYYY-MM-DD' format. Defaults to None.
        date_end (str, optional): The end date in 'YYYY-MM-DD' format. Defaults to None.
        bands (list, optional): A list of band names to select. Defaults to None.
        bounds (ee.Geometry, optional): The bounds to filter the image collection. Defaults to None.

    Returns:
        ee.ImageCollection: The filtered ee.ImageCollection.
    """
    ic = ee.ImageCollection(product)

    if date_start is not None:
        ic = ic.filterDate(date_start, date_end)

    if bounds is not None:
        ic = ic.filterBounds(bounds)

    if bands is not None:
        ic = ic.select(bands)

    return ic


def bitwise_extract(
    image: ee.Image, from_bit: int, to_bit: Optional[int] = None
) -> ee.Image:
    """Performs bitwise extraction for masking images from QA bands

    Args:
        image (ee.Image): Single-band image (QA Band) with n-bit values
        from_bit (int): Position of starting bit
        to_bit (int, optional): Position of ending bit (if values span multiple bits).
        Defaults to None.

    Returns:
        ee.Image
    """
    if to_bit is None:
        to_bit = from_bit
    mask_size = ee.Number(1).add(to_bit).subtract(from_bit)
    mask = ee.Number(1).leftShift(mask_size).subtract(1)
    return image.rightShift(from_bit).bitwiseAnd(mask)


def mask_clouds(
    ic: ee.ImageCollection, qa_band: str = "state_1km"
) -> ee.ImageCollection:
    """Masks cloudy (or cloud-shadowed) pixels of MODIS Terra Land Reflectance images
    contained within ImageCollection

    Args:
        ic (ee.ImageCollection): ImageCollection to be masked
        qa_band (str): QA band name

    Returns:
        ee.ImageCollection: Masked image collection
    """

    def _mask_clouds(image: ee.Image) -> ee.Image:
        qa = image.select(qa_band)
        cloud_mask = bitwise_extract(qa, 0, 1).eq(0).Or(bitwise_extract(qa, 0, 1).eq(3))
        internal_cloud_mask = bitwise_extract(qa, 10).eq(0)
        mask = internal_cloud_mask.And(cloud_mask)
        image_masked = image.updateMask(mask)

        return image_masked

    return ic.map(_mask_clouds)


def calculate_monthly_averages(ic: ee.ImageCollection, year_start: str, year_end: str):
    """
    Calculate monthly averages for each band in an ImageCollection.

    Parameters:
        ic (ee.ImageCollection): The input image collection.
        year_start (str): The starting year for calculating monthly averages.
        year_end (str): The ending year for calculating monthly averages.

    Returns:
        ee.ImageCollection: The image collection containing monthly average images for each band.
    """

    months = range(1, 13)

    # Get the first image in the collection to retrieve band names
    first_image = ee.Image(ic.first())
    bands = first_image.bandNames().getInfo()

    monthly_averages = []

    for month in months:
        for band in bands:
            bn = f"{band}_{year_start}-{year_end}_m{month}_mean"
            mean_image = (
                ic.filter(ee.Filter.calendarRange(month, month, "month"))
                .select(band)
                .mean()
                .rename(bn)
            )
            monthly_averages.append(mean_image)

    return ee.ImageCollection(monthly_averages)


@dataclass
class ExportParams:
    """
    Represents the parameters for exporting data in Google Earth Engine.

    Attributes:
        crs (str): The coordinate reference system (CRS) for the exported data. Default
            is "EPSG:4326".
        scale (int): The scale of the exported data. Default is 1000.
        target (str): The target destination for the exported data. Default is "gcs".
        folder (str): The folder name for storing the exported data. Default is "gee_exports".
    """

    crs: str = "EPSG:4326"
    scale: int = 1000
    target: str = "gcs"
    folder: str = "gee_exports"
    nodata: Optional[int] = None

    def __post_init__(self):
        if self.target not in ["gcs", "gdrive"]:
            raise ValueError("Invalid target. Use 'gcs' or 'gdrive'.")


def validate_bucket_and_create_if_not_exists(bucket_id: str) -> storage.Bucket:
    """
    Validate a Google Cloud Storage bucket and create it if it does not exist.

    Args:
        bucket_id (str): The ID of the Google Cloud Storage bucket.

    Returns:
        storage.Bucket: The Google Cloud Storage bucket.
    """
    log = setup_logger(__name__)
    storage_client = storage.Client()

    log.info("Getting bucket %s...", bucket_id)
    try:
        bucket = storage_client.get_bucket(bucket_id)
    except NotFound:
        log.info("Bucket %s not found. Creating...", bucket_id)
        bucket = storage_client.create_bucket(bucket_id)

    return bucket


def export_image(
    image: ee.Image, filename: str, export_params: ExportParams
) -> ee.batch.Task:
    """Export an image to Drive or Google Cloud Storage.

    Args:
        image (ee.Image): Image to export.
        filename (str): Filename to be used as prefix of exported file and name of task.
        export_params (ExportParams): Export parameters including destination folder,
            projection, and scale.

    Returns:
        ee.batch.Task: The export task that was started.
    """
    task_config = {
        "description": filename,
        "fileNamePrefix": filename,
        "crs": export_params.crs,
        "fileFormat": "GeoTIFF",
        "formatOptions": {"cloudOptimized": True, "noData": export_params.nodata},
        "maxPixels": 1e13,
        "scale": export_params.scale,
        "skipEmptyTiles": True,
    }

    if export_params.target == "gcs":
        validate_bucket_and_create_if_not_exists(export_params.folder)
        task_config["bucket"] = export_params.folder
        task = ee.batch.Export.image.toCloudStorage(image, **task_config)
        task.start()

    else:
        task_config["folder"] = export_params.folder
        task = ee.batch.Export.image.toDrive(image, **task_config)
        task.start()

    return task


def export_collection(
    collection: ee.ImageCollection,
    export_params: ExportParams,
    test: bool = False,
) -> list[ee.batch.Task]:
    """Export an ImageCollection to Drive

    Args:
        collection (ee.ImageCollection): ImageCollection to be exported
        export_params (ExportParams): Export parameters specifying the destination folder,
            projection, and scale
        test (bool, optional): If True, only exports the first image in the collection
            for testing purposes. Defaults to False.

    Returns:
        list[ee.batch.Task]: List of export tasks for each image in the collection
    """
    num_images = int(collection.size().getInfo())
    image_list = collection.toList(num_images)

    tasks = []
    for i in range(num_images if not test else 1):
        image = ee.Image(image_list.get(i))
        out_name = f"{image.bandNames().getInfo()[0]}"
        print(f"Exporting {out_name} with scale {export_params.scale} m")
        task = export_image(image, out_name, export_params)
        tasks.append(task)

    return tasks


def download_when_complete(
    bucket_id: str,
    out_dir: str | os.PathLike,
    tasks: Optional[list[ee.batch.Task]] = None,
    verbose: bool = False,
) -> None:
    """
    Downloads files from a Google Cloud Storage bucket when they are marked as completed.

    Args:
        bucket_id (str): The ID of the Google Cloud Storage bucket.
        out_dir (str | os.PathLike): The directory where the downloaded files will be saved.
        tasks (Optional[list[ee.batch.Task]]): The list of tasks to monitor for completion.
            If not provided, it will retrieve the task list from Earth Engine.
        verbose (bool, optional): Whether to enable verbose logging. Defaults to False.

    Returns:
        None
    """
    log = setup_logger(__name__)
    if verbose:
        log.setLevel(logging.INFO)

    if tasks is None:
        log.info("No tasks provided. Getting task list...")
        tasks = ee.batch.Task.list()

    log.info("Connecting to Google Cloud Storage...")
    storage_client = storage.Client()

    log.info("Getting bucket %s...", bucket_id)
    bucket = storage_client.get_bucket(bucket_id)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    log.info("Checking for completed tasks...")
    while tasks:
        for task in tasks.copy():
            status = task.status()

            if status["state"] == "COMPLETED":
                file_name = status["description"] + ".tif"
                local_file_path = Path(out_dir) / file_name

                blob = bucket.blob(file_name)

                if blob.exists():
                    if not local_file_path.exists():
                        log.info("Downloading %s... to %s", file_name, str(out_dir))
                        blob.download_to_filename(str(local_file_path))
                    else:
                        log.info(
                            "File %s already exists at %s", file_name, str(out_dir)
                        )
                else:
                    log.warning("File %s not found in bucket.", file_name)

                tasks.remove(task)

            elif status["state"] == "FAILED":
                log.error("Task %s failed: %s", task.id, status["error_message"])
                tasks.remove(task)
        if tasks:
            time.sleep(60)

    log.info("All tasks and downloads completed.")