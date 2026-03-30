from fastapi import FastAPI
from fastapi.responses import JSONResponse
import uvicorn
import logging
from contextlib import asynccontextmanager

from database.database import init_db
from api.routes import router as api_router
from api.auth_routes import router as auth_api_router
from web.routes import router as web_router
from web.auth_routes import router as web_auth_router
from config import get_settings

settings = get_settings()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting CSP Cutting Solution Platform...")
    init_db()
    logger.info("Database initialized")
    yield
    logger.info("Shutting down...")


app = FastAPI(
    title="CSP Cutting Solution Platform",
    description="Cutting plan optimization with MKG ERP integration",
    version="2.0.0",
    lifespan=lifespan
)

# API routes
app.include_router(auth_api_router, prefix="/api")
app.include_router(api_router, prefix="/api")

# Web UI routes
app.include_router(web_auth_router)   # /login, /register, /logout, /settings
app.include_router(web_router)        # /, /cutting-plans, ...


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok"})

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_level="info"
    )
