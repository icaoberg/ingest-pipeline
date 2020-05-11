import sys
import os
import yaml
import json
from pathlib import Path
from pprint import pprint
from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.bash_operator import BashOperator
from airflow.operators.python_operator import PythonOperator
from airflow.operators.python_operator import BranchPythonOperator
from airflow.operators.dummy_operator import DummyOperator
from airflow.operators.dagrun_operator import TriggerDagRunOperator
from airflow.hooks.http_hook import HttpHook

import utils

from utils import localized_assert_json_matches_schema as assert_json_matches_schema


def get_dataset_uuid(**kwargs):
    ctx = kwargs['dag_run'].conf
    return ctx['submission_id']


# Following are defaults which can be overridden later on
default_args = {
    'owner': 'hubmap',
    'depends_on_past': False,
    'start_date': datetime(2019, 1, 1),
    'email': ['joel.welling@gmail.com'],
    'email_on_failure': False,
    'email_on_retry': False,
    'retries': 1,
    'retry_delay': timedelta(minutes=1),
    'provide_context': True,
    'xcom_push': True,
    'queue': utils.map_queue_name('general'),
    'on_failure_callback': utils.create_dataset_state_error_callback(get_dataset_uuid)    
}


with DAG('scan_and_begin_processing', 
         schedule_interval=None, 
         is_paused_upon_creation=False, 
         default_args=default_args,
         user_defined_macros={'tmp_dir_path' : utils.get_tmp_dir_path}         
         ) as dag:
        
    def send_status_msg(**kwargs):
        ctx = kwargs['dag_run'].conf
        retcode_ops = ['run_md_extract']
        retcodes = [int(kwargs['ti'].xcom_pull(task_ids=op))
                    for op in retcode_ops]
        print('retcodes: ', {k:v for k, v in zip(retcode_ops, retcodes)})
        success = all([rc == 0 for rc in retcodes])
        ds_dir = ctx['lz_path']
        http_conn_id='ingest_api_connection'
        endpoint='/datasets/status'
        method='PUT'
        headers={'authorization' : 'Bearer ' + ctx['auth_tok'],
                 'content-type' : 'application/json'}
        print('headers:')
        pprint(headers)
        extra_options=[]
         
        http = HttpHook(method,
                        http_conn_id=http_conn_id)
 
        if success:
            md_fname = os.path.join(utils.get_tmp_dir_path(kwargs['run_id']),
                                    'rslt.yml')
            with open(md_fname, 'r') as f:
                scanned_md = yaml.safe_load(f)
            dag_prv = utils.get_git_provenance_list([__file__])
            md = {'dag_provenance_list' : dag_prv,
                  'metadata' : scanned_md}
            md.update(utils.get_file_metadata_dict(ds_dir,
                                                   utils.get_tmp_dir_path(kwargs['run_id'])))
            try:
                assert_json_matches_schema(md, 'dataset_metadata_schema.yml')
                data = {'dataset_id' : ctx['submission_id'],
                        'status' : 'QA',
                        'message' : 'the process ran',
                        'metadata': md}
            except AssertionError as e:
                print('invalid metadata follows:')
                pprint(md)
                data = {'dataset_id' : ctx['submission_id'],
                        'status' : 'Error',
                        'message' : 'internal error; schema violation: {}'.format(e),
                        'metadata': {}}
            kwargs['ti'].xcom_push(key='collectiontype',
                                   value=(scanned_md['collectiontype']
                                          if 'collectiontype' in scanned_md
                                          else None))
        else:
            log_fname = os.path.join(utils.get_tmp_dir_path(kwargs['run_id']),
                                     'session.log')
            with open(log_fname, 'r') as f:
                err_txt = '\n'.join(f.readlines())
            data = {'dataset_id' : ctx['submission_id'],
                    'status' : 'Invalid',
                    'message' : err_txt}
            kwargs['ti'].xcom_push(key='collectiontype', value=None)
        print('data: ')
        pprint(data)
         
        response = http.run(endpoint,
                            json.dumps(data),
                            headers,
                            extra_options)
        print('response: ')
        pprint(response.json())


    def maybe_spawn_dag(context, dag_run_obj):
        """
        This needs to be table-driven.  Its purpose is to suppress
        triggering the dependent DAG if the collectiontype doesn't
        have a dependent DAG.
        """
        print('context:')
        pprint(context)
        ctx = context['dag_run'].conf
        md_extract_retcode = int(context['ti'].xcom_pull(task_ids="run_md_extract"))
        collectiontype = context['ti'].xcom_pull(key='collectiontype',
                                                 task_ids='send_status_msg')
        if md_extract_retcode == 0 and collectiontype is not None:
            md_fname = os.path.join(utils.get_tmp_dir_path(context['run_id']),
                                    'rslt.yml')
            with open(md_fname, 'r') as f:
                md = yaml.safe_load(f)
            if collectiontype in ['rnaseq_10x', 'codex', 'devtest']:
                payload = {'ingest_id' : context['run_id'],
                           #'ingest_id' : ctx['ingest_id'],
                           'auth_tok' : ctx['auth_tok'],
                           'parent_lz_path' : ctx['lz_path'],
                           'parent_submission_id' : ctx['submission_id'],
                           'metadata': md}
                dag_run_obj.payload = payload
                return dag_run_obj
            else:
                print('Extracted metadata has no "collectiontype" element')
                return None
        else:
            # The extraction of metadata failed or collectiontype is unknown,
            # so spawn no child runs
            return None


    def trigger_or_skip(**kwargs):
        collectiontype = kwargs['ti'].xcom_pull(key='collectiontype',
                                                task_ids="send_status_msg")
        print('collectiontype: <%s>' % collectiontype)
        if collectiontype is None:
            return 'no_spawn'
        else:
            return 'maybe_spawn_dag'

    t_run_md_extract = BashOperator(
        task_id='run_md_extract',
        bash_command=""" \
        lz_dir="{{dag_run.conf.lz_path}}" ; \
        src_dir="{{dag_run.conf.src_path}}/md" ; \
        top_dir="{{dag_run.conf.src_path}}" ; \
        work_dir="{{tmp_dir_path(run_id)}}" ; \
        cd $work_dir ; \
        env PYTHONPATH=${PYTHONPATH}:$top_dir \
        python $src_dir/metadata_extract.py --out ./rslt.yml --yaml "$lz_dir" \
          > ./session.log 2>&1 ; \
        echo $?
        """,
        provide_context = True
        )

    t_send_status = PythonOperator(
        task_id='send_status_msg',
        python_callable=send_status_msg,
        provide_context=True
    )

    t_maybe_trigger = BranchPythonOperator(
        task_id='maybe_trigger',
        python_callable=trigger_or_skip,
        provide_context=True
        )

    t_join = DummyOperator(
        task_id='join',
        trigger_rule='one_success')

    t_no_spawn = DummyOperator(
        task_id='no_spawn')

    t_spawn_dag = TriggerDagRunOperator(
        task_id="maybe_spawn_dag",
        trigger_dag_id="trig_{{ti.xcom_pull(key='collectiontype', task_ids='send_status_msg')}}",
        python_callable = maybe_spawn_dag
        )
 

    t_create_tmpdir = BashOperator(
        task_id='create_temp_dir',
        bash_command='mkdir {{tmp_dir_path(run_id)}}',
        provide_context=True
        )


    t_cleanup_tmpdir = BashOperator(
        task_id='cleanup_temp_dir',
        bash_command='rm -r {{tmp_dir_path(run_id)}}',
        trigger_rule='all_success'
        )

    (dag >> t_create_tmpdir >> t_run_md_extract >> t_send_status
     >> t_maybe_trigger)
    t_maybe_trigger >> t_spawn_dag >> t_join
    t_maybe_trigger >> t_no_spawn >> t_join
    t_join >> t_cleanup_tmpdir

