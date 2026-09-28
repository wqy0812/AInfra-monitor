"""Exercise the real access middleware without starting monitoring workers."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('allowed,client,expected', [
    ({'*'}, '192.0.2.1', 200),
    ({'127.0.0.1'}, '127.0.0.1', 200),
    ({'127.0.0.1'}, '192.0.2.1', 403),
    (set(), '192.0.2.1', 403),
])
def test_client_allowlist(allowed, client, expected):
    tree = ast.parse((Path(__file__).parents[1] / 'monitoring/api.py').read_text())
    function = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'restrict')
    function.decorator_list = []
    namespace = {'ALLOWED': allowed}
    exec(compile(ast.Module(body=[function], type_ignores=[]), 'api.py', 'exec'), namespace)

    async def next_handler(request):
        return SimpleNamespace(status_code=200)

    request = SimpleNamespace(client=SimpleNamespace(host=client))
    response = asyncio.run(namespace['restrict'](request, next_handler))
    assert response.status_code == expected
