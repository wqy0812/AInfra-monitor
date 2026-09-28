"""Read-only local rendering fixture: does not run either materialization loop."""
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fastapi import FastAPI
import uvicorn
from monitoring.perses_acceleration import AccelerationService, install_routes

ROOT = Path(__file__).resolve().parents[2]


@asynccontextmanager
async def lifespan(app):
    catalog = json.loads((ROOT / 'monitoring/perses_acceleration_catalog.json').read_text())
    service = AccelerationService('http://127.0.0.1:18547', ROOT / 'evidence/acceleration-20260926/synthetic-state', catalog)
    app.state.perses_acceleration = service
    try:
        yield
    finally:
        await service.close()


app = FastAPI(lifespan=lifespan)
install_routes(app)


@app.get('/health')
async def health():
    return app.state.perses_acceleration.status()


if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=18550, access_log=False)
