"""Extract button context from saved HTML."""
import re

html = open("screenshots/step5_form_found_source.html", "r", encoding="utf-8").read()

# Find all button elements
print("=== All buttons ===")
for m in re.finditer(r'<button[^>]*>', html, re.I):
    start = m.start()
    # find end of button
    end_tag = html.find('</button>', start)
    if end_tag == -1:
        end_tag = start + 500
    else:
        end_tag += len('</button>')
    content = html[start:end_tag]
    # skip very long style/script content
    if len(content) > 2000:
        content = content[:500] + "..." + content[-200:]
    print(f"\n--- Button ---")
    print(content[:800])
    print()

# Find the sign-in/login specific button - look for class btn-block or similar
print("\n=== Looking for login button specifically ===")
for pattern in [r'btn-block', r'mat-raised-button', r'btn-primary', r'loginForm']:
    for m in re.finditer(pattern, html, re.I):
        start = max(0, m.start()-300)
        end = min(len(html), m.end()+300)
        print(f"\n[{pattern}] found:")
        print(html[start:end])
        print()
