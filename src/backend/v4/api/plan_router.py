"""Plan Management API Router.

This module handles all plan-related endpoints, including:
- Creating new plans (process_request)
- Plan approval/rejection
- User clarification responses
- Agent messages
- Retrieving plans
"""

import asyncio
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from pydantic import BaseModel

from src.backend.auth.auth_utils import get_authenticated_user_details
from src.backend.common.database.database_factory import DatabaseFactory
from src.backend.common.models.messages_af import InputTask, Plan, PlanStatus
from src.backend.common.utils.event_utils import track_event_if_configured
from src.backend.common.utils.utils_af import rai_success
from src.backend.v4.common.services.plan_service import PlanService
from src.backend.v4.config.settings import connection_config, orchestration_config
from src.backend.v4.models import messages
from src.backend.v4.models.messages import WebsocketMessageType
from src.backend.v4.orchestration.orchestration_manager import OrchestrationManager

logger = logging.getLogger(__name__)

plan_router = APIRouter(tags=["Plans"])


# ---------------------------------------------------------------------------
# Endpoints (InputTask imported from src.backend.common.models.messages_af)
# ---------------------------------------------------------------------------


@plan_router.post("/process_request")
async def process_request(
    background_tasks: BackgroundTasks, input_task: InputTask, request: Request
):
    """Create a new plan without full processing.

    Creates a plan, validates RAI compliance, and starts background orchestration.
    """
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]
    if not user_id:
        track_event_if_configured(
            "UserIdNotFound", {"status_code": 400, "detail": "no user"}
        )
        raise HTTPException(status_code=400, detail="no user found")

    try:
        memory_store = await DatabaseFactory.get_database(user_id=user_id)
        user_current_team = await memory_store.get_current_team(user_id=user_id)
        team_id = None
        if user_current_team:
            team_id = user_current_team.team_id
        if not team_id:
            raise HTTPException(
                status_code=404,
                detail="No team selected. Please select a team first.",
            )
        team = await memory_store.get_team_by_id(team_id=team_id)
        if not team:
            raise HTTPException(
                status_code=404,
                detail=f"Team configuration '{team_id}' not found or access denied",
            )
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Error retrieving team configuration: {e}",
        ) from e

    if not await rai_success(input_task.description, team, memory_store):
        track_event_if_configured(
            "RAI failed",
            {
                "status": "Plan not created - RAI check failed",
                "description": input_task.description,
                "session_id": input_task.session_id,
            },
        )
        raise HTTPException(
            status_code=400,
            detail="Request contains content that doesn't meet our safety guidelines, try again.",
        )

    if not input_task.session_id:
        input_task.session_id = str(uuid.uuid4())
    try:
        plan_id = str(uuid.uuid4())
        # Initialize memory store and service
        plan = Plan(
            id=plan_id,
            plan_id=plan_id,
            user_id=user_id,
            session_id=input_task.session_id,
            team_id=team_id,
            initial_goal=input_task.description,
            overall_status=PlanStatus.in_progress,
        )
        await memory_store.add_plan(plan)

        track_event_if_configured(
            "PlanCreated",
            {
                "status": "success",
                "plan_id": plan.plan_id,
                "session_id": input_task.session_id,
                "user_id": user_id,
                "team_id": team_id,
                "description": input_task.description,
            },
        )
    except Exception as e:
        logger.error(f"Error creating plan: {e}")
        track_event_if_configured(
            "PlanCreationFailed",
            {
                "status": "error",
                "description": input_task.description,
                "session_id": input_task.session_id,
                "user_id": user_id,
                "error": str(e),
            },
        )
        raise HTTPException(status_code=500, detail="Failed to create plan") from e

    try:

        async def run_orchestration_task():
            await OrchestrationManager().run_orchestration(user_id, input_task)

        background_tasks.add_task(run_orchestration_task)

        return {
            "status": "Request started successfully",
            "session_id": input_task.session_id,
            "plan_id": plan_id,
        }

    except Exception as e:
        track_event_if_configured(
            "RequestStartFailed",
            {
                "session_id": input_task.session_id,
                "description": input_task.description,
                "error": str(e),
            },
        )
        raise HTTPException(
            status_code=400, detail=f"Error starting request: {e}"
        ) from e


