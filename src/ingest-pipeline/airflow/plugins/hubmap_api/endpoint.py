"""
Based heavily on https://github.com/airflow-plugins/airflow_api_plugin
"""

import json
import os
import logging
import configparser
from datetime import datetime
import pytz
import yaml
import ast

from werkzeug.exceptions import HTTPException, NotFound 

from flask import Blueprint, current_app, send_from_directory, abort, escape, request, Response
from sqlalchemy import or_
from airflow import settings
from airflow.exceptions import AirflowException, AirflowConfigException
from airflow.www.app import csrf
from airflow.models import DagBag, DagRun, Variable
from airflow.utils import timezone
from airflow.utils.dates import date_range as utils_date_range
from airflow.utils.state import State
from airflow.api.common.experimental import trigger_dag
from airflow.configuration import conf as airflow_conf

from hubmap_api.manager import blueprint as api_bp
from hubmap_api.manager import show_template

from hubmap_commons.hm_auth import AuthHelper, AuthCache, secured
#from hubmap_api.hm_auth import AuthHelper, AuthCache, secured

API_VERSION = 1

LOGGER = logging.getLogger(__name__)

airflow_conf.read(os.path.join(os.environ['AIRFLOW_HOME'], 'instance', 'app.cfg'))

"""
Potentially helpful code for creating a connection

from airflow import settings
from airflow.models import Connection
conn = Connection(
        conn_id=conn_id,
        conn_type=conn_type,
        host=host,
        login=login,
        password=password,
        port=port
) #create a connection object
session = settings.Session() # get the session
session.add(conn)
session.commit() # it will insert the connection object programmatically.
"""

def config(section, key):
    dct = airflow_conf.as_dict()
    if section in dct and key in dct[section]:
        rslt = dct[section][key]
        # airflow config reader leaves quotes, which we want to strip
        for qc in ['"', "'"]:
            if rslt.startswith(qc) and rslt.endswith(qc):
                rslt = rslt.strip(qc)
        return rslt
    else:
        raise AirflowConfigException('No config entry for [{}] {}'.format(section, key))


AUTH_HELPER = None
if not AuthHelper.isInitialized():
    AUTH_HELPER = AuthHelper.create(clientId=config('connections', 'app_client_id'), 
                                    clientSecret=config('connections', 'app_client_secret'))
else:
    AUTH_HELPER = authHelper.instance()


class HubmapApiInputException(Exception):
    pass


class HubmapApiConfigException(Exception):
    pass
 
 
class HubmapApiResponse:
 
    def __init__(self):
        pass
 
    STATUS_OK = 200
    STATUS_BAD_REQUEST = 400
    STATUS_UNAUTHORIZED = 401
    STATUS_NOT_FOUND = 404
    STATUS_SERVER_ERROR = 500
 
    @staticmethod
    def standard_response(status, payload):
        json_data = json.dumps({
            'response': payload
        })
        resp = Response(json_data, status=status, mimetype='application/json')
        return resp
 
    @staticmethod
    def success(payload):
        return HubmapApiResponse.standard_response(HubmapApiResponse.STATUS_OK, payload)
 
    @staticmethod
    def error(status, error):
        return HubmapApiResponse.standard_response(status, {
            'error': error
        })
 
    @staticmethod
    def bad_request(error):
        return HubmapApiResponse.error(HubmapApiResponse.STATUS_BAD_REQUEST, error)
 
    @staticmethod
    def not_found(error='Resource not found'):
        return HubmapApiResponse.error(HubmapApiResponse.STATUS_NOT_FOUND, error)
 
    @staticmethod
    def unauthorized(error='Not authorized to access this resource'):
        return HubmapApiResponse.error(HubmapApiResponse.STATUS_UNAUTHORIZED, error)
 
    @staticmethod
    def server_error(error='An unexpected problem occurred'):
        return HubmapApiResponse.error(HubmapApiResponse.STATUS_SERVER_ERROR, error)


