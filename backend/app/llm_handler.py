"""
llm_handler.py
──────────────
Centralised helpers for talking to a local Ollama server.

✓ Lists local models via the Ollama HTTP API (no CLI required)
✓ Falls back to `ollama list --json` if the REST endpoint is unreachable
✓ Exposes simplified wrappers for chat/prompting
"""
from __future__ import annotations

import http.client, json, os, ssl, subprocess, traceback, urllib.parse, shutil
from typing import Dict, List, Optional

# Use the official ollama client library for core operations
import ollama
from dotenv import load_dotenv

# ─── env / defaults ──────────────────────────────────────────────
# Load .env from backend directory (one level up from app/)
dotenv_path = os.path.join(os.path.dirname(__file__), "..", ".env")
load_dotenv(dotenv_path=dotenv_path, override=True)
print(f"Attempting to load .env from: {dotenv_path}")

# Get Ollama endpoint URL from environment variable or use default
OLLAMA_ENDPOINT = os.getenv("OLLAMA_ENDPOINT", "http://host.docker.internal:11434") # Docker host default
print(f"Using Ollama Endpoint: {OLLAMA_ENDPOINT}")

# Get default model names from environment or use fallbacks
PLANNING_TOOLING_MODEL = os.getenv("PLANNING_TOOLING_MODEL", "llama3:latest")
print(f"Default Planning/Tooling Model: {PLANNING_TOOLING_MODEL}")
# DEEPCODER_MODEL is set via env var passed to the tool directly if needed

# Initialize Ollama client (singleton-like)
try:
    _client = ollama.Client(host=OLLAMA_ENDPOINT)
    print("Ollama client initialized.")
except Exception as e:
    print(f"CRITICAL ERROR: Failed to initialize Ollama client: {e}")
    _client = None # Set client to None if initialization fails

# ──────────────────────────────────────────────────────────────────
# Helper for direct HTTP requests (used for model listing fallback)
# -----------------------------------------------------------------
def _http_json(method: str, path: str, body: Optional[Dict] = None) -> Dict:
    """Minimal HTTP request helper, avoiding ollama client complexities for specific endpoints."""
    url = urllib.parse.urlparse(OLLAMA_ENDPOINT)
    port = url.port or (443 if url.scheme == "https" else 80)
    hostname = url.hostname

    if not hostname:
         raise ValueError(f"Invalid Ollama endpoint URL (missing hostname): {OLLAMA_ENDPOINT}")

    conn = None
    try:
        if url.scheme == "https":
            # Create HTTPS connection, ignoring self-signed cert errors if necessary
            context = ssl._create_unverified_context() # Adjust context if needed
            conn = http.client.HTTPSConnection(hostname, port, context=context, timeout=10)
        else:
            conn = http.client.HTTPConnection(hostname, port, timeout=10)

        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        body_json = json.dumps(body or {}) if body else None

        conn.request(method, path, body=body_json, headers=headers)
        response = conn.getresponse()
        response_data = response.read().decode('utf-8')

        if response.status >= 400:
             raise http.client.HTTPException(f"HTTP Error {response.status} {response.reason} for {method} {path}: {response_data}")

        return json.loads(response_data or "{}") # Return empty dict if response is empty

    except Exception as e:
         print(f"HTTP request to {method} {path} failed: {e}")
         raise # Re-raise the exception
    finally:
        if conn:
            conn.close()

