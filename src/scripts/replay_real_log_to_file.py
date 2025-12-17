import os
import time
import sys
from pathlib import Path

# Source File (The real log provided by user)
SOURCE_FILE = Path("c:/Users/alex/Documents/Code/eve-fleet-chat-translator/Fleet_20251216_082457_1117005149.txt")

# Target Directory (where App watches)
TARGET_DIR = os.path.expanduser("~/Documents/EVE/logs/Chatlogs")
TARGET_FILENAME = f"Fleet_DEMO_REAL_{int(time.time())}.txt"
TARGET_FILE = os.path.join(TARGET_DIR, TARGET_FILENAME)

def main():
    if not SOURCE_FILE.exists():
        print(f"Error: Source file not found at {SOURCE_FILE}")
        return

    print(f"Reading from: {SOURCE_FILE}")
    print(f"Replaying to: {TARGET_FILE}")
    print("The overlay app should detect this new file and show messages.")
    
    # Read all lines first
    with open(SOURCE_FILE, 'r', encoding='utf-16-le', errors='replace') as f:
        content = f.read()

    # Split lines ?
    # The source file might have specific line endings.
    # Note: EVE logs might have \ufeff at start.
    # We should just read line by line.
    
    with open(SOURCE_FILE, 'r', encoding='utf-16-le', errors='replace') as f:
        lines = f.readlines()

    # Open target
    with open(TARGET_FILE, 'w', encoding='utf-16-le') as out_f:
        out_f.write('\ufeff') # Write BOM
        
        print("Replay starting in 5 seconds...")
        time.sleep(5)
        
        count = 0
        for line in lines:
            # Skip if empty or just newline
            if not line.strip():
                continue
                
            out_f.write(line)
            out_f.flush()
            
            # Print progress
            count += 1
            if count % 10 == 0:
                print(f"Replayed {count} lines...")
            else:
                 sys.stdout.write(".")
                 sys.stdout.flush()

            # Delay to simulate chat speed
            # Use small delay to make it readable in overlay (e.g. 0.2s - 1s)
            # If line is header, maybe fast?
            # Header typically has "Channel ID", etc.
            if "Channel ID" in line or "listener" in line.lower() or "--------" in line:
                time.sleep(0.01)
            else:
                time.sleep(0.5) 

    print("\nReplay complete.")
    time.sleep(10) # Keep window open

if __name__ == "__main__":
    main()