@api_bp.route('/test')
@secured(groups="HuBMAP-read")
def api_test():
    token = None
    clientId=config('connections', 'app_client_id')
    print ("Client id: " + clientId)
    clientSecret=config('connections', 'app_client_secret')
    print ("Client secret: " + clientSecret)
    if 'MAUTHORIZATION' in request.headers:
        token = str(request.headers["MAUTHORIZATION"])[8:]
    elif 'AUTHORIZATION' in request.headers:
        token = str(request.headers["AUTHORIZATION"])[7:]
    print ("Token: " + token)
    return HubmapApiResponse.success({'api_is_alive': True})
 

@api_bp.route('/version')
def api_version():
    return HubmapApiResponse.success({'api': API_VERSION,
                                      'build': config('hubmap_api_plugin', 'build_number')})

 
def format_dag_run(dag_run):
    return {
        'run_id': dag_run.run_id,
        'dag_id': dag_run.dag_id,
        'state': dag_run.get_state(),
        'start_date': (None if not dag_run.start_date else str(dag_run.start_date)),
        'end_date': (None if not dag_run.end_date else str(dag_run.end_date)),
        'external_trigger': dag_run.external_trigger,
        'execution_date': str(dag_run.execution_date)
    }


def find_dag_runs(session, dag_id, dag_run_id, execution_date):
    qry = session.query(DagRun)
    qry = qry.filter(DagRun.dag_id == dag_id)
    qry = qry.filter(or_(DagRun.run_id == dag_run_id, DagRun.execution_date == execution_date))

    return qry.order_by(DagRun.execution_date).all()


def _get_required_string(data, st):
    """
    Return data[st] if present and a valid string; otherwise raise HubmapApiInputException
    """
    if st in data and data[st] is not None:
        return data[st]
    else:
        raise HubmapApiInputException(st)


def get_request_ingest_reply_parms(provider, submission_id, process):
    """
    This routine finds and returns the response parameters required by the request_ingest message,
    as an ordered tuple.  The input parameters correspond to the values in the request_ingest request.
    """
    if process.startswith('mock.'):
        # test request; there should be pre-recorded response data
        yml_path = os.path.join(os.path.dirname(__file__),
                                '../../data/mock_data/',
                                process + '.yml')
        try:
            with open(yml_path, 'r') as f:
                mock_data = yaml.safe_load(f)
                overall_file_count = mock_data['request_ingest_response']['overall_file_count']
                top_folder_contents = mock_data['request_ingest_response']['top_folder_contents']
        except IOError as e:
            LOGGER.error('mock data load failed: {}'.format(e))
            raise HubmapApiInputException('No mock data found for process %s', process)
    else:
        #dct = {'provider' : provider, 'submission_id' : submission_id, 'process' : process}
        dct = {'provider' : 'Vanderbilt TMC', 'submission_id' : 'VAN0001-RK-1-21_24', 'process' : process}
        lz_path = config('connections', 'lz_path').format(**dct)
        if os.path.exists(lz_path) and os.path.isdir(lz_path):
            n_files = 0
            top_folder_contents = None
            for root, subdirs, files in os.walk(lz_path):
                if root == lz_path:
                    top_folder_contents = (subdirs + files)[:]
                n_files += len(files)
            assert top_folder_contents is not None, 'internal error using os.walk?'
            overall_file_count = n_files
        else:
            LOGGER.error("cannot find the ingest data for '%s' '%s '%s' (expected %s)"
                         % (provider, submission_id, process, lz_path))
            raise HubmapApiInputException("Cannot find the expected ingest directory for '%s' '%s' '%s'"
                                          % (provider, submission_id, process))
    
    LOGGER.info('get_request_ingest_reply_parms returning {} {}'.format(overall_file_count, top_folder_contents))
    return overall_file_count, top_folder_contents


