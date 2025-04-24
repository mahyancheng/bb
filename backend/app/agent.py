# backend/app/agent.py

import os
import datetime
import asyncio
import traceback
import json
import re
import time # Import time for potential delays if needed
import shlex # For safe splitting of shell commands if needed

# Attempt to import json_repair, warn if not available
try:
    from json_repair import repair_json
    JSON_REPAIR_AVAILABLE = True
except ImportError:
    JSON_REPAIR_AVAILABLE = False
    print("Warning: 'json-repair' library not found. Run 'pip install json-repair' for better JSON parsing robustness.")
    def repair_json(s): # Define a dummy function if library is missing
        return s # Just return the original string

from .prompt_template import SYSTEM_PROMPT
from .llm_handler import (
    # Use the simplified wrappers from the updated llm_handler
    simple_prompt, # Replacement for send_prompt/send_prompt_with_functions
    PLANNING_TOOLING_MODEL,
    # DEEPCODER_MODEL is now set via env var passed to tool if needed
)

# Import tool execution functions
from .tools.shell_terminal import execute_shell_command as execute_shell_command_impl
from .tools.code_interpreter import execute_python_code as execute_python_code_impl
from .tools.browseruse_integration import browse_website as browse_website_impl

# -------------------------------------------------------------------
# Configuration
# -------------------------------------------------------------------
# TASK_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "tasks"))
# os.makedirs(TASK_DIR, exist_ok=True) # Keep for potential future use or debugging, but not used in main flow
MAX_RETRIES = 2 # Max retries for a single failing step
MAX_WORKFLOW_STEPS = 10 # Overall limit on number of steps executed in one workflow run
BROWSER_STEP_LIMIT_SUGGESTION = 15 # Hint passed to browser sub-agent
# -------------------------------------------------------------------

# -------------------------------------------------------------------
# Helper: Send Task List Update
# -------------------------------------------------------------------
async def send_task_update(websocket, tasks_with_status):
    """Formats tasks with status and sends via WebSocket using 'Agent Task Update:' prefix."""
    try:
        # Ensure keys expected by frontend ('description', 'status') are present
        tasks_for_ui = [
            {
                "description": t.get("description", "Unnamed Task"),
                "status": t.get("status", "pending") # Ensure status exists, default to pending
             }
            for t in tasks_with_status
        ]
        payload = json.dumps(tasks_for_ui)
        # Use a specific prefix for task updates for frontend routing
        await websocket.send_text(f"TASK_LIST_UPDATE:{payload}")
        # print(f"DEBUG: Sent task update: {payload}") # Optional debug print
    except Exception as e:
        print(f"Error sending task update: {e}")
        # Optionally inform the client about the failure
        try:
            await websocket.send_text(f"Agent Error: Failed to send task list update to UI.")
        except Exception:
            pass # Ignore error if websocket is already closed