# ─── Discover local models (Primary Function) ───────────────────
def list_local_models() -> List[str]:
    """
    Fetches locally available Ollama models. Prefers Ollama client,
    falls back to direct HTTP API, then to CLI.
    Returns a sorted list of unique model names (e.g., ["llama3:latest", "qwen2:7b"]).
    """
    models = set()

    # 1. Try using the official Ollama client's list method
    if _client:
        try:
            response = _client.list()
            models.update(m.get('name') for m in response.get('models', []) if m.get('name'))
            if models:
                print(f"Models found via Ollama Client: {len(models)}")
                return sorted(list(models))
        except Exception as e:
            print(f"Warning: Ollama client list() failed: {e}. Trying direct HTTP API.")
            # Fall through to next method

    # 2. Try direct HTTP API call to /api/tags (if client failed or wasn't initialized)
    try:
        response = _http_json("GET", "/api/tags")
        # The actual key might be 'models', containing dicts with 'name' or 'model'
        models_list = response.get("models", [])
        models.update(m.get('model') or m.get('name') for m in models_list if m.get('model') or m.get('name'))
        if models:
            print(f"Models found via HTTP API (/api/tags): {len(models)}")
            return sorted(list(models))
    except Exception as e:
        print(f"Warning: Ollama HTTP API (/api/tags) failed: {e}. Falling back to CLI.")
        # Fall through to next method

    # 3. Fallback: Use `ollama list` command line
    try:
        ollama_cli_path = shutil.which("ollama") # Find ollama executable in PATH
        if not ollama_cli_path:
            raise FileNotFoundError("ollama CLI command not found in system PATH.")

        # Run `ollama list` and capture output
        # Use --json flag if available, otherwise parse text output
        try: # Try with --json first
             # Use subprocess.run for simplicity here
             result = subprocess.run(
                 [ollama_cli_path, "list", "--json"],
                 capture_output=True, text=True, check=True, timeout=15
             )
             # Ollama's --json output is one JSON object per line
             for line in result.stdout.strip().splitlines():
                 if line:
                     model_data = json.loads(line)
                     if model_data.get('name'): models.add(model_data['name'])

        except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError):
             print("Warning: `ollama list --json` failed or not supported. Trying plain `ollama list`.")
             # Fallback to parsing plain text output
             result = subprocess.run(
                 [ollama_cli_path, "list"],
                 capture_output=True, text=True, check=True, timeout=15
             )
             # Example text line: llama3:latest  8b  7.4 GB  1 minute ago
             for line in result.stdout.strip().splitlines():
                 parts = line.split()
                 if len(parts) > 0 and ':' in parts[0]: # Basic check for model:tag format
                     models.add(parts[0])

        if models:
            print(f"Models found via Ollama CLI: {len(models)}")
            return sorted(list(models))

    except FileNotFoundError:
        print("Error: 'ollama' command line tool not found in PATH. Cannot list models via CLI.")
    except subprocess.CalledProcessError as e:
        print(f"Error: `ollama list` command failed (Exit Code {e.returncode}): {e.stderr}")
    except Exception as e:
        print(f"Error: Failed to list models using Ollama CLI: {e}")

    # If all methods fail
    print("Error: Could not retrieve models using any method.")
    return [] # Return empty list on complete failure

# ─── Simplified Wrappers for Backend Use ────────────────────────
def simple_prompt(model: str, prompt: str, system: Optional[str] = None) -> Optional[str]:
    """
    Sends a simple user prompt (optionally with a system message) to the specified model.
    Ensures the model is pulled locally if not present. Returns the response content or None.
    """
    if not _client:
        print("Error: Ollama client not initialized. Cannot send prompt.")
        return None
    try:
        # Check if model exists locally, pull if not (optional, client might handle this)
        # _ensure_model_pulled(model) # You could add this helper if needed

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        print(f"Sending prompt to '{model}'...")
        response = _client.chat(model=model, messages=messages)
        content = response.get("message", {}).get("content")
        print(f"Received response from '{model}'. Length: {len(content) if content else 0}")
        return content
    except Exception as e:
        print(f"Error during Ollama chat with model '{model}': {e}")
        traceback.print_exc()
        return None

# --- Optional: Helper to explicitly pull model ---
# def _ensure_model_pulled(model: str):
#     """Checks if model exists and pulls it if not."""
#     if not _client: return
#     try:
#         _client.show(model) # Check if model exists locally
#         # print(f"Model '{model}' found locally.")
#     except ollama.ResponseError as e:
#         if e.status_code == 404: # Model not found locally
#             print(f"Model '{model}' not found locally. Pulling...")
#             try:
#                 _client.pull(model)
#                 print(f"Model '{model}' pulled successfully.")
#             except Exception as pull_err:
#                 print(f"Error pulling model '{model}': {pull_err}")
#         else: # Other API error
#             print(f"Error checking model '{model}': {e}")
#     except Exception as e:
#          print(f"Unexpected error checking model '{model}': {e}")

# --- Ensure default planning model is available at startup ---
# print(f"Ensuring default planning model '{PLANNING_TOOLING_MODEL}' is available...")
# _ensure_model_pulled(PLANNING_TOOLING_MODEL)
# print("LLM Handler ready.")

# Back-compat aliases if needed by older agent code, though direct use is preferred
# chat = _client.chat # Direct alias might be too simple if error handling/logging is needed
send_prompt = simple_prompt
send_prompt_with_functions = simple_prompt # Assuming same logic for now