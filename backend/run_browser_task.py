#!/usr/bin/env python
"""
run_browser_task.py
───────────────────
Executes Browser-Use’s Agent in isolation. Designed to be called
by browseruse_integration.py in a separate process.

Input (argv[1]): JSON
    {
      "instructions": "<fully-formed prompt>",
      "model":        "qwen2.5:7b"           # required model name
    }

Stdout: exactly one JSON object
    {"result": "..."} on success
    {"error":  "..."} on failure
Exit code 0 iff "result" key is present.
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import sys
import traceback
from dotenv import load_dotenv

# ─── logging ─────────────────────────────────────────────────────
# Basic logging setup, consider adding file logging if needed
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [browser-task] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)

# ─── env ─────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(__file__)
# Load .env from the same directory as this script (expected to be /app inside container)
load_dotenv(os.path.join(BASE_DIR, ".env"), override=True)
# Get Ollama endpoint from environment, provide a default if not set
OLLAMA_ENDPOINT = os.getenv("OLLAMA_ENDPOINT", "http://host.docker.internal:11434") # Default uses docker host gateway
logging.info(f"Ollama Endpoint: {OLLAMA_ENDPOINT}")

# ─── heavy imports (with error handling) ─────────────────────────
try:
    from browser_use.agent.service import Agent as BrowserAgent
    from browser_use.browser.browser import Browser, BrowserConfig
    from browser_use.browser.context import (
        BrowserContextConfig,
        BrowserContextWindowSize,
    )
    from langchain_ollama import ChatOllama
    logging.info("Dependencies loaded successfully.")
except ImportError as e:
    logging.error("Import failure: %s", e)
    # Print error as JSON to stdout so the calling process knows
    print(json.dumps({"error": f"Import Error: {e}"}))
    sys.exit(1)
except Exception as e:
     logging.error("Unexpected error during imports: %s", e)
     print(json.dumps({"error": f"Unexpected Import Error: {e}"}))
     sys.exit(1)

# ───────────────────────────────────────────────── async core ────
async def _run(instructions: str, model: str) -> dict:
    """Core logic to run the browser agent task."""
    llm = None
    browser = None
    ctx = None
    logging.info(f"Starting browser task. Model: {model}, Instructions: {instructions[:100]}...")

    try:
        # 1. Initialize LLM
        logging.info(f"Initializing LLM: {model} at {OLLAMA_ENDPOINT}")
        try:
            llm = ChatOllama(
                model=model,
                base_url=OLLAMA_ENDPOINT,
                temperature=0.0 # Low temperature for predictable behavior
                )
            # Optional: Simple test to ensure LLM connection works
            # await llm.ainvoke("Respond with OK")
            logging.info("LLM initialized.")
        except Exception as e:
            logging.error(f"Failed to initialize LLM '{model}': {e}", exc_info=True)
            return {"error": f"Init LLM '{model}' failed: {e}"}

        # 2. Initialize Browser (headless=False for VNC)
        logging.info("Initializing Browser...")
        try:
            browser = Browser(config=BrowserConfig(headless=False, disable_security=True))
            logging.info("Browser initialized.")
        except Exception as e:
            logging.error(f"Failed to initialize Browser: {e}", exc_info=True)
            return {"error": f"Browser initialization failed: {e}"}

        # 3. Create Browser Context
        logging.info("Creating Browser Context...")
        try:
            ctx = await browser.new_context(
                config=BrowserContextConfig(
                    browser_window_size=BrowserContextWindowSize(width=1280, height=1024)
                )
            )
            logging.info("Browser Context created.")
        except Exception as e:
             logging.error(f"Failed to create Browser Context: {e}", exc_info=True)
             return {"error": f"Browser context creation failed: {e}"}

        # 4. Initialize Browser Agent
        logging.info("Initializing Browser Agent...")
        try:
            agent = BrowserAgent(
                task=instructions, browser=browser, browser_context=ctx, llm=llm, use_vision=False
            )
            logging.info("Browser Agent initialized.")
        except Exception as e:
             logging.error(f"Failed to initialize Browser Agent: {e}", exc_info=True)
             return {"error": f"Browser agent initialization failed: {e}"}

        # 5. Run Agent Task with Timeout
        logging.info("Running agent task...")
        agent_timeout = 240.0 # 4 minutes timeout
        try:
            hist = await asyncio.wait_for(agent.run(), timeout=agent_timeout)
            final_result = hist.final_result() if hasattr(hist, "final_result") else str(hist)
            logging.info(f"Agent task finished. Result: {final_result[:200]}...")
            # Return successful result
            return {"result": final_result or "Browser task finished (empty result)."}

        except asyncio.TimeoutError:
            logging.error(f"Browser task timed out after {agent_timeout} seconds.")
            return {"error": f"Browser task timed out after {agent_timeout}s inside subprocess."}
        except Exception as e:
            logging.error(f"Unexpected error during agent run: {e}", exc_info=True)
            # traceback.print_exc() # Already logged via logging.error with exc_info=True
            return {"error": f"Unexpected error during agent execution: {e}"}

    finally:
        # 6. Cleanup (Essential!)
        logging.info("Cleaning up browser resources...")
        if ctx and hasattr(ctx, 'close'):
            try:
                await ctx.close()
                logging.info("Browser context closed.")
            except Exception as e_ctx:
                 logging.warning(f"Error closing context: {e_ctx}", exc_info=True)
        if browser and hasattr(browser, 'close'):
            try:
                await browser.close()
                logging.info("Browser closed.")
            except Exception as e_brw:
                 logging.warning(f"Error closing browser: {e_brw}", exc_info=True)
        logging.info("Cleanup finished.")

# ───────────────────────────────────────────────── CLI glue ──────
def main():
    """Parses command line arguments and runs the async task."""
    if len(sys.argv) < 2:
        # No JSON input provided
        print(json.dumps({"error": "No JSON input provided via command line argument."}))
        sys.exit(1)

    # Parse input JSON from command line argument
    try:
        input_json_str = sys.argv[1]
        data = json.loads(input_json_str)
        instructions = data["instructions"] # Required key
        # Model is now required in the input payload for clarity
        model = data["model"]
        if not model:
            raise ValueError("'model' key is missing or empty in input JSON.")

    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"Invalid JSON input: {e}. Input: {input_json_str[:100]}..."}))
        sys.exit(1)
    except KeyError as e:
         print(json.dumps({"error": f"Missing required key in input JSON: {e}"}))
         sys.exit(1)
    except ValueError as e:
         print(json.dumps({"error": str(e)}))
         sys.exit(1)
    except Exception as e:
        # Catch any other parsing errors
        print(json.dumps({"error": f"Error parsing input arguments: {e}"}))
        sys.exit(1)

    # Run the async function
    result_dict = asyncio.run(_run(instructions, model))

    # Print the result dictionary as JSON to stdout
    print(json.dumps(result_dict))

    # Exit with appropriate code (0 for success, 1 for error)
    sys.exit(0 if "result" in result_dict else 1)


if __name__ == "__main__":
    main()