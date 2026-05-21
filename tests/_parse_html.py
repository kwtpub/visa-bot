"""Parse the saved HTML to find form element selectors."""
import re
import sys

html = open("screenshots/step5_form_found_source.html", "r", encoding="utf-8").read()

print(f"HTML length: {len(html)}")
print()

# Find formcontrolname attrs
print("=== formcontrolname attributes ===")
for m in re.finditer(r'formcontrolname="([^"]+)"', html, re.I):
    start = max(0, m.start()-150)
    end = min(len(html), m.end()+50)
    context = html[start:end]
    print(f"  {m.group(1)}: ...{context}...")
    print()

# Find submit buttons  
print("\n=== type=submit ===")
for m in re.finditer(r'type="submit"', html, re.I):
    start = max(0, m.start()-200)
    end = min(len(html), m.end()+200)
    print(f"  ...{html[start:end]}...")
    print()

# Find Sign In / Войти buttons
print("\n=== Sign In / Войти text ===")
for pattern in [r"Sign [Ii]n", r"Войти", r"Вход"]:
    for m in re.finditer(pattern, html):
        start = max(0, m.start()-200)
        end = min(len(html), m.end()+100)
        print(f"  [{m.group()}] ...{html[start:end]}...")
        print()

# Find cookie consent
print("\n=== Cookie consent ===")
for pattern in [r"onetrust", r"Accept All", r"Accept Only"]:
    count = len(re.findall(pattern, html, re.I))
    print(f"  '{pattern}' found {count} times")

# Find turnstile sitekey
print("\n=== Turnstile ===")
for m in re.finditer(r'data-sitekey="([^"]+)"', html):
    print(f"  sitekey: {m.group(1)}")
for m in re.finditer(r'challenges\.cloudflare\.com[^"]*', html):
    print(f"  cf URL: {m.group()[:120]}")
