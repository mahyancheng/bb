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
# Import defaults only for initial setting, actual models chosen by user
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
    print(f"WebSocket connection accepted from: {websocket.client.host}:{websocket.client.port}")

    # --- Default Model Selections (can be overridden by client messages) ---
    # These are just initial values before the first message from the client
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

            # --- Set Environment Variables for Subprocesses ---
            # Make the chosen models available to tools running in subprocesses
            # (Mainly for the browser tool which runs run_browser_task.py)
            os.environ["BROWSER_AGENT_INTERNAL_MODEL"] = current_browser_model
            # If code interpreter ran as subprocess, set its model too:
            # os.environ["DEEPCODER_MODEL"] = current_code_model

            # --- Execute Agent Workflow ---
            # Pass the *planner* model explicitly, others are read from env by tools
            await handle_agent_workflow(
                user_query=user_query,
                selected_model=current_planner_model, # Model for planning/correction steps
                websocket=websocket
            )
            # Workflow completion message is handled within handle_agent_workflow

    except WebSocketDisconnect:
        print(f"WebSocket disconnected: {websocket.client.host}:{websocket.client.port} (Code: {websocket.close_code})")
        # Normal disconnect, no action needed
    except Exception as e:
        # Catch unexpected errors during WebSocket handling or agent execution
        tb = traceback.format_exc()
        print(f"WebSocket Error or Agent Workflow Error: {e}\n{tb}")
        try:
            # Try to inform the client about the error
            await websocket.send_text(f"Agent Error: An unexpected server error occurred: {e}")
        except Exception as send_err:
            print(f"Failed to send error message to disconnected WebSocket: {send_err}")
    finally:
        # Ensure WebSocket is closed gracefully if still open
        try:
            await websocket.close()
            print(f"WebSocket connection closed for {websocket.client.host}:{websocket.client.port}")
        except Exception:
            # Ignore errors if already closed
            pass

# ──────────────────────── Serve Static Frontend Files ───────────────────
from pathlib import Path

# main.py sits in /app/app
FRONTEND_DIR = Path(__file__).parent / "frontend"
print(f"Serving static files from {FRONTEND_DIR}")

if FRONTEND_DIR.joinpath("index.html").is_file():
    # serve SPA at root URL
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="static")
else:
    print(f"WARNING: {FRONTEND_DIR}/index.html not found; frontend will not load.")
    @app.get("/")
    async def _missing_frontend():
        return {"error": f"index.html not found in {FRONTEND_DIR}"}

# --- Application Entry Point (for direct run, though uvicorn in Docker is standard) ---
# if __name__ == "__main__":
#     import uvicorn
#     uvicorn.run(app, host="0.0.0.0", port=8000)