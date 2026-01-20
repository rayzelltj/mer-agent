"""Main API Router for v4.

This router aggregates all v4 API sub-routers:
- MER Review endpoints
- Team management endpoints
- Plan management endpoints
- WebSocket endpoints
"""

import logging

from fastapi import APIRouter

from src.backend.v4.api.mer_router import mer_router
from src.backend.v4.api.plan_router import plan_router
from src.backend.v4.api.team_router import team_router
from src.backend.v4.api.ws_router import ws_router

router = APIRouter()
logger = logging.getLogger(__name__)

app_v4 = APIRouter(
    prefix="/api/v4",
    responses={404: {"description": "Not found"}},
)

# Include all sub-routers
app_v4.include_router(mer_router)
app_v4.include_router(team_router)
app_v4.include_router(plan_router)
app_v4.include_router(ws_router)
