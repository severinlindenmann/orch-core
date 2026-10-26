import logging

import pytest

from orch.runner.config import parse
from orch.runner.grants import GrantError, GrantStore, RedactFilter
from orch.runner.link import spawn_spec
from orch.runner.supervise import SpawnError

RUN = 'r_' + '0' * 26
GRANT = 'gr_' + '0' * 26
ARGS = dict(run=RUN, workspace_id='a' * 32, person='p_' + 'b' * 32,
            grant=GRANT, secret=GRANT + '.' + 'x' * 43, until='2030-01-01T00:00:00Z')


def test_separate_one_use_drop_redaction():
    store = GrantStore(clock=lambda: 0)
    store.put({**ARGS, 'deposit_id': '0' * 26})
    before = store.report()
    store.put_run(ARGS)
    assert store.report() == before
    assert ARGS['secret'] in store.secrets()
    record = logging.LogRecord('test', 20, '', 0, ARGS['secret'] + ' ' + 'x' * 43, (), None)
    RedactFilter(store).filter(record)
    assert 'x' * 43 not in record.getMessage()
    with pytest.raises(GrantError) as exc:
        store.secret_for_run(RUN, 'wrong')
    assert exc.value.code == 'launch.no_grant'
    assert store.secret_for_run(RUN, GRANT) == ARGS['secret']
    with pytest.raises(GrantError):
        store.secret_for_run(RUN, GRANT)
    assert store.secret_for(ARGS['workspace_id'], ARGS['person'], GRANT) == ARGS['secret']
    store.put_run(ARGS)
    assert store.drop(GRANT)
    assert store.secrets() == [] and store.report() == []


def test_expiry_and_capacity():
    now = [0]
    store = GrantStore(clock=lambda: now[0])
    for n in range(16):
        store.put_run({**ARGS, 'run': 'r_' + f'{n:026d}'})
    with pytest.raises(GrantError):
        store.put_run({**ARGS, 'run': 'r_' + '1' * 26})
    now[0] = 2_000_000_000
    with pytest.raises(GrantError) as exc:
        store.secret_for_run(RUN, GRANT)
    assert exc.value.code == 'launch.no_grant'
    with pytest.raises(GrantError):
        store.put_run(ARGS)
    store.put_run({**ARGS, 'until': '2040-01-01T00:00:00Z'})
    assert len(store.secrets()) == 1


@pytest.mark.parametrize('change', [{'run': 'bad'}, {'workspace_id': 'bad'}, {'person': 'bad'},
                                     {'grant': 'bad'}, {'secret': 'bad'}, {'until': '2030-99-01T00:00:00Z'}])
def test_invalid_put(change):
    with pytest.raises(GrantError) as exc:
        GrantStore(clock=lambda: 0).put_run({**ARGS, **change})
    assert exc.value.code == 'invalid.input'


def test_spawn_consumes_run_slot():
    store = GrantStore(clock=lambda: 0)
    store.put_run(ARGS)
    args = {**ARGS, 'kind': 'agent', 'run_grant': True, 'session': 's_' + '0' * 26,
            'argv': ['/bin/codex'], 'env_names': [], 'env_fixed': {}, 'cwd': {}}
    config = parse({'v': 1, 'login_shell': False, 'programs': {'codex': '/bin/codex'}})
    with pytest.raises(SpawnError):
        spawn_spec({**args, 'run_grant': 'true'}, {}, config, store)
    assert spawn_spec(args, {}, config, store).env['ORCH_GRANT'] == ARGS['secret']
    with pytest.raises(SpawnError) as exc:
        spawn_spec(args, {}, config, store)
    assert exc.value.code == 'launch.no_grant'