"""
Parameters for this request (all required)

Key            Method    Type    Description
provider        post    string    Providing site, presumably a known TMC
submission_id   post    string    Unique ID string specifying this dataset
process         post    string    string denoting a unique known processing workflow to be applied to this data

Parameters included in the response:
Key        Type    Description
ingest_id  string  Unique ID string to be used in references to this request
run_id     string  The identifier by which the ingest run is known to Airflow
overall_file_count  int  Total number of files and directories in submission
top_folder_contents list list of all files and directories in the top level folder
"""
@csrf.exempt
@api_bp.route('/request_ingest', methods=['POST'])
#@secured(groups="HuBMAP-read")
def request_ingest():
    authorization = request.headers.get('authorization')
    LOGGER.info('top of request_ingest: AUTH %s', authorization)
    assert authorization[:len('BEARER')].lower() == 'bearer', 'authorization is not BEARER'
    auth_dct = ast.literal_eval(authorization[len('BEARER'):].strip())
    LOGGER.info('auth_dct: %s', auth_dct)
    assert 'nexus_token' in auth_dct, 'authorization has no nexus_token'
    auth_tok = auth_dct['nexus_token']
    LOGGER.info('auth_tok: %s', auth_tok)
  
    # decode input
    data = request.get_json(force=True)
    
    # Test and extract required parameters
    try:
        provider = _get_required_string(data, 'provider')
        submission_id = _get_required_string(data, 'submission_id')
        process = _get_required_string(data, 'process')
    except HubmapApiInputException as e:
        return HubmapApiResponse.bad_request('Must specify {} to request data be ingested'.format(str(e)))

    process = process.lower()  # necessary because config parser has made the corresponding string lower case

    try:
        dag_id = config('ingest_map', process)
    except HubmapApiConfigException:
        return HubmapApiResponse.bad_request('{} is not a known ingestion process'.format(process))
    
    overall_file_count, top_folder_contents = get_request_ingest_reply_parms(provider, submission_id,
                                                                             process)
    
    try:
        session = settings.Session()

        dagbag = DagBag('dags')
 
        if dag_id not in dagbag.dags:
            return HubmapApiResponse.not_found("Dag id {} not found".format(dag_id))
 
        dag = dagbag.get_dag(dag_id)

        # Produce one and only one run
        tz = pytz.timezone(config('core', 'timezone'))
        execution_date = datetime.now(tz)
        LOGGER.info('execution_date: {}'.format(execution_date))

        run_id = '{}_{}_{}'.format(submission_id, process, execution_date.isoformat())
        ingest_id = run_id

        conf = {'provider': provider,
                'submission_id': submission_id,
                'process': process,
                'dag_id': dag_id,
                'run_id': run_id,
                'ingest_id': ingest_id,
                'auth_tok': auth_tok
                }

        if find_dag_runs(session, dag_id, run_id, execution_date):
            # The run already happened??
            return HubmapAPIResponse.server_error('The request happened twice?')

        try:
            dr = trigger_dag.trigger_dag(dag_id, run_id, conf, execution_date=execution_date)
        except AirflowException as err:
            LOGGER.error(err)
            return HubmapApiResponse.server_error("Attempt to trigger run produced an error: {}".format(err))
        LOGGER.info('dagrun follows: {}'.format(dr))

#             dag.create_dagrun(
#                 run_id=run['run_id'],
#                 execution_date=run['execution_date'],
#                 state=State.RUNNING,
#                 conf=conf,
#                 external_trigger=True
#             )
#            results.append(run['run_id'])

        session.close()
    except HubmapApiInputException as e:
        return HubmapApiResponse.bad_request(str(e))
    except ValueError as e:
        return HubmapApiResponse.server_error(str(e))
    except AirflowException as e:
        return HubmapApiResponse.server_error(str(e))
    except Exception as e:
        return HubmapApiResponse.server_error(str(e))

    return HubmapApiResponse.success({'ingest_id': ingest_id,
                                      'run_id': run_id,
                                      'overall_file_count': overall_file_count,
                                      'top_folder_contents': top_folder_contents})

"""
Parameters for this request: None

Parameters included in the response:
Key        Type    Description
process_strings  list of strings  The list of valid 'process' strings
"""
@api_bp.route('get_process_strings')
def get_process_strings():
    dct = airflow_conf.as_dict()
    psl = [s.upper() for s in dct['ingest_map']] if 'ingest_map' in dct else []
    return HubmapApiResponse.success({'process_strings': psl})
