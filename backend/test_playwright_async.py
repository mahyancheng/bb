# backend/test_playwright_async.py
import asyncio
import sys
import traceback

# Apply the policy change directly in the test script
if sys.platform == "win32":
    try:
        # Try setting the policy
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        print("Successfully set asyncio policy to WindowsSelectorEventLoopPolicy")
    except Exception as policy_error:
        print(f"Error setting asyncio policy: {policy_error}")

# Try importing playwright *after* potentially setting the policy
try:
    from playwright.async_api import async_playwright
    print("Successfully imported playwright.async_api")
except ImportError as import_err:
    print(f"Failed to import playwright: {import_err}")
    print("Make sure playwright is installed: pip install playwright")
    sys.exit(1)
except Exception as other_import_err:
     print(f"Unexpected error importing playwright: {other_import_err}")
     traceback.print_exc()
     sys.exit(1)


async def main():
    print("\nStarting minimal async Playwright test...")
    pw = None
    browser = None
    try:
        print("Getting async_playwright context...")
        async with async_playwright() as pw:
            print("Launching browser (Chromium headless)...")
            browser = await pw.chromium.launch(headless=True)
            print("Browser launched successfully.")
            print("Creating new page...")
            page = await browser.new_page()
            print("Navigating to example.com...")
            await page.goto("https://example.com")
            page_title = await page.title()
            print(f"Page title: {page_title}")
            print("Closing browser...")
            await browser.close()
            print("Browser closed successfully.")
            print("\nMinimal Playwright test PASSED!")
    except NotImplementedError as nie:
            print("\n--- TEST FAILED ---")
            print("Caught NotImplementedError during Playwright execution!")
            print("This indicates the asyncio event loop policy fix is likely not working correctly for Playwright on this system.")
            traceback.print_exc()
    except Exception as e:
        print("\n--- TEST FAILED ---")
        print(f"An unexpected error occurred during Playwright execution: {e}")
        traceback.print_exc()
    finally:
        # Ensure browser is closed even if errors occurred before await browser.close()
        if browser and not browser.is_connected():
            print("Attempting final browser close...")
            try:
                await browser.close()
            except Exception as close_err:
                 print(f"Error during final browser close: {close_err}")


if __name__ == "__main__":
     # Check the policy *after* attempting to set it
     print(f"Using asyncio policy: {type(asyncio.get_event_loop_policy()).__name__}")
     # asyncio.run should handle loop creation/closing correctly
     asyncio.run(main())