"""Bound VM connection pools to one collection cycle or history query group."""
from contextlib import asynccontextmanager
from contextvars import ContextVar

import httpx


class QueryClient:
    def __init__(self, **options):
        self.options = options
        self.current = ContextVar('monitoring_query_client', default=None)
        self.is_closed = False

    @asynccontextmanager
    async def scope(self):
        if self.is_closed:
            raise RuntimeError('Monitoring client is closed')
        # Child query tasks share this pool; unrelated cycles and readers do not.
        # A cancelled/timed-out batch cannot poison every subsequent refresh.
        async with httpx.AsyncClient(trust_env=False, timeout=10,
                                     limits=httpx.Limits(max_connections=8), **self.options) as client:
            token = self.current.set(client)
            try:
                yield
            finally:
                self.current.reset(token)

    async def request(self, method, url, **kwargs):
        if self.is_closed:
            raise RuntimeError('Monitoring client is closed')
        client = self.current.get()
        if client is not None:
            return await client.request(method, url, **kwargs)
        async with self.scope():
            return await self.current.get().request(method, url, **kwargs)

    async def get(self, url, **kwargs):
        return await self.request('GET', url, **kwargs)

    async def post(self, url, **kwargs):
        return await self.request('POST', url, **kwargs)

    async def aclose(self):
        # Owners cancel and join their batch tasks before closing the facade.
        self.is_closed = True
