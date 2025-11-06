from dotenv import load_dotenv; load_dotenv()
from playwright.sync_api import sync_playwright
from login_helper import login_guardian

def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, slow_mo=200)
        page = browser.new_page()
        login_guardian(page)
        print("Logged in, current URL:", page.url)
        input("Press Enter to close…")
        browser.close()

if __name__ == "__main__":
    main()