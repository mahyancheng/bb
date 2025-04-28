#!/usr/bin/env python
"""
run_browser_task.py
───────────────────
Executes Browser-Use’s Agent in isolation.

Input (argv[1]): JSON {"instructions": "<prompt>", "model": "model:tag"}
Stdout: JSON {"result": "..."} or {"error": "..."}
Exit code 0 on success, 1 on error.
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import sys
import traceback
from dotenv import load_dotenv

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [browser-task] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)

# --- Env ---
BASE_DIR = os.path.dirname(__file__)
load_dotenv(os.path.join(BASE_DIR, ".env"), override=True)
OLLAMA_ENDPOINT = os.getenv("OLLAMA_ENDPOINT", "http://host.docker.internal:11434")
logging.info(f"Ollama Endpoint: {OLLAMA_ENDPOINT}")

# --- Imports ---
try:
    from browser_use.agent.service import Agent as BrowserAgent
    from browser_use.browser.browser import Browser, BrowserConfig
    from browser_use.browser.context import (
        BrowserContextConfig, BrowserContextWindowSize, BrowserContext
    )
    from langchain_ollama import ChatOllama
    logging.info("Dependencies loaded successfully.")
except ImportError as e:
    logging.error("Import failure: %s", e); print(json.dumps({"error": f"Import Error: {e}"})); sys.exit(1)
except Exception as e:
     logging.error("Unexpected import error: %s", e); print(json.dumps({"error": f"Unexpected Import Error: {e}"})); sys.exit(1)

# --- Core Logic ---
async def _run(instructions: str, model: str) -> dict:
    llm = None
    browser: Browser | None = None
    ctx: BrowserContext | None = None
    final_result = None

    # Determine context window size based on model name (VERIFY THESE VALUES)
    default_num_ctx = 8192; num_ctx_to_use = default_num_ctx; model_lower = model.lower()
    if 'llama3' in model_lower: num_ctx_to_use = 8192
    elif 'qwen' in model_lower: num_ctx_to_use = 32768 if any(k in model_lower for k in ['72b','32b','14b','7b']) else 8192
    elif 'mistral' in model_lower or 'mixtral' in model_lower: num_ctx_to_use = 32768
    elif 'phi3' in model_lower: num_ctx_to_use = 128000 if '128k' in model_lower else 4096
    logging.info(f"Using num_ctx={num_ctx_to_use} for model {model}")

    logging.info(f"Starting task. Model: {model}, Ctx: {num_ctx_to_use}, Instr: {instructions[:100]}...")

    try:
        # 1. Init LLM
        logging.info(f"Initializing LLM: {model} at {OLLAMA_ENDPOINT}")
        llm = ChatOllama(model=model, base_url=OLLAMA_ENDPOINT, temperature=0.0, num_ctx=num_ctx_to_use)
        logging.info("LLM initialized.")
        # 2. Init Browser
        logging.info("Initializing Browser...")
        browser = Browser(config=BrowserConfig(headless=False, disable_security=True))
        logging.info("Browser initialized.")
        # 3. Create Context
        logging.info("Creating Browser Context...")
        ctx = await browser.new_context(config=BrowserContextConfig(browser_window_size=BrowserContextWindowSize(width=1280, height=1024)))
        logging.info("Browser Context created.")
        # 4. Init Agent
        logging.info("Initializing Browser Agent...")
        agent = BrowserAgent(task=instructions, browser=browser, browser_context=ctx, llm=llm, use_vision=False)
        logging.info("Browser Agent initialized.")
        # 5. Run Agent Task
        logging.info("Running agent task...")
        agent_timeout = 240.0
        hist = await asyncio.wait_for(agent.run(), timeout=agent_timeout)
        final_result = hist.final_result() if hasattr(hist, "final_result") else str(hist)
        return {"result": final_result or "Browser task finished (empty result)."}

    except asyncio.TimeoutError:
        logging.error(f"Task timed out after {agent_timeout}s.")
        return {"error": f"Browser task timed out after {agent_timeout}s."}
    except Exception as e:
        logging.error(f"Error during task execution: {e}", exc_info=True)
        return {"error": f"Error during agent execution: {e}"}

    finally:
        # 6. Cleanup
        logging.info("Cleaning up browser resources...")
        if final_result is not None: logging.info(f"Final Result: {str(final_result)[:200]}...")
        else: logging.info("Task finished with error or timeout.")
        if ctx:
            try:
                is_closed_method = getattr(ctx, 'is_closed', None)
                if callable(is_closed_method) and not await is_closed_method(): await ctx.close(); logging.info("Context closed.")
                else: logging.info("Context already closed or cannot check.")
            except Exception as e: logging.warning(f"Ctx close error: {e}", exc_info=False)
        if browser:
            try:
                is_connected_method = getattr(browser, 'is_connected', None)
                if callable(is_connected_method) and browser.is_connected(): await browser.close(); logging.info("Browser closed.")
                else: logging.info("Browser disconnected or cannot check.")
            except Exception as e: logging.warning(f"Browser close error: {e}", exc_info=False)
        logging.info("Cleanup finished.")

# --- CLI Glue ---
def main():
    if len(sys.argv) < 2: print(json.dumps({"error": "No JSON input."})); sys.exit(1)
    try:
        input_json_str = sys.argv[1]; data = json.loads(input_json_str)
        instructions = data["instructions"]; model = data["model"]
        if not model: raise ValueError("'model' missing.")
        if not instructions: raise ValueError("'instructions' missing.")
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        print(json.dumps({"error": f"Input Error: {e}"})); sys.exit(1)
    except Exception as e: print(json.dumps({"error": f"Arg parsing error: {e}"})); sys.exit(1)
    result_dict = asyncio.run(_run(instructions, model))
    print(json.dumps(result_dict)); sys.exit(0 if "result" in result_dict else 1)

if __name__ == "__main__": main()