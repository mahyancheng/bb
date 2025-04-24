"""
browseruse_integration.py
─────────────────────────
Utility that launches `run_browser_task.py` in a separate Python process.

Public coroutine
----------------
    browse_website(
        user_instruction: str,
        websocket,
        *,
        browser_model: str,  # Model name is now required
        context_hint: str | None = None
    ) -> str

Returns the final summary string from the isolated browser task, or an
error string starting with “Error: …”.
"""

from __future__ import annotations
import asyncio
import json
import os
import subprocess
import sys
import traceback
import shlex # For safe command joining/quoting in logs

# --- Paths ---
PYTHON_EXECUTABLE = sys.executable # Use the same Python interpreter running this backend
# Calculate path to run_browser_task.py relative to this file's location
TOOLS_DIR = os.path.dirname(__file__) # /app/app/tools
BACKEND_APP_DIR = os.path.dirname(TOOLS_DIR) # /app/app
BACKEND_DIR = os.path.dirname(BACKEND_APP_DIR) # /app
RUNNER_SCRIPT_PATH = os.path.join(BACKEND_DIR, "run_browser_task.py") # /app/run_browser_task.py

print(f"[Browser Tool] Subprocess Runner Path: {RUNNER_SCRIPT_PATH}")

# ───────────────────────────────────────────────── Prompt Helper ---
def _build_prompt(user_instruction: str, context_hint: str | None = None, step_limit: int = 15) -> str:
    """Adds a system header to the user instruction for the sub-agent."""
    header = (
        f"You are an autonomous browser agent running in isolation. Your goal is to complete the user's task using browser actions. "
        f"Aim to complete the task efficiently, ideally within {step_limit} internal actions. "
        f"If the task is complex and likely to exceed this, focus on gathering the core information and return a summary.\n"
        f"Respond with the final answer or summary ONLY.\n"
    )
    if context_hint and context_hint != "No output from previous steps.": # Avoid adding default hint
        # Sanitize hint slightly - limit length?
        context_hint_clean = str(context_hint)[:1000] # Limit context length passed
        header += f"\n**Context from previous workflow steps (use if relevant):**\n{context_hint_clean}\n"

    return header + "\n--- USER TASK ---\n" + user_instruction.strip()

# ───────────────────────────────────────────────── Subprocess Runner ---
async def _run_subprocess(cmd: list[str], timeout: float, websocket):
    """Runs a command in a subprocess using asyncio and logs stderr."""
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"} # Ensure UTF-8
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout)
        exit_code = process.returncode

        # Log stderr from the subprocess for debugging
        if stderr_bytes:
             stderr_str = stderr_bytes.decode('utf-8', errors='replace').strip()
             print(f"--- [Browser Subprocess STDERR] ---\n{stderr_str}\n---")
             # Optionally send stderr snippets to UI websocket for live debugging
             # await websocket.send_text(f"Browser Tool Log: {stderr_str[:200]}...")

        return exit_code, stdout_bytes # Return exit code and raw stdout bytes

    except asyncio.TimeoutError:
        print(f"Browser subprocess timed out after {timeout}s. Killing process...")
        try:
            process.kill()
            await process.wait() # Wait for kill to complete
        except ProcessLookupError: pass # Already terminated
        except Exception as kill_err: print(f"Error killing timed-out process: {kill_err}")
        raise # Re-raise TimeoutError to be caught by caller

