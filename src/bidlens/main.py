from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from .config import AUTO_CREATE_SCHEMA, ENABLE_INTERNAL_SCHEDULER, startup_diagnostics, validate_deployment_config


for diagnostic in startup_diagnostics():
    print(f"[startup] {diagnostic}")

validate_deployment_config()

from .database import engine, Base
from .routes import admin, auth, opportunities, api, settings, company_profile, pursuit_lanes, imports, grants, integrations, home, platform, connect_sources
from . import models
from .routes import sam
from .scheduler import start_scheduler
from .middleware import ClientRedirectMiddleware

if AUTO_CREATE_SCHEMA:
    Base.metadata.create_all(bind=engine)
else:
    print("AUTO_CREATE_SCHEMA disabled; skipping Base.metadata.create_all()")

app = FastAPI(title="BidLens")
app.add_middleware(ClientRedirectMiddleware)
app.mount("/static", StaticFiles(directory="src/bidlens/static"), name="static")
app.include_router(auth.router)
app.include_router(platform.router)
app.include_router(home.router)
app.include_router(connect_sources.router)
app.include_router(admin.router)
app.include_router(opportunities.router)
app.include_router(api.router)
app.include_router(settings.router)
app.include_router(company_profile.router)
app.include_router(pursuit_lanes.router)
app.include_router(imports.router)
app.include_router(sam.router)
app.include_router(grants.router)
app.include_router(integrations.router)

@app.get("/health")
def health():
    return {"status": "ok"}


@app.on_event("startup")
def _startup():
    if not ENABLE_INTERNAL_SCHEDULER:
        print("ENABLE_INTERNAL_SCHEDULER disabled; APScheduler will not start in this process")
        return
    if getattr(app.state, "scheduler", None) is not None:
        print("Internal scheduler already started; skipping duplicate startup")
        return
    app.state.scheduler = start_scheduler()
