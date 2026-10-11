"""Official vmalert owns evaluation; manual replay requires explicit bounds."""
import json
import pytest
from monitoring import recording_runtime as runtime

@pytest.mark.parametrize('replay',[False,True])
def test_launcher_execs_official_binary_without_supervisor_or_state_writes(tmp_path,monkeypatch,replay):
    output=tmp_path/'rules.json';calls=[]
    args=['recording','--output',str(output),'--vm','http://vm','--binary','/official']
    if replay:args+=['--replay-start','2026-10-11T00:00:00Z','--replay-end','2026-10-11T01:00:00Z']
    monkeypatch.setattr('sys.argv',args)
    monkeypatch.setattr(runtime.os,'execv',lambda *a:calls.append(a))
    runtime.main()
    binary,command=calls[0]
    assert binary==command[0]=='/official'
    assert '-datasource.url=http://vm' in command and '-remoteWrite.url=http://vm' in command
    assert any(x.startswith('-replay.timeFrom=') for x in command)==replay
    assert len(json.loads(output.read_text())['groups'])==4
    assert list(tmp_path.iterdir())==[output]

def test_one_replay_bound_cannot_accidentally_start_live_writer(tmp_path,monkeypatch):
    monkeypatch.setattr('sys.argv',['recording','--replay-start','100','--output',str(tmp_path/'rules')])
    with pytest.raises(SystemExit):runtime.main()
    assert not list(tmp_path.iterdir())
