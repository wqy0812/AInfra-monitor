"""Shared evidence and resource checks for dashboard and image publication."""
import json
import time
from urllib.parse import quote


def save(root, name, data):
    path = root / name
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def record_failure(root, name, error):
    if hasattr(error, 'add_note'):
        error.add_note('Rollback is disabled; preserve current state and fix forward.')
    try:
        save(root, name, {'passed': False, 'at': time.time(), 'type': type(error).__name__,
                         'error': str(error)[:500], 'recovery': 'fix_forward', 'automatic_rollback': False})
    except OSError as reporting_error:
        if hasattr(error, 'add_note'):
            error.add_note('Failure report could not be saved: ' + str(reporting_error))


def snapshot(fetch, *, grouped=False):
    """Read every project, including custom projects, with the caller's auth."""
    projects = fetch('/api/v1/projects')
    result = {'projects': projects}
    if not grouped:
        result.update(datasources=[], dashboards=[])
    for project in projects:
        name = project['metadata']['name']
        for kind in ('datasources', 'dashboards'):
            documents = fetch('/api/v1/projects/' + quote(name, safe='') + '/' + kind)
            if grouped:
                result[name + '/' + kind] = documents
            else:
                result[kind].extend(documents)
    return result


def readback(fetch, route, expected, *, normalize=lambda document: document['spec']):
    actual = fetch(route)
    assert normalize(actual) == normalize(expected), 'Readback differs: ' + route
    return actual
