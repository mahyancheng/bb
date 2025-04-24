"""
browseruse_integration.py
─────────────────────────
Launches run_browser_task.py in a sandboxed subprocess.

Key Improvements 24-Apr-2025
────────────────────────────
1.  Re-written `_build_prompt()` so Browser-Use’s internal agent receives
    *one* system message followed by a *single* user message.  
    • Removes the dashed separator that sometimes fooled the LLM into
      thinking there were two user turns.  
    • Adds explicit obligations:

        – “think step-by-step but only SHOW the final answer”  
        – “never mention internal actions or HTML”

2.  Adds `step_limit_suggestion` to the payload we pass to the runner so
    the child script can print it to the log (nice when debugging).

3.  Tightens logging and always echoes the **exact** prompt text to the
    FastAPI websocket (wrapped in a collapsible block) for visibility.

Everything else (timeout, error handling, etc.) is unchanged.
"""

from __future__ import annotations
import asyncio, json, os, shlex, subprocess, sys, traceback

# ——— Paths ————————————————————————————————————————————————
PYTHON_EXECUTABLE   = sys.executable
TOOLS_DIR           = os.path.dirname(__file__)
BACKEND_APP_DIR     = os.path.dirname(TOOLS_DIR)
BACKEND_DIR         = os.path.dirname(BACKEND_APP_DIR)
RUNNER_SCRIPT_PATH  = os.path.join(BACKEND_DIR, "run_browser_task.py")

print(f"[Browser Tool] Subprocess Runner Path: {RUNNER_SCRIPT_PATH}")

# ——— Prompt helper ——————————————————————————————————————————
def _build_prompt(
    user_instruction: str,
    context_hint: str | None = None,
    step_limit: int = 15
) -> str:
    """
    Returns ONE string = system + user message separated by \\n\\n.
    Browser-Use splits on first \\n\\n into system/user automatically.
    """
    system_header = (
        "You are an autonomous browser agent running **inside a sandboxed "
        "Chromium**. You can click, type and read like a human.\n"
        f"- Finish in ≲{step_limit} actions.\n"
        "- Think step-by-step **internally**, but **output only the final answer**.\n"
        "- Do **NOT** reveal HTML, internal thoughts or the action log.\n"
        "- If you cannot finish, summarise what you found so far."
    )

    body = user_instruction.strip()
    if context_hint and context_hint != "No output from previous steps.":
        body += "\n\n(Background context from earlier steps)\n" + context_hint.strip()

    return system_header + "\n\n" + body


# ——— Async helper to run subprocess ———————————————————————
async def _run_subprocess(cmd: list[str], timeout: float, websocket):
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"}
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise

    if stderr_b:
        print("--- [Browser Subprocess STDERR] ---")
        print(stderr_b.decode("utf-8", errors="replace"))
        print("-----------------------------------")

    return proc.returncode, stdout_b


# ——— Public coroutine ————————————————————————————————
async def browse_website(
    user_instruction: str,
    websocket,
    *,
    browser_model: str,
    context_hint: str | None = None,
    step_limit_suggestion: int = 15
) -> str:
    if not os.path.exists(RUNNER_SCRIPT_PATH):
        err = f"Error: helper script missing at {RUNNER_SCRIPT_PATH}"
        await websocket.send_text(f"Agent Error: {err}")
        return err

    if not browser_model:
        err = "Error: browser_model not supplied to browse_website()"
        await websocket.send_text(f"Agent Error: {err}")
        return err

    prompt   = _build_prompt(user_instruction, context_hint, step_limit_suggestion)
    payload  = json.dumps({
        "instructions": prompt,
        "model": browser_model,
        "step_limit": step_limit_suggestion
    })

    # show prompt in UI (collapsible code fence)
    await websocket.send_text(
        "Tool Input: ```browser\n" + prompt[:1500] + ("\n...```" if len(prompt) > 1500 else "```")
    )

    cmd = [PYTHON_EXECUTABLE, RUNNER_SCRIPT_PATH, payload]
    print("[Browser Tool] Launch:", " ".join(shlex.quote(p) for p in cmd))

    try:
        exit_code, stdout_b = await _run_subprocess(cmd, timeout=240.0, websocket=websocket)
    except asyncio.TimeoutError:
        err = "Error: Browser subprocess timed out after 240 s."
        await websocket.send_text(f"Agent Error: {err}")
        return err

    stdout = stdout_b.decode("utf-8", errors="replace").strip()
    if exit_code != 0:
        await websocket.send_text(f"Agent Error: browser task failed (exit {exit_code})")
        return f"Error: subprocess exit {exit_code}"

    try:
        data = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return "Error: Browser subprocess returned non-JSON output."

    if "error" in data:
        return f"Error from browser task: {data['error']}"

    return data.get("result", "(browser task finished with no result)")
