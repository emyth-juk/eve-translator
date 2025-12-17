import sys
import os
from pathlib import Path

# Add project root to path to allow imports
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../')))

from src.core.parser import LineParser
from src.core.tokenizer import EVELinkTokenizer
from src.core.detector import LanguageDetector
from src.services.translator import TranslationService, MockTranslator, GoogleTransProvider

def main():
    # File in the root of the workspace
    log_path = Path("c:/Users/alex/Documents/Code/eve-fleet-chat-translator/Fleet_20251216_082457_1117005149.txt")
    
    if not log_path.exists():
        print(f"Error: File not found at {log_path}")
        return

    print(f"Processing log file: {log_path}")
    print("-" * 50)

    parser = LineParser()
    tokenizer = EVELinkTokenizer()
    detector = LanguageDetector()
    translator = TranslationService(provider=GoogleTransProvider())

    try:
        with open(log_path, 'r', encoding='utf-16-le', errors='replace') as f:
            lines = f.readlines()
    except Exception as e:
        print(f"Error reading file: {e}")
        return

    print(f"Read {len(lines)} lines.")
    
    processed_count = 0
    translated_count = 0

    for i, line in enumerate(lines):
        # Debug: Print first non-header line repr
        if "[" in line and processed_count == 0:
             print(f"DEBUG Line {i}: {repr(line)}")

        msg = parser.parse(line, i)
        if not msg:
            continue

        processed_count += 1
        
        # Tokenize
        tokenized = tokenizer.tokenize(msg.message)
        
        # Detect
        # Force translate for CJK or if it looks 'foreign' enough
        # Using same logic as main.py
        if not detector.should_translate(tokenized.cleaned, ignored_langs={'en', 'de'}):
            continue

        translated_count += 1
        print(f"\n[Original] {msg.sender}: {tokenized.original}")
        
        # Translate
        # Mocking or Real? User said "test against live file", implies they want to see Real translation.
        # But for speed in this script, Google might be slow for 500 lines.
        # Let's translate a few or print what WOULD be translated.
        # But user wants to SEE the result.
        # Let's limit to first 5 translations to avoid rate limits/time.
        
        if translated_count > 100:
            print("... (limit reached for demo) ...")
            continue

        translated = translator.translate_message(tokenized.cleaned)
        restored = tokenizer.restore(translated, tokenized.tokens)
        
        print(f"[Translated] {restored}")
        
    print("-" * 50)
    print(f"Total Lines: {len(lines)}")
    print(f"Parsed Messages: {processed_count}")
    print(f"Candidates for Translation: {translated_count}")

if __name__ == "__main__":
    main()
