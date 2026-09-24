"""Read live VM data with the candidate module; never start materialization."""
import asyncio
import copy
import json
import time
from monitoring.api import Service, summary_point


async def main():
    end = int((time.time() - 120) // 5) * 5
    results = {}
    for env in ('dcu-pd', 'a3-vllm', 'xpu-pd'):
        service = Service(env)
        calls = []
        original = service._history
        async def counted(*args):
            calls.append(args)
            return await original(*args)
        service._history = counted
        try:
            full = await service.history(1, end - 3600, end)
            values = await asyncio.gather(*(service.history(1, end - 3600, end, view='summary') for _ in range(20)))
            reduced = values[0]
            assert len(calls) == 2 and all(v is reduced for v in values)
            assert reduced['points'] and reduced == {**full, 'points': [summary_point(copy.deepcopy(p)) for p in full['points']]}
            assert await service.history(1, end - 3600, end, view='summary') is reduced
            results[env] = {'points': len(reduced['points']), 'shared_waiters': len(values), 'query_groups': len(calls), 'full_summary_match': True}
        finally:
            await service.close()
    print(json.dumps({'passed': True, 'results': results}))


asyncio.run(main())
