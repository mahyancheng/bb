# backend/test_browser_import.py
import sys
import os
import traceback

print("-" * 60)
print(f"Running with Python executable: {sys.executable}")
print("-" * 60)
print("Python sys.path:")
for path_item in sys.path:
    print(f"  - {path_item}")
print("-" * 60)

# Check if browser-use is installed according to pkg_resources (alternative check)
try:
    import pkg_resources
    dist = pkg_resources.get_distribution("browser-use")
    print(f"Found 'browser-use' via pkg_resources: Version {dist.version} at {dist.location}")
except Exception as e:
    print(f"Could not find 'browser-use' via pkg_resources: {e}")
print("-" * 60)


# Test basic import
print("Attempting: import browser_use")
try:
    import browser_use
    print("SUCCESS: Basic 'import browser_use' worked.")
    # Print the path where the imported module was found
    if hasattr(browser_use, '__file__'):
        print(f"  -> Imported from: {browser_use.__file__}")
    else:
         print("  -> Imported module has no __file__ attribute (might be namespace package or built-in).")

except ImportError as e:
    print(f"FAILED: Basic 'import browser_use' failed.")
    print(f"  Error: {e}")
    traceback.print_exc()
    sys.exit(1) # Exit if basic import fails
except Exception as e:
     print(f"FAILED: Basic 'import browser_use' failed with unexpected error.")
     print(f"  Error: {e}")
     traceback.print_exc()
     sys.exit(1)
print("-" * 60)


# Test importing specific classes
print("Attempting: from browser_use import Agent as BrowserUseAgent, BrowserSession")
try:
    # Add environment variable for telemetry if needed, mimicking app behavior
    # os.environ["BROWSER_USE_TELEMETRY_ENABLED"] = "false" # Example: disable telemetry for test
    from browser_use import Agent as BrowserUseAgent, BrowserSession
    print("SUCCESS: 'from browser_use import Agent, BrowserSession' worked.")
    print(f"  -> BrowserUseAgent type: {type(BrowserUseAgent)}")
    print(f"  -> BrowserSession type: {type(BrowserSession)}")

except ImportError as e:
    print(f"FAILED: 'from browser_use import Agent, BrowserSession' failed.")
    print(f"  Error: {e}")
    traceback.print_exc()
    sys.exit(1) # Exit if class import fails
except Exception as e:
     print(f"FAILED: 'from browser_use import Agent, BrowserSession' failed with unexpected error.")
     print(f"  Error: {e}")
     traceback.print_exc()
     sys.exit(1)
print("-" * 60)


# Test Agent initialization (often triggers internal imports)
print("Attempting: Initialize BrowserUseAgent (this might trigger more imports/errors)")
try:
    # Minimal initialization - may need LLM provider details depending on library version
    # Let's assume default (might try Ollama if configured)
    agent = BrowserUseAgent()
    print("SUCCESS: Initializing BrowserUseAgent worked.")
    print(f"  -> Agent object created: {agent}")

except ImportError as e:
    # Catch potential ImportErrors triggered during initialization
    print(f"FAILED: Initializing BrowserUseAgent failed with ImportError.")
    print(f"  Error: {e}")
    traceback.print_exc()
except Exception as e:
    print(f"FAILED: Initializing BrowserUseAgent failed with unexpected error.")
    print(f"  Error: {e}")
    traceback.print_exc()
print("-" * 60)

print("Test script finished.")