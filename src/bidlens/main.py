from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from .database import engine, Base
from .routes import auth, opportunities, api, settings
from . import models
from .routes import sam
from .scheduler import start_scheduler
from .middleware import ClientRedirectMiddleware
from .config import DATABASE_URL, DOTENV_PATH


print("DATABASE_URL =", DATABASE_URL)
print("DOTENV_PATH =", DOTENV_PATH)

Base.metadata.create_all(bind=engine)

app = FastAPI(title="BidLens")
app.add_middleware(ClientRedirectMiddleware)
app.mount("/static", StaticFiles(directory="src/bidlens/static"), name="static")
app.include_router(auth.router)
app.include_router(opportunities.router)
app.include_router(api.router)
app.include_router(settings.router)
app.include_router(sam.router)

@app.get("/health")
def health():
    return {"status": "ok"}
@app.on_event("startup")
def _startup():
    start_scheduler()