@plan_router.post("/plan_approval")
async def plan_approval(
    human_feedback: messages.PlanApprovalResponse, request: Request
):
    """Endpoint to receive plan approval or rejection from the user."""
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]
    if not user_id:
        raise HTTPException(
            status_code=401, detail="Missing or invalid user information"
        )
    # Set the approval in the orchestration config
    try:
        if user_id and human_feedback.m_plan_id:
            if (
                orchestration_config
                and human_feedback.m_plan_id in orchestration_config.approvals
            ):
                orchestration_config.set_approval_result(
                    human_feedback.m_plan_id, human_feedback.approved
                )
                logger.info(f"Plan approval received: {human_feedback}")

                try:
                    result = await PlanService.handle_plan_approval(
                        human_feedback, user_id
                    )
                    logger.info(f"Plan approval processed: {result}")

                except ValueError as ve:
                    logger.error(f"ValueError processing plan approval: {ve}")
                    await connection_config.send_status_update_async(
                        {
                            "type": WebsocketMessageType.ERROR_MESSAGE,
                            "data": {
                                "content": "Approval failed due to invalid input.",
                                "status": "error",
                                "timestamp": asyncio.get_event_loop().time(),
                            },
                        },
                        user_id,
                        message_type=WebsocketMessageType.ERROR_MESSAGE,
                    )

                except Exception:
                    logger.error("Error processing plan approval", exc_info=True)
                    await connection_config.send_status_update_async(
                        {
                            "type": WebsocketMessageType.ERROR_MESSAGE,
                            "data": {
                                "content": "An unexpected error occurred while processing the approval.",
                                "status": "error",
                                "timestamp": asyncio.get_event_loop().time(),
                            },
                        },
                        user_id,
                        message_type=WebsocketMessageType.ERROR_MESSAGE,
                    )

                track_event_if_configured(
                    "PlanApprovalReceived",
                    {
                        "plan_id": human_feedback.plan_id,
                        "m_plan_id": human_feedback.m_plan_id,
                        "approved": human_feedback.approved,
                        "user_id": user_id,
                        "feedback": human_feedback.feedback,
                    },
                )

                return {"status": "approval recorded"}
            else:
                logging.warning(
                    "No orchestration or plan found for plan_id: %s",
                    human_feedback.m_plan_id
                )
                raise HTTPException(
                    status_code=404, detail="No active plan found for approval"
                )
    except Exception as e:
        logging.error(f"Error processing plan approval: {e}")
        try:
            await connection_config.send_status_update_async(
                {
                    "type": WebsocketMessageType.ERROR_MESSAGE,
                    "data": {
                        "content": "An error occurred while processing your approval request.",
                        "status": "error",
                        "timestamp": asyncio.get_event_loop().time(),
                    },
                },
                user_id,
                message_type=WebsocketMessageType.ERROR_MESSAGE,
            )
        except Exception as ws_error:
            # Don't let WebSocket send failure break the HTTP response
            logging.warning(f"Failed to send WebSocket error: {ws_error}")
        raise HTTPException(status_code=500, detail="Internal server error")


@plan_router.post("/user_clarification")
async def user_clarification(
    human_feedback: messages.UserClarificationResponse, request: Request
):
    """Endpoint to receive user clarification responses for clarification requests sent by the system."""
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]
    if not user_id:
        raise HTTPException(
            status_code=401, detail="Missing or invalid user information"
        )
    try:
        memory_store = await DatabaseFactory.get_database(user_id=user_id)
        user_current_team = await memory_store.get_current_team(user_id=user_id)
        team_id = None
        if user_current_team:
            team_id = user_current_team.team_id
        if not team_id:
            raise HTTPException(
                status_code=404,
                detail="No team selected. Please select a team first.",
            )
        team = await memory_store.get_team_by_id(team_id=team_id)
        if not team:
            raise HTTPException(
                status_code=404,
                detail=f"Team configuration '{team_id}' not found or access denied",
            )
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Error retrieving team configuration: {e}",
        ) from e
    # Set the approval in the orchestration config
    if user_id and human_feedback.request_id:
        # validate rai
        if human_feedback.answer is not None or human_feedback.answer != "":
            if not await rai_success(human_feedback.answer, team, memory_store):
                track_event_if_configured(
                    "RAI failed",
                    {
                        "status": "Plan Clarification ",
                        "description": human_feedback.answer,
                        "request_id": human_feedback.request_id,
                    },
                )
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error_type": "RAI_VALIDATION_FAILED",
                        "message": "Content Safety Check Failed",
                        "description": "Your request contains content that doesn't meet our safety guidelines. Please modify your request to ensure it's appropriate and try again.",
                        "suggestions": [
                            "Remove any potentially harmful, inappropriate, or unsafe content",
                            "Use more professional and constructive language",
                            "Focus on legitimate business or educational objectives",
                            "Ensure your request complies with content policies",
                        ],
                        "user_action": "Please revise your request and try again",
                    },
                )

        if (
            orchestration_config
            and human_feedback.request_id in orchestration_config.clarifications
        ):
            # Use the new event-driven method to set clarification result
            orchestration_config.set_clarification_result(
                human_feedback.request_id, human_feedback.answer
            )
            try:
                result = await PlanService.handle_human_clarification(
                    human_feedback, user_id
                )
                logger.info(f"Human clarification processed: {result}")
            except ValueError as ve:
                logger.error(f"ValueError processing human clarification: {ve}")
            except Exception as e:
                logger.error(f"Error processing human clarification: {e}")
            track_event_if_configured(
                "HumanClarificationReceived",
                {
                    "request_id": human_feedback.request_id,
                    "answer": human_feedback.answer,
                    "user_id": user_id,
                },
            )
            return {
                "status": "clarification recorded",
            }
        else:
            logging.warning(
                f"No orchestration or plan found for request_id: {human_feedback.request_id}"
            )
            raise HTTPException(
                status_code=404, detail="No active plan found for clarification"
            )


