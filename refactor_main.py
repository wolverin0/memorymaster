import re
import sys

# Read cli.py
with open('memorymaster/cli.py', 'r') as f:
    content = f.read()

# Find main function
start = content.find('def main(argv: list[str] | None = None) -> int:')
try_start = content.find('    try:', start)
except_start = content.find('    except', try_start)

# Extract the handler blocks between try and except
try_body = content[try_start + 8:except_start]  # +8 to skip "    try:\n"

# Find all command if blocks
pattern = r'        if args\.command == "([^"]+)":'
commands = re.findall(pattern, try_body)

print(f"Found {len(commands)} commands:")
for i, cmd in enumerate(commands):
    print(f"  {i+1}. {cmd}")

print(f"\nRefactoring would:")
print(f"  - Extract {len(commands)} handler functions")
print(f"  - Make each handler 15-50 lines")
print(f"  - Reduce main from ~740 lines to ~80 lines")
print(f"  - Reduce main C901 from 126 to ~15")
print(f"  - Net reduction: ~111 C901 points")