# -------------------------------------------------------------------
# Step 0: Parse the JSON plan produced by the LLM
# -------------------------------------------------------------------
def parse_plan(plan_json: str):
    """
    Parse the LLM's plan JSON into a list of task dicts.
    Attempts to repair JSON before parsing. Adds default descriptions.
    Raises ValueError if invalid JSON or unexpected format.
    Returns a list of dictionaries, e.g., [{'tool': 'shell', 'description': '...', 'command': ['ls']}]
    """
    original_plan_json = plan_json # Keep original for error messages
    try:
        # 1. Clean potential markdown code fences first
        plan_json_cleaned = re.sub(r'^```json\s*|\s*```$', '', plan_json, flags=re.MULTILINE | re.DOTALL).strip()
        if not plan_json_cleaned:
            raise ValueError("Received empty plan after cleaning markdown fences.")

        # 2. Attempt to repair the JSON string (if library available)
        repaired_json_string = repair_json(plan_json_cleaned) # Uses dummy if json-repair not installed
        parsed_plan = None

        # 3. Try parsing the (potentially) repaired JSON
        try:
            parsed_plan = json.loads(repaired_json_string)
        except json.JSONDecodeError as repair_decode_error:
            # If repair failed or wasn't effective, try parsing the original cleaned string
            print(f"Warning: Failed to parse potentially repaired JSON ({repair_decode_error}). Falling back to cleaned original.")
            try:
                parsed_plan = json.loads(plan_json_cleaned)
            except json.JSONDecodeError as final_decode_error:
                # If both fail, raise a detailed error
                error_detail = f"Invalid JSON plan received (JSONDecodeError: {final_decode_error})"
                if repaired_json_string and repaired_json_string != plan_json_cleaned:
                    error_detail += f"\nRepair attempt output (may be invalid):\n{repaired_json_string[:500]}..."
                raise ValueError(error_detail) from final_decode_error

        # 4. Validate the structure (must be a list of dictionaries with 'tool' key)
        if parsed_plan is None: # Safeguard
             raise ValueError("Failed to parse JSON plan after cleaning and repair attempts.")

        if not isinstance(parsed_plan, list):
            # Handle case where LLM might return a single dict instead of a list
            if isinstance(parsed_plan, dict) and 'tool' in parsed_plan:
                print("Warning: LLM returned a single task dict, wrapping in a list.")
                parsed_plan = [parsed_plan]
            else:
                raise ValueError(f"Plan is not a list of tasks. Received type: {type(parsed_plan)}")

        validated_tasks = []
        for idx, task in enumerate(parsed_plan):
            if not isinstance(task, dict):
                raise ValueError(f"Item at index {idx} in plan is not a dictionary: {task}")
            if 'tool' not in task:
                raise ValueError(f"Task at index {idx} is missing 'tool' key: {task}")

            # Ensure description exists, provide a default if missing or empty
            if 'description' not in task or not task.get('description'):
                tool_name = task.get('tool', 'unknown_tool')
                # Try to get some parameter info for the default description
                params = task.get('command') or task.get('code') or task.get('input') or task.get('browser_input') or ''
                param_str = str(params)
                if len(param_str) > 50: param_str = param_str[:47] + '...'
                task['description'] = f"Run {tool_name}" + (f" ({param_str})" if param_str else f" (step {idx+1})")
                print(f"Warning: Task at index {idx} missing description. Using default: '{task['description']}'")

            validated_tasks.append(task)

        return validated_tasks

    except ValueError as e: # Catch errors from validation or parsing
         # Ensure the original raw plan is included in the error message for debugging
         raise ValueError(f"Invalid plan structure or JSON parsing failed: {e}\nOriginal Plan JSON received:\n{original_plan_json}") from e
    except Exception as e: # Catch unexpected errors
         raise ValueError(f"Unexpected error during plan parsing: {e}\nOriginal Plan JSON received:\n{original_plan_json}") from e


