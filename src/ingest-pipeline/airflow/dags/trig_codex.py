from airflow import DAG
from airflow.operators.bash_operator import BashOperator
from airflow.operators.dagrun_operator import DagRunOrder
from airflow.operators.multi_dagrun import TriggerMultiDagRunOperator
from airflow.operators.python_operator import PythonOperator
from airflow.operators.http_operator import SimpleHttpOperator
from airflow.hooks.http_hook import HttpHook
from airflow.configuration import conf
from airflow.models import Variable
from datetime import datetime, timedelta
import pytz
from pprint import pprint
import os
import yaml
import json

import utils

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
    'xcom_push': True
}

with DAG('trig_codex', 
         schedule_interval=None, 
         is_paused_upon_creation=False, 
         default_args=default_args) as dag:

 
    def maybe_spawn_dags(**kwargs):
        """
        This is a generator which returns appropriate DagRunOrders
        """
        print('kwargs:')
        pprint(kwargs)
        print('dag_run conf:')
        pprint(kwargs['dag_run'].conf)
        metadata = kwargs['dag_run'].conf['metadata']
        auth_tok = kwargs['dag_run'].conf['auth_tok']
        assert 'components' in metadata, 'codex metadata with no components'
        payload = {k:kwargs['dag_run'].conf[k] for k in kwargs['dag_run'].conf}
        payload['apply'] = 'codex_cytokit'
        if 'dag_provenance' in payload:
            payload['dag_provenance'].update(utils.get_git_provenance_dict(__file__))
        else:
            payload['dag_provenance'] = utils.get_git_provenance_dict(__file__)
        yield DagRunOrder(payload=payload)


    t_spawn_dag = TriggerMultiDagRunOperator(
        task_id="spawn_dag",
        trigger_dag_id="codex_cytokit",  # Ensure this equals the dag_id of the DAG to trigger
        python_callable = maybe_spawn_dags,
        )
  
  
    dag >> t_spawn_dag