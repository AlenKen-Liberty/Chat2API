#!/usr/bin/env python3
"""
Helper script to capture Perplexity cookies using browser automation.
This is optional - you can also manually copy cookies from your browser.
"""

import json
import os
from pathlib import Path

try:
    from patchright.sync_api import sync_playwright
except ImportError:
    print("❌ Patchright not installed. Install with: pip install patchright")
    print("   Or manually copy cookies from your browser (see README.md)")
    exit(1)

COOKIES_FILE = Path(__file__).parent / "cookies.json"

def save_cookies():
    """Capture and save Perplexity cookies"""
    print("🔐 Perplexity Cookie Capture Tool\n")
    print("This will open a browser. Please:")
    print("1. Log in to Perplexity.ai")
    print("2. Complete any verification if needed")
    print("3. Press Enter here when done\n")
    
    # Setup display for VNC if needed
    display = os.getenv('DISPLAY', ':0')
    os.environ['DISPLAY'] = display
    
    with sync_playwright() as p:
        # Launch browser
        browser = p.chromium.launch(
            headless=False,
            args=['--no-sandbox', '--disable-blink-features=AutomationControlled']
        )
        
        context = browser.new_context()
        page = context.new_page()
        
        # Navigate to Perplexity
        print("🌐 Opening Perplexity.ai...")
        page.goto("https://www.perplexity.ai")
        
        # Wait for user to login
        input("✅ Press Enter after you've logged in...")
        
        # Get cookies
        cookies = context.cookies()
        
        # Save to file
        with open(COOKIES_FILE, 'w') as f:
            json.dump(cookies, f, indent=2)
        
        print(f"\n✅ Cookies saved to: {COOKIES_FILE}")
        print(f"📊 Saved {len(cookies)} cookies")
        
        # Check expiration
        for cookie in cookies:
            if 'expires' in cookie and cookie['expires'] > 0:
                from datetime import datetime
                expiry = datetime.fromtimestamp(cookie['expires'])
                print(f"🔒 Cookie '{cookie['name']}' expires: {expiry}")
        
        browser.close()
    
    print("\n🎉 Setup complete! You can now run: python3 -m chat2api.server")

if __name__ == "__main__":
    save_cookies()