# -------------------------------------------------------------------
# Step 1b: Review & auto‑repair a failing tool invocation
# -------------------------------------------------------------------
async def review_and_resolve(task: dict, result: str, attempt: int, websocket):
    """
    If `result` contains error indicators and we haven't exhausted retries,
    ask the LLM to return one corrected JSON tool call.
    Returns the corrected task dict or None.
    """
    # Check for common error indicators (case-insensitive)
    is_error = any(err_indicator in result.lower() for err_indicator in
                   ["error:", "failed", "exception", "traceback", "exit code:", "command not found", "module not found", "permission denied", "timeout", "invalid json"]) # Added more indicators

    if is_error and attempt < MAX_RETRIES:
        task_desc = task.get("description", f"Execute {task.get('tool', 'unknown tool')}")
        # Prepare JSON representation of the failed task for the prompt (exclude internal status)
        failed_task_json = json.dumps({k: v for k, v in task.items() if k != 'status'}, indent=2)

        prompt = (
            f"The following agent step failed (Attempt {attempt + 1}/{MAX_RETRIES}):\n"
            f"**Task Description:** {task_desc}\n"
            f"**Tool Call JSON that Failed:**\n```json\n{failed_task_json}\n```\n\n"
            f"**Output/Error from the tool:**\n```\n{result}\n```\n\n"
            "Analyze the error and the original tool call. Provide **only** the corrected JSON tool call needed to fix the error and achieve the original task goal. Ensure the JSON includes 'tool', 'description', and necessary parameter keys (like 'command', 'code', or 'input'). Your output must be **only** valid JSON, without any markdown fences or explanations."
        )
        await websocket.send_text(f"Agent: Reviewing failure (attempt {attempt + 1}) and trying to resolve...")
        # Use the planning model for correction
        corrected_json_str = simple_prompt( # Use simple_prompt wrapper
            model=PLANNING_TOOLING_MODEL,
            prompt=prompt,
            system=SYSTEM_PROMPT # Provide context
        )

        if not corrected_json_str:
            await websocket.send_text("Agent Warning: LLM failed to provide a correction suggestion.")
            return None

        try:
            # Clean potential markdown fences and repair/parse
            corrected_json_str = re.sub(r'^```json\s*|\s*```$', '', corrected_json_str, flags=re.MULTILINE | re.DOTALL).strip()
            if not corrected_json_str:
                raise ValueError("LLM returned empty correction string after cleaning.")

            repaired_correction = repair_json(corrected_json_str) # Attempt repair
            corrected_task = json.loads(repaired_correction)

            # Validate the correction structure
            if not isinstance(corrected_task, dict) or 'tool' not in corrected_task:
                raise ValueError("Correction is not a valid task dictionary (must be object with 'tool' key).")
            if 'description' not in corrected_task or not corrected_task.get('description'):
                 # If LLM omits description, reuse the original one
                 corrected_task['description'] = task.get('description', f"Execute {corrected_task.get('tool', 'unknown tool')} (corrected)")

            await websocket.send_text("Agent: Received potential correction from LLM.")
            # Return the full corrected task dictionary
            return corrected_task
        except (json.JSONDecodeError, ValueError) as e:
            await websocket.send_text(f"Agent Error: Failed to parse LLM correction: {e}\nRaw correction received:\n{corrected_json_str}")
            return None # Failed to parse correction
    elif is_error:
        # Error occurred, but max retries reached or self-correction not attempted
        await websocket.send_text(f"Agent: Step failed, and max retries ({MAX_RETRIES}) reached or self-correction skipped.")

    return None # No correction needed or possible

