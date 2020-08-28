from pathlib import Path
import geopandas as gpd
import numpy as np
import openeo
import pandas as pd
import scipy.signal
import shapely
from openeo import Job
from openeo.rest.conversions import timeseries_json_to_pandas
import ee
import statistics
from datetime import timedelta

from openeo.rest.job import RESTJob
from tensorflow.keras.models import load_model
import geojson
import uuid
import json

# General approach:
#
# first merge all required inputs into a single multiband raster datacube
# compute timeseries for one or more fields, for all bands (one step)
# do postprocessing of the timeseries:
#   compute cropsar based on cleaned fapar + sigma0
#   combine cropsar + coherence to determine cropcalendar
#   return cropcalendar output in your own json format

class Cropcalendars():
    def __init__(self, fAPAR_rescale_Openeo, coherence_rescale_Openeo, path_harvest_model,VH_VV_range_normalization, fAPAR_range_normalization, metrics_order):
        # crop calendar independant variables
        self.fAPAR_rescale_Openeo = fAPAR_rescale_Openeo
        self.coherence_rescale_Openeo = coherence_rescale_Openeo
        self.path_harvest_model = path_harvest_model
        self.VH_VV_range_normalization = VH_VV_range_normalization
        self.fAPAR_range_normalization  = fAPAR_range_normalization
        self.metrics_order = metrics_order

    def get_resource(self,relative_path):
        return str(Path(relative_path))

    def load_udf(self, relative_path):
        with open(self.get_resource(relative_path), 'r+', encoding="utf8") as f:
            return f.read()

    def generate_cropcalendars(self, start, end, gjson_path, window_values, thr_detection, crop_calendar_event, metrics_crop_event):
            ##### FUNCTION TO BUILD A DATACUBE IN OPENEO

            def makekernel(size: int) -> np.ndarray:
                assert size % 2 == 1
                kernel_vect = scipy.signal.windows.gaussian(size, std=size / 3.0, sym=True)
                kernel = np.outer(kernel_vect, kernel_vect)
                kernel = kernel / kernel.sum()
                return kernel

            ## cropsar masking function, probably still needs an update!
            def create_advanced_mask(band, startdate, enddate, band_math_workaround=True):
                # in openEO, 1 means mask (remove pixel) 0 means keep pixel
                classification = band

                # keep useful pixels, so set to 1 (remove) if smaller than threshold
                first_mask = ~ ((classification == 4) | (classification == 5) | (classification == 6) | (classification == 7))
                first_mask = first_mask.apply_kernel(makekernel(17))
                # remove pixels smaller than threshold, so pixels with a lot of neighbouring good pixels are retained?
                if band_math_workaround:
                    first_mask = first_mask.add_dimension("bands", "mask", type="bands").band("mask")
                first_mask = first_mask > 0.057

                # remove cloud pixels so set to 1 (remove) if larger than threshold
                second_mask = (classification == 3) | (classification == 8) | (classification == 9) | (classification == 10)
                second_mask = second_mask.apply_kernel(makekernel(161))
                if band_math_workaround:
                    second_mask = second_mask.add_dimension("bands", "mask", type="bands").band("mask")
                second_mask = second_mask > 0.1

                # TODO: the use of filter_temporal is a trick to make cube merging work, needs to be fixed in openeo client
                return first_mask.filter_temporal(startdate, enddate) | second_mask.filter_temporal(startdate, enddate)
                # return first_mask | second_mask
                # return first_mask

            def get_bands(startdate,enddate):
                eoconn=openeo.connect('http://openeo-dev.vgt.vito.be/openeo/1.0.0/')
                eoconn.authenticate_basic('bontek','bontek123')

                S2mask=create_advanced_mask(eoconn.load_collection('TERRASCOPE_S2_TOC_V2',bands=['SCENECLASSIFICATION_20M']).band('SCENECLASSIFICATION_20M'),startdate=startdate,enddate=enddate)
                fapar = eoconn.load_collection('TERRASCOPE_S2_FAPAR_V2')

                fapar_masked=fapar.mask(S2mask)

                gamma0=eoconn.load_collection('TERRASCOPE_S1_GAMMA0_V1')
                sigma_ascending = eoconn.load_collection('S1_GRD_SIGMA0_ASCENDING')
                sigma_descending = eoconn.load_collection('S1_GRD_SIGMA0_DESCENDING')

                coherence=eoconn.load_collection('TERRASCOPE_S1_SLC_COHERENCE_V1')

                all_bands = gamma0.merge(sigma_ascending).merge(sigma_descending).merge(fapar_masked)#.merge(coherence)
                return all_bands

            def GEE_RO_retrieval(gj, i):
                #### GEE part to find the available RO per orbit pass
                ee.Initialize()
                # Import the collections
                sentinel1 = ee.ImageCollection("COPERNICUS/S1_GRD")
                collection = ee.FeatureCollection(
                    [ee.Feature(
                        ee.Geometry.Polygon(
                            [gj.features[i].geometry.coordinates[0]
                             ]
                        ), {'ID': '{}'.format(gj.features[i].properties['id'])}
                    )]
                )
                filter_field = collection.filter(ee.Filter.eq('ID', '{}'.format(gj.features[i].properties['id'])))

                try:
                    ###############################################################################
                    # PROCESSING SENTINEL 1
                    ###############################################################################
                    dict_metadata_ascending = dict()
                    dict_metadata_descending = dict()
                    for mode in ['ASCENDING', 'DESCENDING']:
                        print('Extracting Sentinel-1 data in %s mode for %s' % (mode,gj.features[i].properties['id'] ))
                        # Filter S1 by metadata properties.
                        sentinel1_filtered = sentinel1.filterBounds(filter_field.geometry().bounds()).filterDate(
                            start, end) \
                            .filter(ee.Filter.eq('orbitProperties_pass', mode)) \
                            .filter(ee.Filter.eq('instrumentMode', 'IW')) \
                            .filter(ee.Filter.listContains('transmitterReceiverPolarisation', 'VV')) \
                            .filter(ee.Filter.listContains('transmitterReceiverPolarisation', 'VH'))

                        sentinel1_collection_contents = ee.List(sentinel1_filtered).getInfo()
                        current_nr_files = len(sentinel1_collection_contents['features'])

                        print('{} Sentinel-1 images match the request ...'.format(current_nr_files))
                        for img_nr in range(current_nr_files):
                            current_sentinel_img_id = str(sentinel1_collection_contents['features'][img_nr]['id'])

                            # if want to know the incidence angle for the field
                            # current_sentinel_img = ee.Image(current_sentinel_img_id)
                            # current_sentinel_img.clip(filter_field.geometry()).reduceRegion(ee.Reducer.mean()).getInfo()['angle']

                            if 'S1A' in current_sentinel_img_id:
                                RO = ((int(current_sentinel_img_id.rsplit('_')[7][1:]) - 73) % 175) + 1
                            if 'S1B' in current_sentinel_img_id:
                                RO = ((int(current_sentinel_img_id.rsplit('_')[7][1:]) - 27) % 175) + 1
                            if mode == 'ASCENDING':
                                dict_metadata_ascending.update(
                                    {pd.to_datetime(current_sentinel_img_id.rsplit('_')[5][0:8]): RO})
                            if mode == 'DESCENDING':
                                dict_metadata_descending.update(
                                    {pd.to_datetime(current_sentinel_img_id.rsplit('_')[5][0:8]): RO})

                except KeyboardInterrupt:
                    raise
                # TO DO can eventually use the max angle orbit
                RO_ascending_selection = statistics.mode(list(dict_metadata_ascending.values()))
                RO_descending_selection = statistics.mode(list(dict_metadata_descending.values()))
                dict_ascending_orbits_field.update({gj.features[i].properties['id']: RO_ascending_selection})
                dict_descending_orbits_field.update({gj.features[i].properties['id']: RO_descending_selection})
                return RO_ascending_selection,RO_descending_selection


            #### LOAD THE FIELDS FOR WHICH THE TIMESERIES SHOULD BE EXTRACTED FOR THE CROP CALENDARS

            ### ingest the field(s) for which the crop calendars should be defined
            #TODO: The properties of the fields should be ingested from the main script => use Geojson as input
            ##### GEOJSON AS INPUT FORMAT FOR EXTRACTING THE DATA
            with open(gjson_path) as f: gj = geojson.load(f)

            ## give every parcel an unique id and convert it to a geometry collection
            unique_ids_fields = []
            dict_ascending_orbits_field = dict()
            dict_descending_orbits_field = dict()

            for i in range(len(gj)):
                gj.features[i].properties['id'] = str(uuid.uuid1())
                unique_ids_fields.extend([gj.features[i].properties['id']])
                ### RETRIEVE THE MOST FREQUENT RELATIVE ORBIT PASS PER FIELD AND PER PASS FOR THE SPECIFIED TIME RANGE
                RO_ascending_selection,RO_descending_selection = GEE_RO_retrieval(gj,i)
                dict_ascending_orbits_field.update({gj.features[i].properties['id']: RO_ascending_selection})
                dict_descending_orbits_field.update({gj.features[i].properties['id']: RO_descending_selection})


            geo=shapely.geometry.GeometryCollection([shapely.geometry.shape(feature["geometry"]).buffer(0) for feature in gj["features"]])

            # # make a list with unique ids per field to simplify data extraction in the df's and to link the crop calendar result with the field
            #for n in range(len(geo)): unique_ids_fields.extend([uuid.uuid4().hex[:30].lower()])

            # get the datacube containing the time series data
            #TODO QUESTION FOR OPENEO INTEGRATION: PER FIELD EXTRACT THE MOST FREQUENT RO FOR ASCENDING AND DESCENDING ORBIT => HOW IMPLEMENT THIS EFFICIENTLY WITHOUT LOOPING OVER ALL THE FIELDS
            bands_ts = get_bands(start,end)


            ##### POST PROCESSING TIMESERIES USING A UDF
            timeseries = bands_ts.filter_temporal(start,end).polygonal_mean_timeseries(geo)

            udf = self.load_udf('crop_calendar_udf.py')

            run_local = False

            if not run_local:
                job_result:Job = timeseries.process("run_udf",data = timeseries._pg, udf = udf, runtime = 'Python').execute_batch(r"crop_calendar_field_test.json")
                out_location = "cropcalendar.json"
                job_result.download_results(out_location)
                with open(out_location,'r') as calendar_file:
                    crop_calendars = json.load(calendar_file)
            else:
                # demo datacube of VH_VV and fAPAR time series
                with open(r"C:\Users\bontek\git\e-shape\Pilot1\Tests\Cropcalendars\EX_files\TAP_fields_datacube_metrics_test.json",'r') as ts_file:
                    ts_dict = json.load(ts_file)
                    df_metrics = timeseries_json_to_pandas(ts_dict)
                    df_metrics.index  = pd.to_datetime(df_metrics.index)
                # use the UDF to determine the crop calendars for the fields in the geometrycollection
                from .crop_calendar_udf import udf_cropcalendars
                crop_calendars = udf_cropcalendars(df_metrics, unique_ids_fields)

            #### FINALLY ASSIGN THE CROP CALENDAR EVENTS AS PROPERTIES TO THE GEOJSON FILE WITH THE FIELDS
            for s in range(len(gj)):
                for c in range(crop_calendars.shape[1]):  # the amount of crop calendar events which were determined
                    gj.features[s].properties[crop_calendars.columns[c]] = \
                    crop_calendars.loc[crop_calendars.index == unique_ids_fields[s]][crop_calendars.columns[c]].values[0]  # the date of the event


            return gj








