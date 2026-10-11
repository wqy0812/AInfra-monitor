"""Native publication ignores retired admission state and removes proxy files."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).parents[1]/"perses"))
from acceleration_publication import published
from project_split import read_resources
from dashboard_reorg import write_resources


def test_native_publication_is_idempotent_and_ignores_legacy_groups():
    resources=read_resources(Path(__file__).parents[1]/'perses/projects')
    assert published(resources,{'groups':[]})==published(resources,{'groups':['cpu','a3','dcu']})
    assert published(published(resources))==published(resources)
    assert all(d['metadata']['name']=='victoriametrics' for d in published(resources)['datasources'])


def test_generator_removes_retired_datasource_files(tmp_path):
    resources=read_resources(Path(__file__).parents[1]/'perses/projects')
    for project in ('a3-monitoring','dcu-monitoring','xpu-monitoring'):
        path=tmp_path/project/'perses-accelerated-datasource.json';path.parent.mkdir();path.write_text('old proxy')
    write_resources(resources,tmp_path)
    assert len(read_resources(tmp_path)['datasources'])==3
    assert not list(tmp_path.rglob('perses-accelerated-datasource.json'))
