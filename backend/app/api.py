from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import json, os

# Import helpers from the updated llm_handler
from .llm_handler import simple_prompt, list_local_models, PLANNING_TOOLING_MODEL

router = APIRouter()

# ─── Endpoint to list available Ollama models ───────────────────
@router.get("/models")
async def list_models_endpoint(): # Use async def for consistency
    """Returns a list of locally available Ollama models."""
    try:
        models = list_local_models()
        # print(f"DEBUG: /api/models returning: {models}") # Optional debug print
        return {"models": models}
    except Exception as e:
        # Log the error on the backend
        print(f"ERROR fetching local models: {e}")
        # Return an error response to the frontend
        raise HTTPException(status_code=500, detail=f"Failed to retrieve models from Ollama: {e}")

# ─── Minimal HTTP chat endpoint (Optional - WebSocket is primary) ─
class ChatInput(BaseModel):
    query: str
    model: str | None = None # Optional model override

@router.post("/chat")
async def chat_http_endpoint(inp: ChatInput):
    """Basic HTTP endpoint for simple prompts (no agent workflow)."""
    model_to_use = inp.model or PLANNING_TOOLING_MODEL # Use specified or default
    try:
        answer = simple_prompt(model=model_to_use, prompt=inp.query)
        if answer is None:
            raise HTTPException(status_code=500, detail="LLM communication failed.")
        return {"response": answer}
    except Exception as e:
         print(f"ERROR in /chat endpoint: {e}")
         raise HTTPException(status_code=500, detail=f"LLM Error: {e}")