@plan_router.post("/agent_message")
async def agent_message_user(
    agent_message: messages.AgentMessageResponse, request: Request
):
    """Endpoint to receive messages from agents (agent -> user communication)."""
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]
    if not user_id:
        raise HTTPException(
            status_code=401, detail="Missing or invalid user information"
        )
    # Set the approval in the orchestration config

    try:
        result = await PlanService.handle_agent_messages(agent_message, user_id)
        logger.info(f"Agent message processed: {result}")
    except ValueError as ve:
        logger.error(f"ValueError processing agent message: {ve}")
    except Exception as e:
        logger.error(f"Error processing agent message: {e}")

    track_event_if_configured(
        "AgentMessageReceived",
        {
            "agent": agent_message.agent,
            "content": agent_message.content,
            "user_id": user_id,
        },
    )
    return {
        "status": "message recorded",
    }


@plan_router.get("/plans")
async def get_plans(request: Request):
    """Retrieve plans for the current user.

    Gets completed plans for the user's current team.
    """
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]
    if not user_id:
        track_event_if_configured(
            "UserIdNotFound", {"status_code": 400, "detail": "no user"}
        )
        raise HTTPException(status_code=400, detail="no user")

    # Initialize memory context
    memory_store = await DatabaseFactory.get_database(user_id=user_id)

    current_team = await memory_store.get_current_team(user_id=user_id)
    if not current_team:
        return []

    all_plans = await memory_store.get_all_plans_by_team_id_status(
        user_id=user_id, team_id=current_team.team_id, status=PlanStatus.completed
    )

    return all_plans


@plan_router.get("/plan")
async def get_plan_by_id(
    request: Request,
    plan_id: Optional[str] = Query(None),
):
    """Retrieve a specific plan by ID."""
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]
    if not user_id:
        track_event_if_configured(
            "UserIdNotFound", {"status_code": 400, "detail": "no user"}
        )
        raise HTTPException(status_code=400, detail="no user")

    # Initialize memory context
    memory_store = await DatabaseFactory.get_database(user_id=user_id)
    try:
        if plan_id:
            plan = await memory_store.get_plan_by_plan_id(plan_id=plan_id)
            if not plan:
                track_event_if_configured(
                    "GetPlanBySessionNotFound",
                    {"status_code": 400, "detail": "Plan not found"},
                )
                raise HTTPException(status_code=404, detail="Plan not found")

            # Use get_steps_by_plan to match the original implementation

            team = await memory_store.get_team_by_id(team_id=plan.team_id) if plan.team_id else None
            agent_messages = await memory_store.get_agent_messages(plan_id=plan.plan_id)
            mplan = plan.m_plan if plan.m_plan else None
            streaming_message = plan.streaming_message if plan.streaming_message else ""
            plan.streaming_message = ""  # clear streaming message after retrieval
            plan.m_plan = None  # remove m_plan from plan object for response
            return {
                "plan": plan,
                "team": team if team else None,
                "messages": agent_messages,
                "m_plan": mplan,
                "streaming_message": streaming_message,
            }
        else:
            track_event_if_configured(
                "GetPlanId", {"status_code": 400, "detail": "no plan id"}
            )
            raise HTTPException(status_code=400, detail="no plan id")
    except Exception as e:
        logging.error(f"Error retrieving plan: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error occurred")
