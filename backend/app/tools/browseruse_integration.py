"""
browseruse_integration.py
─────────────────────────
Utility that launches `run_browser_task.py` in a separate Python process.
"""

from __future__ import annotations
import asyncio
import json
import os
import subprocess
import sys
import traceback
import shlex

# --- Paths ---
PYTHON_EXECUTABLE = sys.executable
TOOLS_DIR = os.path.dirname(__file__)
BACKEND_APP_DIR = os.path.dirname(TOOLS_DIR)
BACKEND_DIR = os.path.dirname(BACKEND_APP_DIR)
RUNNER_SCRIPT_PATH = os.path.join(BACKEND_DIR, "run_browser_task.py")

print(f"[Browser Tool] Subprocess Runner Path: {RUNNER_SCRIPT_PATH}")

# ───────────────────────────────────────────────── Prompt Helper ---
# Definition already accepts step_limit
def _build_prompt(user_instruction: str, context_hint: str | None = None, step_limit: int = 15) -> str:
    """Adds a system header to the user instruction for the sub-agent."""
    header = (
        f"You are an autonomous browser agent. Complete the user's task using browser actions. "
        f"Aim for ~{step_limit} actions max. If complex, gather core info & return summary.\n"
        f"Respond with the final answer/summary ONLY.\n"
    )
    if context_hint and context_hint != "No output from previous steps.":
        context_hint_clean = str(context_hint)[:1000]
        header += f"\n**Context from previous workflow steps (use if relevant):**\n{context_hint_clean}\n"
    return header + "\n--- USER TASK ---\n" + user_instruction.strip()

# ───────────────────────────────────────────────── Subprocess Runner ---
async def _run_subprocess(cmd: list[str], timeout: float, websocket):
    """Runs a command in a subprocess using asyncio and logs stderr."""
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"}
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout)
        exit_code = process.returncode
        if stderr_bytes:
             stderr_str = stderr_bytes.decode('utf-8', errors='replace').strip()
             print(f"--- [Browser Subprocess STDERR] ---\n{stderr_str}\n---")
             # Optional: Send snippets to UI for live debugging
             # await websocket.send_text(f"Browser Tool Log: {stderr_str[:200]}...")
        return exit_code, stdout_bytes
    except asyncio.TimeoutError:
        print(f"Browser subprocess timeout ({timeout}s). Killing...")
        try: process.kill(); await process.wait()
        except Exception: pass
        raise # Re-raise TimeoutError

# ───────────────────────────────────────────────── Public Coroutine ---
# *** CORRECTION HERE: Change user_instr to user_instruction ***
async def browse_website(
    user_instruction: str, # <<< RENAMED PARAMETER
    websocket,
    *,
    browser_model: str, # Model name is required
    context_hint: str | None = None,
    step_limit_suggestion: int = 15 # Keep this parameter
) -> str:
    """Launch `run_browser_task.py` subprocess to perform a browser task."""
    if not os.path.exists(RUNNER_SCRIPT_PATH):
        err = f"Error: Browser helper script not found: {RUNNER_SCRIPT_PATH}"
        await websocket.send_text(f"Agent Error: {err}"); print(f"[Browser Tool] {err}"); return err
    if not browser_model:
        err = "Error: No browser_model specified for browse_website."
        await websocket.send_text(f"Agent Error: {err}"); print(f"[Browser Tool] {err}"); return err

    # *** CORRECTION HERE: Use the renamed parameter ***
    instructions_for_subprocess = _build_prompt(user_instruction, context_hint, step_limit_suggestion)

    await websocket.send_text("Browser Tool: Launching isolated browser process...")
    # *** Use the renamed parameter in the log message too ***
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
            result_str = f"Error: Browser subprocess failed (Exit: {exit_code})."
            await websocket.send_text(f"Agent Error: {result_str} See backend logs.")
            print(f"[Browser Tool] {result_str}")
            # Try to decode stdout anyway for potential error messages from the script itself
            if stdout_bytes:
                 stdout_str = stdout_bytes.decode('utf-8', errors='replace').strip()
                 try:
                     error_data = json.loads(stdout_str)
                     if "error" in error_data: result_str += f" Subprocess Error: {error_data['error']}"
                 except json.JSONDecodeError: result_str += f" Raw stdout: {stdout_str[:200]}..."
            return result_str # Return the error string

        # Exit code 0, process stdout
        stdout_str = stdout_bytes.decode('utf-8', errors='replace').strip() if stdout_bytes else ""
        if not stdout_str:
             await websocket.send_text("Agent Warning: Browser process finished successfully but produced no output.")
             print("[Browser Tool] Warning: Subprocess exited 0 with empty stdout.")
             return "Browser action completed with no specific output."

        # Decode stdout JSON
        try: result_data = json.loads(stdout_str)
        except json.JSONDecodeError:
            err = "Error: Browser process returned non-JSON output."; await websocket.send_text(f"Agent Error: {err}")
            print(f"[Browser Tool] Invalid JSON. Raw:\n{stdout_str}\n---"); return f"{err} Raw: {stdout_str[:200]}..."

        # Check for 'error' key in the JSON result
        if "error" in result_data:
            err = f"Error from browser task: {result_data['error']}"; await websocket.send_text(f"Agent Error: {err[:200]}...")
            print(f"[Browser Tool] {err}"); return err

        # Success case: Extract 'result' key
        final_result = result_data.get("result", "Browser task finished (no 'result' key).")
        await websocket.send_text("Browser Tool: Action completed successfully.")
        print(f"[Browser Tool] Success. Result: {final_result[:200]}..."); return final_result

    except asyncio.TimeoutError:
        err = f"Error: Browser subprocess exceeded hard timeout ({timeout_seconds}s)."
        await websocket.send_text(f"Agent Error: {err}"); print(f"[Browser Tool] {err}"); return err
    except Exception as e:
        # Catch unexpected errors during subprocess launch or management
        tb = traceback.format_exc(); err = f"Error launching/managing browser process: {e}"
        await websocket.send_text(f"Agent Error: {err}"); print(f"[Browser Tool] {err}\n{tb}"); return err