# -------------------------------------------------------------------
# Step 1→3: Main Agent Workflow (With Task Updates & Step Limit)
# -------------------------------------------------------------------
async def handle_agent_workflow(user_query: str, selected_model: str, websocket):
    """
    Main agent execution loop: Plan -> Send Tasks -> Execute Steps -> Finalize.
    Uses the selected_model for planning/correction.
    """
    tasks_with_status = [] # Holds full task info + status: [{'description': '...', 'status': '...', 'original_task': {...}, ...}]
    final_agent_message = "Agent: Workflow finished." # Default message
    workflow_stopped_prematurely = False # Flag for limit or failure stopping

    try:
        # 1) PLAN
        await websocket.send_text("Agent: Planning steps based on your request...")
        # Use the model selected in the UI (passed as selected_model) for planning
        planning_model_to_use = selected_model # Use the model from UI dropdown
        print(f"Using Planning/Tooling Model: {planning_model_to_use}")

        # Refined planning prompt emphasizing JSON structure and escaping
        planning_prompt = (
            f"User request: '{user_query}'\n\n"
            "Based on the user request and the available tools (shell_terminal, code_interpreter, browser), generate a plan as a JSON list of dictionaries. Each dictionary must represent one step and include:\n"
            "1. `tool`: The name of the tool (string).\n"
            "2. `description`: A short, user-friendly description (string).\n"
            "3. Tool-specific parameters (e.g., `command`: list of strings, `code`: string, `input`: string).\n\n"
            "**CRITICAL JSON FORMATTING:**\n"
            "- The entire output MUST be a single, valid JSON list (`[...]`).\n"
            "- **Python Code Escaping:** If using `code_interpreter`, the value for the `code` key MUST be a single JSON string. All special characters within the Python code (newlines, backslashes, quotes) MUST be properly escaped (e.g., `\\n`, `\\\\`, `\\\"`). Do NOT use Python triple quotes (`\"\"\"`) inside the JSON string value.\n"
            "- Ensure all strings within the JSON are correctly quoted and escaped.\n\n"
            f"Aim for a concise plan, ideally around {MAX_WORKFLOW_STEPS} steps or fewer.\n"
            "Output **only** the valid JSON list, without any surrounding text or markdown fences."
        )

        # Use the simple_prompt wrapper which handles the underlying ollama call
        plan_json = simple_prompt(
            model=planning_model_to_use,
            prompt=planning_prompt,
            system=SYSTEM_PROMPT # Provide capabilities/context
        )

        if not plan_json:
            raise ValueError("LLM failed to generate a plan (returned empty).")

        # Parse the plan (raises ValueError on failure)
        raw_tasks = parse_plan(plan_json)

        # Initialize tasks with 'pending' status for UI tracking
        tasks_with_status = [
            {
                'description': task.get('description'), # Use validated description
                'status': 'pending',
                'original_task': task, # Store the parsed task data
                'result': None,
                'final_executed_task': None # Store the version eventually run (after potential fixes)
             }
            for task in raw_tasks
        ]

        # 2) SEND Initial Task List to UI
        await send_task_update(websocket, tasks_with_status)
        if not tasks_with_status:
            await websocket.send_text("Agent: Plan generated, but no actionable steps found.")
            final_agent_message = "Agent: No actionable steps planned."
            return # End if no tasks
        else:
            await websocket.send_text(f"Agent: Plan generated with {len(tasks_with_status)} steps.")
        await asyncio.sleep(0.1) # Small delay for UI update

        # 3) EXECUTE STEPS (Loop with Step Limit)
        last_successful_result = "No output from previous steps."
        executed_step_count = 0

        for idx, task_info in enumerate(tasks_with_status):

            # ===>>> Check Step Limit BEFORE starting the step <<<===
            if executed_step_count >= MAX_WORKFLOW_STEPS:
                await websocket.send_text(f"**Agent Warning: Maximum step limit ({MAX_WORKFLOW_STEPS}) reached. Stopping workflow.**")
                final_agent_message = f"Agent: Workflow stopped after reaching the limit of {MAX_WORKFLOW_STEPS} executed steps."
                workflow_stopped_prematurely = True
                # Mark remaining tasks as pending (or skipped)
                for i in range(idx, len(tasks_with_status)):
                    tasks_with_status[i]['status'] = 'pending' # Keep as pending, as they weren't started
                await send_task_update(websocket, tasks_with_status) # Final task update before breaking
                break # Exit the execution loop

            # --- Update UI: Mark as Running ---
            tasks_with_status[idx]['status'] = 'running'
            await send_task_update(websocket, tasks_with_status)
            await websocket.send_text(f"**Agent: Starting Step {idx + 1}/{len(tasks_with_status)}: {task_info['description']}**")
            await asyncio.sleep(0.1)

            current_task_dict = task_info['original_task'].copy() # Work on a copy for the retry loop
            step_result = "Error: Step execution did not produce a result." # Default if loop skipped
            final_task_executed_this_step = current_task_dict

            # --- Retry loop for self-repair ---
            for attempt in range(MAX_RETRIES + 1):
                tool = current_task_dict.get("tool")
                current_attempt_result = "" # Reset for this attempt
                tool_input_log_msg = f"Tool Input ({tool}): {json.dumps({k:v for k,v in current_task_dict.items() if k not in ['description', 'tool', 'status']}, indent=2)}"
                await websocket.send_text(tool_input_log_msg) # Log input to UI
                print(f"Executing Step {idx+1}, Attempt {attempt+1}: Tool={tool}, Task='{task_info['description']}'")

                try:
                    # === Tool Execution ===
                    if tool == "shell_terminal":
                        cmd_list = current_task_dict.get("command", [])
                        # Handle if LLM provides string instead of list
                        if isinstance(cmd_list, str):
                             print("Warning: Received shell command as string, attempting shlex split.")
                             cmd_list = shlex.split(cmd_list)
                        full_cmd = " ".join(cmd_list) # For execution function if it expects a string
                        current_attempt_result = await execute_shell_command_impl(full_cmd, websocket)

                    elif tool == "code_interpreter":
                        code = current_task_dict.get("code", "")
                        if not code: raise ValueError("Missing 'code' parameter for code_interpreter")
                        current_attempt_result = await execute_python_code_impl(code, websocket)

                    elif tool == "browser":
                        inp = current_task_dict.get("input") or current_task_dict.get("browser_input", "")
                        if not inp: raise ValueError("Missing 'input' or 'browser_input' for browser tool")
                        # Get the designated browser model from environment (set in main.py)
                        browser_model_name = os.getenv("BROWSER_AGENT_INTERNAL_MODEL", "qwen2.5:7b") # Fallback needed?

                        # Pass the model name to the browser tool wrapper
                        current_attempt_result = await browse_website_impl(
                             user_instruction=inp,
                             websocket=websocket,
                             browser_model=browser_model_name, # Pass the selected model
                             context_hint=last_successful_result # Pass previous result as context
                        )
                    else:
                        current_attempt_result = f"Error: Unknown tool '{tool}' specified in plan."
                        await websocket.send_text(f"Agent Error: Step {idx+1} specifies unknown tool '{tool}'.")
                        break # No point retrying unknown tool

                    # --- Check for errors in this attempt ---
                    step_result = current_attempt_result # Store result of this attempt
                    is_error = any(err_indicator in step_result.lower() for err_indicator in
                                   ["error:", "failed", "exception", "traceback", "exit code:", "command not found", "module not found", "permission denied", "timeout", "invalid json"])

                    # Log raw tool output
                    await websocket.send_text(f"Tool Output (Attempt {attempt + 1}):\n```\n{step_result}\n```")
                    print(f"Step {idx+1}, Attempt {attempt+1} Raw Result: {step_result[:300]}...") # Truncated log

                    if not is_error:
                        final_task_executed_this_step = current_task_dict # Track successful version
                        break # Exit retry loop on success

                    # --- Error occurred, try self-correction ---
                    await websocket.send_text(f"Agent: Step {idx + 1} encountered an error (Attempt {attempt + 1}).")
                    corrected_task_dict = await review_and_resolve(current_task_dict, step_result, attempt, websocket)

                    if corrected_task_dict:
                        await websocket.send_text(f"Agent: Applying correction for step {idx + 1} (Attempt {attempt + 2})...")
                        # Update description in main list if correction changed it
                        if 'description' in corrected_task_dict and corrected_task_dict['description'] != tasks_with_status[idx]['description']:
                            tasks_with_status[idx]['description'] = corrected_task_dict['description']
                            await send_task_update(websocket, tasks_with_status) # Update UI

                        current_task_dict = corrected_task_dict # Use corrected for next attempt
                        final_task_executed_this_step = current_task_dict # Track corrected version

                    else: # No correction provided or possible
                         if attempt < MAX_RETRIES:
                             await websocket.send_text(f"Agent: Could not resolve error for step {idx + 1} after review.")
                         else: # Max retries reached
                             await websocket.send_text(f"Agent: Max retries ({MAX_RETRIES}) reached for step {idx + 1}. Failing step.")
                         break # Exit retry loop

                except Exception as tool_exec_err:
                    # Catch unexpected errors *during* tool call/retry logic
                    tb = traceback.format_exc()
                    step_result = f"Error: Unhandled exception during tool execution: {tool_exec_err}\n{tb}"
                    await websocket.send_text(f"Agent Error: Critical error executing tool '{tool}' in step {idx+1}: {tool_exec_err}")
                    print(f"Critical Tool Execution Error:\n{tb}")
                    break # Exit retry loop on critical error

            # --- After Retry Loop Finishes for the Step ---
            executed_step_count += 1 # Increment counter *after* all attempts for the step

            # Update final status and result for the step in the main list
            final_status_is_error = any(err_indicator in step_result.lower() for err_indicator in
                                        ["error:", "failed", "exception", "traceback", "exit code:", "command not found", "module not found", "permission denied", "timeout", "invalid json"])
            final_status = 'error' if final_status_is_error else 'done'

            tasks_with_status[idx]['status'] = final_status
            tasks_with_status[idx]['final_executed_task'] = final_task_executed_this_step
            tasks_with_status[idx]['result'] = step_result

            await send_task_update(websocket, tasks_with_status) # Update UI with final step status

            # Report final step outcome (success or final error)
            await websocket.send_text(f"**Agent: Step {idx + 1} finished with status: {final_status.upper()}**")
            # Log the final result/error again for clarity if needed
            # await websocket.send_text(f"Final Result/Error for Step {idx+1}:\n```\n{step_result}\n```")

            if final_status == 'error':
                final_agent_message = f"Agent Error: Workflow failed at step {idx + 1} ('{tasks_with_status[idx]['description']}')."
                await websocket.send_text(f"**{final_agent_message}**") # Announce failure
                workflow_stopped_prematurely = True
                break # Stop workflow execution on first failed step

            # Store successful result for potential context in later steps
            last_successful_result = step_result
            await asyncio.sleep(0.2) # Pause slightly between steps

        # 4) FINALIZE WORKFLOW
        # Loop finished (either completed all steps, hit limit, or failed)
        if not workflow_stopped_prematurely: # All steps completed successfully
             final_agent_message = "Agent: Workflow completed successfully."

        await websocket.send_text(f"**{final_agent_message}**") # Send final status message

        # Optional: Generate a final summary based on successful steps
        # if not workflow_stopped_prematurely and tasks_with_status:
        #     summary_prompt = f"The following tasks were completed successfully:\n"
        #     for task_info in tasks_with_status:
        #          if task_info['status'] == 'done':
        #               summary_prompt += f"- {task_info['description']}: Result: {str(task_info.get('result','N/A'))[:150]}...\n"
        #     summary_prompt += f"\nBased on these results and the original query '{user_query}', provide a concise final answer to the user."
        #     final_answer = simple_prompt(model=planning_model_to_use, prompt=summary_prompt, system="You are summarizing the results of an agent workflow.")
        #     if final_answer:
        #          await websocket.send_text(f"Agent: Final Answer:\n{final_answer}")

    except ValueError as e: # Catch planning/parsing errors
        tb_short = traceback.format_exc(limit=1) # Short traceback
        error_msg = f"Agent Error: Failed during planning or plan parsing: {e}"
        print(f"{error_msg}\n{tb_short}")
        await websocket.send_text(error_msg)
        await send_task_update(websocket, []) # Send empty task list to clear UI
        final_agent_message = "Agent Error: Workflow failed during planning."
    except Exception as e: # Catch any other unexpected errors
        tb_full = traceback.format_exc()
        error_msg = f"Agent Error: An unexpected error occurred during the workflow: {e}"
        print(f"{error_msg}\n{tb_full}")
        await websocket.send_text(error_msg)
        # Try to mark current/pending tasks as error in UI
        updated = False
        for task_info in tasks_with_status:
            if task_info['status'] in ['running', 'pending']:
                task_info['status'] = 'error'; updated = True
        if updated: await send_task_update(websocket, tasks_with_status)
        final_agent_message = "Agent Error: Workflow failed unexpectedly."

    finally:
        # This block executes regardless of exceptions
        print(f"Agent workflow finished. Final status: {final_agent_message}")
        # Optional delay before potentially closing connection
        # await asyncio.sleep(0.5)

# --- Legacy Functions (Not used by handle_agent_workflow) ---
async def create_task_list(*args, **kwargs): await args[-1].send_text("Agent Warning: Legacy function create_task_list called (likely unused).")
async def execute_tasks(*args, **kwargs): await args[-1].send_text("Agent Warning: Legacy function execute_tasks called (likely unused).")
async def review_and_repair(*args, **kwargs): await args[-1].send_text("Agent Warning: Legacy function review_and_repair called (likely unused).")
async def final_review(*args, **kwargs): await args[-1].send_text("Agent Warning: Legacy function final_review called (likely unused).")