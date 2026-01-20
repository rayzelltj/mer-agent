"""WebSocket API Router.

This module handles real-time WebSocket communication endpoints for
process status updates and client-server messaging.
"""

import asyncio
import logging

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from src.backend.common.utils.event_utils import track_event_if_configured
from src.backend.v4.config.settings import connection_config

logger = logging.getLogger(__name__)

ws_router = APIRouter(tags=["WebSocket"])


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@ws_router.websocket("/socket/{process_id}")
async def start_comms(
    websocket: WebSocket, process_id: str, user_id: str = Query(None)
):
    """Web-Socket endpoint for real-time process status updates.

    Maintains a persistent connection for bi-directional communication
    between the backend and connected clients.
    """
    # Always accept the WebSocket connection first
    await websocket.accept()

    user_id = user_id or "00000000-0000-0000-0000-000000000000"

    # Add to the connection manager for backend updates
    connection_config.add_connection(
        process_id=process_id, connection=websocket, user_id=user_id
    )
    track_event_if_configured(
        "WebSocketConnectionAccepted", {"process_id": process_id, "user_id": user_id}
    )

    # Keep the connection open - FastAPI will close the connection if this returns
    try:
        # Keep the connection open - FastAPI will close the connection if this returns
        while True:
            # no expectation that we will receive anything from the client but this keeps
            # the connection open and does not take cpu cycle
            try:
                message = await websocket.receive_text()
                logging.debug(f"Received WebSocket message from {user_id}: {message}")
            except asyncio.TimeoutError:
                # Ignore timeouts to keep the WebSocket connection open, but avoid a tight loop.
                logging.debug(
                    f"WebSocket receive timeout for user {user_id}, process {process_id}"
                )
                await asyncio.sleep(0.1)
            except WebSocketDisconnect:
                track_event_if_configured(
                    "WebSocketDisconnect",
                    {"process_id": process_id, "user_id": user_id},
                )
                logging.info(f"Client disconnected from batch {process_id}")
                break
    except Exception as e:
        logging.error(f"Error in WebSocket connection: {str(e)}")
    finally:
        # Always clean up the connection
        await connection_config.close_connection(process_id=process_id)
