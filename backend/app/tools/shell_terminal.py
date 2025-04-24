# backend/app/tools/shell_terminal.py
import subprocess
import shlex
import asyncio
import traceback
import os

# Whitelist common safe commands + Python/Pip for agent flexibility
ALLOWED_COMMANDS = {
    'ls', 'pwd', 'echo', 'cat', 'grep', 'mkdir', 'rmdir', 'rm', # Added rm carefully
    'touch', 'head', 'tail', 'date', 'uname', 'df', 'free', 'env', # Added env
    'python', 'python3', 'pip', 'pip3', 'wget', 'curl' # Added download tools
}
# Blacklist patterns/characters often used maliciously
# This is a basic check, not foolproof security. Avoid running as root if possible.
ARGUMENT_BLACKLIST_PATTERNS = [';', '|', '&', '`', '$', '(', ')', '<', '>', '*', '?', '[', ']', '{', '}', '\\', '..']

TIMEOUT_SECONDS = 30 # Increased timeout

async def execute_shell_command(full_command: str, websocket) -> str:
    """
    Safely execute whitelisted shell commands using asyncio subprocess.
    Performs basic command parsing and argument sanitization.
    Returns combined stdout/stderr.
    """
    if not full_command.strip():
         await websocket.send_text("Agent Warning: Received empty shell command.")
         return "Error: No shell command provided to execute."

    await websocket.send_text(f"Shell Terminal: Preparing command: {full_command[:70]}...")
    print(f"[Shell Tool] Attempting command: {full_command}")

    # 1) Parse using shlex (handles basic quoting)
    try:
        cmd_parts = shlex.split(full_command)
    except ValueError as e:
        err_msg = f"Error: Command parsing failed: {e}. Check quoting and special characters."
        await websocket.send_text(f"Agent Error: {err_msg}")
        print(f"[Shell Tool] {err_msg}")
        return err_msg

    if not cmd_parts:
        # Should be caught by initial strip(), but double-check
        await websocket.send_text("Agent Error: Empty shell command after parsing.")
        print("[Shell Tool] Error: Empty command after parsing.")
        return "Error: Empty command after parsing."

    # 2) Validate command against whitelist
    command = cmd_parts[0]
    args = cmd_parts[1:]
    command_basename = os.path.basename(command) # Check basename (e.g., `ls` even if `/bin/ls` is used)

    if command_basename not in ALLOWED_COMMANDS:
        err_msg = f"Error: Command '{command_basename}' (from '{command}') is not in the allowed list: {ALLOWED_COMMANDS}"
        await websocket.send_text(f"Agent Error: {err_msg}")
        print(f"[Shell Tool] {err_msg}")
        return err_msg

    # 3) Basic argument sanitization (prevent common injection patterns)
    for arg in args:
        # Check for blacklisted characters/patterns within the argument
        if any(pattern in arg for pattern in ARGUMENT_BLACKLIST_PATTERNS):
             # Specific check for path traversal using '..'
             # This check might be too strict depending on use case, adjust if needed
             if '..' in arg.split(os.sep):
                 err_msg = f"Error: Argument '{arg}' contains potentially unsafe path traversal ('..')."
                 await websocket.send_text(f"Agent Error: {err_msg}")
                 print(f"[Shell Tool] {err_msg}")
                 return err_msg
             # Check for other blacklisted characters
             dangerous_chars_found = [p for p in ARGUMENT_BLACKLIST_PATTERNS if p != '..' and p in arg]
             if dangerous_chars_found:
                  err_msg = f"Error: Argument '{arg}' contains potentially unsafe characters: {', '.join(dangerous_chars_found)}"
                  await websocket.send_text(f"Agent Error: {err_msg}")
                  print(f"[Shell Tool] {err_msg}")
                  return err_msg
        # Optional: Add length checks or more sophisticated pattern matching if needed

    # 4) Execute using asyncio subprocess
    final_result_str = f"Error: Shell command '{command}' execution failed unexpectedly." # Default error
    try:
        # Use the original command path (could be absolute like /usr/bin/python)
        cmd_exec_list = [command] + args
        cmd_str_for_log = " ".join(shlex.quote(part) for part in cmd_exec_list) # Safe logging string

        await websocket.send_text(f"Shell Terminal: Running: {cmd_str_for_log}")
        print(f"[Shell Tool] Executing: {cmd_exec_list}")

        process = await asyncio.create_subprocess_exec(
            *cmd_exec_list,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
            # Consider setting cwd='/app' if commands should run relative to the app dir
        )

        try:
            # Wait for completion with timeout
            stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=TIMEOUT_SECONDS)
            exit_code = process.returncode

            # Decode output, replacing errors
            stdout = stdout_bytes.decode('utf-8', errors='replace').strip() if stdout_bytes else ""
            stderr = stderr_bytes.decode('utf-8', errors='replace').strip() if stderr_bytes else ""

            # Log outputs for debugging
            print(f"[Shell Tool] Command finished. Exit Code: {exit_code}")
            if stderr: print(f"--- [Shell Tool] STDERR ---\n{stderr}\n---")
            if stdout: print(f"--- [Shell Tool] STDOUT ---\n{stdout}\n---")

            # Compile final result string
            result_parts = []
            result_parts.append(f"Exit Code: {exit_code}")
            if stdout:
                result_parts.append(f"Output:\n{stdout}")
            if stderr:
                error_prefix = "Error:\n" if exit_code != 0 else "Stderr Log:\n"
                result_parts.append(f"{error_prefix}{stderr}")

            final_result_str = "\n".join(result_parts)

            # Send final status message
            if exit_code == 0:
                 await websocket.send_text(f"Shell Terminal: Command finished successfully (Exit: {exit_code}).")
            else:
                 await websocket.send_text(f"Shell Terminal: Command finished with errors (Exit: {exit_code}).")

        except asyncio.TimeoutError:
            print(f"[Shell Tool] Command timed out after {TIMEOUT_SECONDS}s: {cmd_str_for_log}")
            try: # Try to kill the timed-out process
                process.kill(); await process.wait()
            except ProcessLookupError: pass
            except Exception as kill_err: print(f"[Shell Tool] Error killing timed-out process: {kill_err}")
            timeout_msg = f"Error: Shell command timed out after {TIMEOUT_SECONDS}s."
            await websocket.send_text(f"Agent Error: {timeout_msg}")
            final_result_str = timeout_msg # Return timeout error

    except FileNotFoundError:
        # Error if the command executable (e.g., 'ls', '/bin/ls') is not found
        fnf_msg = f"Error: Command executable '{command}' not found in system PATH."
        await websocket.send_text(f"Agent Error: {fnf_msg}")
        print(f"[Shell Tool] {fnf_msg}")
        final_result_str = fnf_msg
    except PermissionError as e:
         # Error if file found but no execute permission
         perm_err = f"Error: Permission denied for command '{command}': {e}"
         await websocket.send_text(f"Agent Error: {perm_err}")
         print(f"[Shell Tool] {perm_err}")
         final_result_str = perm_err
    except Exception as e:
        # Catchall for other errors during execution setup/management
        exc_msg = f"Error: Unexpected error executing shell command '{full_command}': {e}"
        await websocket.send_text(f"Agent Error: {exc_msg}")
        print(f"[Shell Tool] {exc_msg}")
        traceback.print_exc()
        final_result_str = f"{exc_msg}\n{traceback.format_exc()}"

    return final_result_str.strip() # Return combined output string