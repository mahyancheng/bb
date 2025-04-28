"""
main.py ─ FastAPI application entry-point
──────────────────────────────────────────
• Serves   /api/* JSON endpoints (e.g., /api/models)
• Serves   /ws     WebSocket for live agent interaction
• Mounts   static  frontend from /app/frontend
"""

from __future__ import annotations
import asyncio, json, os, sys, traceback
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

# Import API router and agent workflow handler
from .api import router as api_router
from .agent import handle_agent_workflow
# Import defaults only for initial setting
from .llm_handler import PLANNING_TOOLING_MODEL

print(f"Python Executable: {sys.executable}")
print(f"Default Asyncio Policy: {type(asyncio.get_event_loop_policy()).__name__}")

# --- FastAPI App Initialization ---
app = FastAPI(title="Local AI Agent Backend")
app.include_router(api_router, prefix="/api") # Include API routes (like /api/models)

# ─────────────────────────── WebSocket Chat Endpoint ───────────────────
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Handles WebSocket connections for the agent workflow."""
    await websocket.accept()
    client_host = websocket.client.host
    client_port = websocket.client.port
    print(f"WebSocket connection accepted from: {client_host}:{client_port}")

    # --- Default Model Selections (can be overridden by client messages) ---
    current_planner_model = PLANNING_TOOLING_MODEL
    current_browser_model = os.getenv("BROWSER_AGENT_INTERNAL_MODEL", "qwen2.5:7b") # Default if not set
    current_code_model    = os.getenv("DEEPCODER_MODEL", "deepcoder:latest") # Default if not set

    try:
        while True:
            # Wait for a message from the client
            raw_data = await websocket.receive_text()
            try:
                client_data = json.loads(raw_data)
            except json.JSONDecodeError:
                print(f"Received invalid JSON via WebSocket: {raw_data[:100]}...")
                await websocket.send_text("Agent Error: Invalid JSON payload received.")
                continue # Skip processing this message

            # --- Extract data from client payload ---
            user_query = client_data.get("query", "")
            # Update models based on client selection, keeping current if not provided
            current_planner_model = client_data.get("planner_model", current_planner_model)
            current_browser_model = client_data.get("browser_model", current_browser_model)
            current_code_model    = client_data.get("code_model", current_code_model)

            print(f"Received Query: '{user_query[:50]}...', Planner: {current_planner_model}, Browser: {current_browser_model}, Code: {current_code_model}")

            if not user_query:
                await websocket.send_text("Agent Error: Received empty query.")
                continue

            # --- Set Environment Variables for Subprocesses/Tools ---
            # Make the chosen models available to tools running in subprocesses
            os.environ["BROWSER_AGENT_INTERNAL_MODEL"] = current_browser_model
            os.environ["DEEPCODER_MODEL"] = current_code_model # If code tool needs it via env

            # --- Execute Agent Workflow ---
            # *** THE FIX IS HERE: Use 'planner_model_name=' to match agent.py ***
            await handle_agent_workflow(
                user_query=user_query,
                planner_model_name=current_planner_model, # <<< CORRECTED ARGUMENT NAME
                websocket=websocket
            )
            # Workflow completion message is handled within handle_agent_workflow

    except WebSocketDisconnect as e:
        print(f"WebSocket disconnected: {client_host}:{client_port} (Code: {e.code}, Reason: {e.reason})")
    except Exception as e:
        # Catch unexpected errors during WebSocket handling or agent execution
        tb = traceback.format_exc()
        print(f"WebSocket Error or Agent Workflow Error: {e}\n{tb}")
        try:
            # Try to inform the client about the error
            await websocket.send_text(f"Agent Error: An unexpected server error occurred: {e}")
        except Exception as send_err:
            print(f"Failed to send error message to potentially closed WebSocket: {send_err}")
    finally:
        # Ensure WebSocket is closed gracefully if still open
        try: await websocket.close()
        except Exception: pass
        print(f"WebSocket connection closed for {client_host}:{client_port}")

# ──────────────────────── Serve Static Frontend Files ───────────────────
# Determine paths relative to this main.py file
SCRIPT_DIR = Path(__file__).parent # /app/app
ROOT = SCRIPT_DIR.parent          # /app
FRONTEND_DIR = ROOT / "frontend"  # /app/frontend

print(f"Serving static files from container path: {FRONTEND_DIR}")

if FRONTEND_DIR.is_dir() and (FRONTEND_DIR / "index.html").is_file():
    try:
        # Mount the directory at the root URL '/'
        app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="static")
        print(f"Successfully mounted static files from {FRONTEND_DIR} at '/'.")
    except Exception as e:
         print(f"ERROR mounting static files from {FRONTEND_DIR}: {e}")
         @app.get("/")
         async def read_root_mount_error():
             return {"message": "Backend running, but failed to mount frontend static files."}
else:
    print(f"WARNING: Frontend directory '{FRONTEND_DIR}' not found or missing 'index.html' inside the container.")
    @app.get("/")
    async def read_root_no_frontend():
        return {"message": f"Backend running. Frontend directory not found at '{FRONTEND_DIR}'."}