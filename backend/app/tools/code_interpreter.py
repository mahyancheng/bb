# backend/app/tools/code_interpreter.py
import subprocess
import tempfile
import os
import asyncio
import traceback
import sys
import re
import shlex # For safe command formatting/logging

TIMEOUT_SECONDS = 60 # Increased timeout for potential installs

async def execute_python_code(code: str, websocket) -> str:
    """
    Executes Python code in a subprocess using asyncio.
    On ModuleNotFoundError, auto-installs the missing package via pip and retries once.
    Sends informative messages via websocket. Returns combined stdout/stderr.
    """
    if not code.strip():
         await websocket.send_text("Agent Warning: Received empty code snippet for execution.")
         return "Error: No Python code provided to execute."

    await websocket.send_text("Code Interpreter: Preparing to run Python code...")
    script_path = None # Initialize script_path

    # Use a context manager for the temporary file creation
    try:
        # Create temp file in a known directory if possible (e.g., /tmp inside container)
        # This avoids potential permission issues in /app
        temp_dir = os.environ.get("TEMP", "/tmp") # Use TEMP env var or default to /tmp
        os.makedirs(temp_dir, exist_ok=True) # Ensure temp dir exists

        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8', dir=temp_dir) as tmp:
            script_path = tmp.name
            tmp.write(code)
        print(f"[Code Interpreter] Code written to temporary file: {script_path}")
    except Exception as file_err:
         error_msg = f"Error: Failed to create temporary file for code execution: {file_err}"
         await websocket.send_text(f"Agent Error: {error_msg}")
         print(f"[Code Interpreter] {error_msg}")
         return error_msg # Return error if file creation fails

    python_executable = sys.executable # Use the same python executing the backend

    async def run_script_attempt(attempt_num):
        """Helper coroutine to run the script and capture output."""
        await websocket.send_text(f"Code Interpreter: Executing script (Attempt {attempt_num})...")
        cmd_str_log = f"{shlex.quote(python_executable)} {shlex.quote(script_path)}"
        print(f"[Code Interpreter] Running command: {cmd_str_log}")

        process = await asyncio.create_subprocess_exec(
            python_executable, script_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
            # Consider setting cwd if the script depends on relative paths
            # cwd='/app' # Or some other directory
        )

        try:
            # Communicate with the process and wait for completion with timeout
            stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=TIMEOUT_SECONDS)
            exit_code = process.returncode

            # Decode output, replacing errors
            stdout = stdout_bytes.decode('utf-8', errors='replace').strip() if stdout_bytes else ""
            stderr = stderr_bytes.decode('utf-8', errors='replace').strip() if stderr_bytes else ""

            # Log outputs for debugging
            print(f"[Code Interpreter] Attempt {attempt_num} finished. Exit Code: {exit_code}")
            if stderr: print(f"--- [Code Interpreter] Attempt {attempt_num} STDERR ---\n{stderr}\n---")
            if stdout: print(f"--- [Code Interpreter] Attempt {attempt_num} STDOUT ---\n{stdout}\n---")

            return exit_code, stdout, stderr # Return decoded strings

        except asyncio.TimeoutError:
            print(f"[Code Interpreter] Attempt {attempt_num} timed out after {TIMEOUT_SECONDS}s.")
            try: # Try to kill the timed-out process
                process.kill()
                await process.wait()
            except ProcessLookupError: pass # Process already finished
            except Exception as kill_err: print(f"[Code Interpreter] Error killing timed-out process: {kill_err}")
            # Return specific timeout error message in stderr field
            return -1, "", f"Error: Python execution timed out after {TIMEOUT_SECONDS}s."
        except Exception as exec_err:
             # Catch other unexpected errors during execution
             print(f"[Code Interpreter] Unexpected error during script execution attempt {attempt_num}: {exec_err}")
             traceback.print_exc()
             # Return error message in stderr field
             return -1, "", f"Error: Unexpected error during script execution: {exec_err}"

    # --- Main Execution Logic ---
    final_result_str = "Error: Code execution failed unexpectedly." # Default error
    try:
        # First attempt
        exit_code, stdout, stderr = await run_script_attempt(1)

        # Auto-install and retry logic for ModuleNotFoundError
        if exit_code != 0 and stderr and "ModuleNotFoundError: No module named" in stderr:
            missing_match = re.search(r"No module named ['\"](.+?)['\"]", stderr)
            if missing_match:
                package_name = missing_match.group(1)
                # Sanitize package name slightly (basic check)
                package_name = re.sub(r"[^a-zA-Z0-9_\-.]", "", package_name)
                if not package_name:
                     await websocket.send_text("Code Interpreter: Could not parse package name for auto-install.")
                else:
                    install_msg = f"Code Interpreter: Detected missing module '{package_name}'. Attempting 'pip install {package_name}'..."
                    await websocket.send_text(install_msg)
                    print(f"[Code Interpreter] {install_msg}")

                    # Run pip install command
                    pip_cmd = [python_executable, '-m', 'pip', 'install', package_name]
                    print(f"[Code Interpreter] Running install command: {' '.join(shlex.quote(p) for p in pip_cmd)}")
                    install_proc = await asyncio.create_subprocess_exec(
                        *pip_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    # Wait for pip to finish (add a reasonable timeout?)
                    pip_stdout_bytes, pip_stderr_bytes = await asyncio.wait_for(install_proc.communicate(), timeout=120.0) # 2 min timeout for install
                    pip_exit_code = install_proc.returncode

                    pip_stderr = pip_stderr_bytes.decode('utf-8', errors='replace').strip()
                    if pip_stderr: print(f"--- [Code Interpreter] Pip Install STDERR ---\n{pip_stderr}\n---")

                    if pip_exit_code == 0:
                        install_success_msg = f"Code Interpreter: Successfully installed '{package_name}'. Retrying script..."
                        await websocket.send_text(install_success_msg)
                        print(f"[Code Interpreter] {install_success_msg}")
                        # Second attempt after install
                        exit_code, stdout, stderr = await run_script_attempt(2)
                    else:
                        install_fail_msg = f"Error: Failed to install package '{package_name}' (Exit Code: {pip_exit_code})."
                        await websocket.send_text(f"Agent Error: {install_fail_msg}")
                        print(f"[Code Interpreter] {install_fail_msg}")
                        # Append pip's stderr to the original script stderr for context
                        stderr += f"\n\n--- Auto-install failed ---\n{install_fail_msg}\n{pip_stderr}\n---"
            else:
                await websocket.send_text("Code Interpreter: ModuleNotFoundError detected, but could not parse package name for auto-install.")

        # Compile final result string, combining stdout and stderr
        result_parts = []
        result_parts.append(f"Exit Code: {exit_code}")
        if stdout:
            result_parts.append(f"Output:\n{stdout}")
        if stderr:
            # Prepend "Error:" prefix if exit code indicates failure
            error_prefix = "Error:\n" if exit_code != 0 else "Stderr Log:\n"
            result_parts.append(f"{error_prefix}{stderr}")

        final_result_str = "\n".join(result_parts)

        # Send final status message
        if exit_code == 0:
             await websocket.send_text("Code Interpreter: Script executed successfully.")
        else:
             await websocket.send_text(f"Code Interpreter: Script finished with errors (Exit Code: {exit_code}).")

    except FileNotFoundError:
        # This error means the python_executable itself wasn't found
        fnf_msg = f"Error: Python interpreter not found at '{python_executable}'."
        await websocket.send_text(f"Agent Error: {fnf_msg}")
        print(f"[Code Interpreter] {fnf_msg}")
        final_result_str = fnf_msg
    except Exception as e:
        # Catchall for errors outside subprocess execution (e.g., during setup, file IO)
        exc_msg = f"Error: Unexpected error in code interpreter wrapper: {e}"
        await websocket.send_text(f"Agent Error: {exc_msg}")
        print(f"[Code Interpreter] {exc_msg}")
        traceback.print_exc()
        final_result_str = f"{exc_msg}\n{traceback.format_exc()}"
    finally:
        # Cleanup the temporary file
        if script_path and os.path.exists(script_path):
            try:
                os.remove(script_path)
                print(f"[Code Interpreter] Cleaned up temporary file: {script_path}")
            except OSError as e:
                print(f"[Code Interpreter] Warning: Could not remove temporary file {script_path}: {e}")

    return final_result_str # Return the combined output string