# ───────────────────────────────────────────────── Public Coroutine ---
async def browse_website(
    user_instruction: str,
    websocket,
    *,
    browser_model: str, # Make model required
    context_hint: str | None = None,
    step_limit_suggestion: int = 15 # Allow passing suggestion from agent
) -> str:
    """
    Launch `run_browser_task.py` subprocess to perform a browser task.
    """
    if not os.path.exists(RUNNER_SCRIPT_PATH):
        err = f"Error: Browser helper script not found at {RUNNER_SCRIPT_PATH}"
        await websocket.send_text(f"Agent Error: {err}")
        print(f"[Browser Tool] {err}")
        return err

    # Ensure a model name is provided
    if not browser_model:
        err = "Error: No browser_model specified for browse_website tool."
        await websocket.send_text(f"Agent Error: {err}")
        print(f"[Browser Tool] {err}")
        return err

    # Build the detailed instruction prompt for the sub-agent
    instructions_for_subprocess = _build_prompt(user_instruction, context_hint, step_limit_suggestion)

    await websocket.send_text("Browser Tool: Launching isolated browser process...")
    print(f"[Browser Tool] Model: {browser_model}, Instruction: {user_instruction[:100]}...")

    # Prepare JSON payload for the subprocess
    payload = json.dumps({
        "instructions": instructions_for_subprocess,
        "model": browser_model # Pass the required model name
        })
    cmd = [PYTHON_EXECUTABLE, RUNNER_SCRIPT_PATH, payload]
    cmd_str_log = " ".join(shlex.quote(p) for p in cmd) # Safely quoted command for logging
    print(f"[Browser Tool] Executing: {cmd_str_log}")

    timeout_seconds = 240.0 # Overall timeout for the subprocess

    try:
        exit_code, stdout_bytes = await _run_subprocess(cmd, timeout=timeout_seconds, websocket=websocket)

        # Process result based on exit code
        if exit_code != 0:
            result_str = f"Error: Browser subprocess failed with exit code {exit_code}."
            await websocket.send_text(f"Agent Error: {result_str} Check backend logs for stderr.")
            print(f"[Browser Tool] {result_str}")
            # Try to decode stdout anyway for potential error messages from the script itself
            if stdout_bytes:
                 stdout_str = stdout_bytes.decode('utf-8', errors='replace').strip()
                 try:
                     error_data = json.loads(stdout_str)
                     if "error" in error_data:
                          result_str += f" Subprocess Error: {error_data['error']}"
                 except json.JSONDecodeError:
                      result_str += f" Raw stdout: {stdout_str[:200]}..." # Include partial raw output
            return result_str # Return the error string

        # Exit code 0, process stdout
        stdout_str = stdout_bytes.decode('utf-8', errors='replace').strip() if stdout_bytes else ""
        if not stdout_str:
             # Handle case where script exits 0 but prints nothing
             await websocket.send_text("Agent Warning: Browser subprocess finished successfully but produced no output.")
             print("[Browser Tool] Warning: Subprocess exited 0 with empty stdout.")
             return "Browser action completed with no specific output."

        # Decode stdout JSON
        try:
            result_data = json.loads(stdout_str)
        except json.JSONDecodeError:
            err = "Error: Browser subprocess returned non-JSON output."
            await websocket.send_text(f"Agent Error: {err}")
            print(f"[Browser Tool] Invalid JSON received. Raw stdout:\n{stdout_str}\n---")
            return f"{err} Raw output: {stdout_str[:200]}..." # Return error with snippet

        # Check for 'error' key in the JSON result
        if "error" in result_data:
            subprocess_error = result_data['error']
            await websocket.send_text(f"Agent Error: Browser subprocess reported: {subprocess_error[:200]}...")
            print(f"[Browser Tool] Error received from subprocess: {subprocess_error}")
            return f"Error from browser task: {subprocess_error}"

        # Success case: Extract 'result' key
        final_result = result_data.get("result", "Browser task finished (no 'result' key found in output).")
        await websocket.send_text("Browser Tool: Action completed successfully.")
        print(f"[Browser Tool] Success. Result: {final_result[:200]}...")
        return final_result

    except asyncio.TimeoutError:
        err = f"Error: Browser subprocess exceeded hard timeout ({timeout_seconds}s)."
        await websocket.send_text(f"Agent Error: {err}")
        print(f"[Browser Tool] {err}")
        return err
    except Exception as e:
        # Catch unexpected errors during subprocess launch or management
        tb = traceback.format_exc()
        err = f"Error: Unexpected failure launching or managing browser subprocess: {e}"
        await websocket.send_text(f"Agent Error: {err}")
        print(f"[Browser Tool] {err}\n{tb}")
        return err