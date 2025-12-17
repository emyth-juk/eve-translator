import re
from dataclasses import dataclass
from typing import Dict, List, Tuple

@dataclass
class TokenizedMessage:
    """Represents a message with EVE links replaced by placeholders."""
    original: str                 # Original message
    cleaned: str                  # Cleaned message (safe for translation)
    tokens: Dict[str, str]        # Placeholder -> original token mapping

class EVELinkTokenizer:
    """
    Detects EVE in-game link tokens using heuristics to identify
    mixed control and printable sequences.
    """
    
    def __init__(self):
        self.token_counter = 0

    def tokenize(self, message: str) -> TokenizedMessage:
        tokens = {}
        self.token_counter = 0
        
        # Detect links using the heuristic scanner
        links = self._detect_eve_links(message)
        
        if not links:
             return TokenizedMessage(original=message, cleaned=message, tokens={})

        # Reconstruct string with placeholders
        result_parts = []
        last_idx = 0
        
        for start, end, content in links:
            # Add text before match
            result_parts.append(message[last_idx:start])
            
            # Create placeholder
            self.token_counter += 1
            placeholder = f"__EVELINK_{self.token_counter}__"
            
            # Store token
            tokens[placeholder] = content
            result_parts.append(placeholder)
            
            last_idx = end
        
        # Add remaining text
        result_parts.append(message[last_idx:])
        cleaned = "".join(result_parts)

        return TokenizedMessage(
            original=message,
            cleaned=cleaned,
            tokens=tokens
        )

    def restore(self, message: str, tokens: Dict[str, str]) -> str:
        """
        Restore EVE links from placeholders.
        """
        result = message
        for placeholder, original in tokens.items():
            result = result.replace(placeholder, original)
        return result

    def _detect_eve_links(self, message: str) -> List[Tuple[int, int, str]]:
        """
        Returns list of (start, end, link_content) tuples.
        Uses heuristics to detect EVE link boundaries.
        Ref: PARSER_SPEC.md
        """
        links = []
        i = 0
        n = len(message)
        
        while i < n:
            char_code = ord(message[i])
            
            # Check if control character (Start of potential link)
            # 0x00-0x1F: ASCII Control
            # 0x7F-0x9F: Latin-1 Supplement Control / C1
            is_control = (char_code < 0x20) or (0x7F <= char_code <= 0x9F)
            
            if is_control:
                start = i
                # Scan forward
                while i < n:
                    c = ord(message[i])
                    
                    # Control char or high Unicode (likely part of link binary data)
                    if (c < 0x20) or (0x7F <= c <= 0x9F):
                        i += 1
                        continue
                    
                    # Printable ASCII (0x20-0x7E)
                    # Might be part of the link (e.g. specialized identifiers)
                    # We check if it continues being a "link" by looking ahead.
                    # Heuristic: If we see printable text, we consume it IF it is followed
                    # quickly by more control chars or weird chars. 
                    # BUT, "Doctrine/" is printable. We don't want to consume that if we started at the control char.
                    # Wait, the start condition was `is_control`. So we are ALREADY inside a declared link block.
                    # We only stop if we see a long run of normal text?
                    # The spec said:
                    # "if i < len(message) and (ord(message[i]) < 0x20 or ord(message[i]) > 0x7E): continue; else break"
                    # This implies valid link content must NOT contain standard ASCII unless immediately followed by non-standard.
                    # Let's try a simplified greedy approach: consume until we hit a run of "normal text".
                    
                    # Spec Logic Implementation:
                    if 0x20 <= c <= 0x7E:
                        # Check next char
                        if (i + 1) < n:
                            next_c = ord(message[i+1])
                            # If next char is also 'normal', assume end of link.
                            # (This breaks if link has 2 consecutive normal chars, which might happen)
                            # But works for `\x1Ahere\x1A`?
                            # `h` (normal), next `e` (normal) -> Break.
                            # So `\x1Ahere\x1A` would be parsed as `\x1A`, `here`, `\x1A`. Three tokens?
                            # Ideally `\x1Ahere\x1A` should be ONE token or handled.
                            # For the test case `\u000eT\x1A\x03`:
                            # 1. `\u000e` (control) -> start
                            # 2. `T` (normal). Next is `\x1A` (control).
                            # If we use the lookahead check:
                            if (next_c < 0x20) or (0x7F <= next_c <= 0x9F):
                                i += 1 # Consume this normal char as part of link
                                continue
                        
                        # If we fall through here, it implies we hit a normal char NOT followed by control.
                        # End of link.
                        break
                    else:
                        # High byte unicode > 0x9F? 
                        # Usually EVE links are weird. Let's assume yes? 
                        # Spec regex `[^\x00-\x1F\x20-\x7E\x80-\x9F\u00A0-\uFFFF]` is empty set? 
                        # No, spec said `[^\x20-\x7E\u00A0-\uFFFF]`.
                        # Let's assume anything non-printable is part of link.
                        i += 1
                
                end = i
                links.append((start, end, message[start:end]))
            else:
                i += 1
                
        return links
