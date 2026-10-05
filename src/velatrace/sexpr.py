"""Bounded, non-evaluating Specctra S-expression reader."""
from .errors import ValidationError


class QuotedAtom(str):
    """Preserve lexical quoting for safe DSN reserialization."""


class JoinedAtom(str):
    """One Specctra token written as a quoted part plus an unquoted tail.

    KiCad writes pins of hyphenated references as "Pi-1"-3: one pin reference
    (component Pi-1, pin 3). The value is the joined text; raw is the exact
    original spelling, which must be written back unchanged."""
    raw: str


# KiCad's own files escape inside quotes; Specctra DSN/SES never does (KiCad's
# specctraMode lexer and Freerouting both read a backslash literally, and Windows
# DSN exports embed the full backslashed path). \x/octal are never written by KiCad.
KICAD_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "a": "\a", "b": "\b", "f": "\f", "v": "\v"}


def parse(text: str, *, kicad: bool = False) -> list:
    if not isinstance(text, str) or len(text) > 32_000_000:
        raise ValidationError("Specctra input is missing or exceeds 32 MB.")
    stack, roots, i, count = [], [], 0, 0
    while i < len(text):
        char = text[i]
        if char.isspace():
            i += 1
            continue
        if char == '(':
            node = []
            (stack[-1] if stack else roots).append(node)
            stack.append(node)
            if len(stack) > 80:
                raise ValidationError("Specctra nesting is too deep.")
            i += 1
        elif char == ')':
            if not stack:
                raise ValidationError("Unmatched Specctra closing parenthesis.")
            stack.pop()
            i += 1
        else:
            if not stack:
                raise ValidationError("Specctra text outside its root expression.")
            if char == '"':
                # Standard Specctra parser declares a literal quote without a closing quote.
                if not kicad and stack[-1] == ['string_quote'] and text[i+1:i+2] == ')':
                    stack[-1].append('"')
                    i += 1
                    continue
                i += 1
                # Slices between escapes, not one character at a time: a single
                # 31 MB quoted token used to take 40 s per parse.
                parts, end = [], text.find('"', i)
                while True:
                    if end < 0:
                        raise ValidationError("Unterminated Specctra string.")
                    escape = text.find('\\', i, end) if kicad else -1
                    if escape < 0:
                        parts.append(text[i:end])
                        break
                    parts.append(text[i:escape])
                    parts.append(KICAD_ESCAPES.get(text[escape + 1], text[escape + 1]))
                    i = escape + 2
                    if i > end:  # That quote was escaped; look for the next one.
                        end = text.find('"', i)
                token = ''.join(parts)
                i = end + 1
                tail_start = i
                while not kicad and i < len(text) and not text[i].isspace() and text[i] not in '()"':
                    i += 1
                if i > tail_start:
                    joined = JoinedAtom(token + text[tail_start:i])
                    joined.raw = '"' + token + '"' + text[tail_start:i]
                    token = joined
                else:
                    token = QuotedAtom(token)
            else:
                start = i
                while i < len(text) and not text[i].isspace() and text[i] not in '()':
                    i += 1
                token = text[start:i]
            stack[-1].append(token)
        count += 1
        if count > 2_000_000:
            raise ValidationError("The file has more than 2,000,000 tokens; designs this large are not supported.")
    if stack or len(roots) != 1 or not roots[0]:
        raise ValidationError("Malformed Specctra document; nothing was applied.")
    return roots[0]


_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t",
            "\a": "\\a", "\b": "\\b", "\f": "\\f", "\v": "\\v"}


def render(value) -> str:
    """Serialize a kicad=True parse() tree; quoted atoms stay quoted and escaped."""
    if isinstance(value, list):
        return "(" + " ".join(render(item) for item in value) + ")"
    if isinstance(value, QuotedAtom):
        return '"' + "".join(_ESCAPES.get(char, char) for char in value) + '"'
    return str(value)


def children(node: list, name: str) -> list[list]:
    return [value for value in node[1:] if isinstance(value, list) and value and value[0] == name]


def one(node: list, name: str) -> list:
    matches = children(node, name)
    if len(matches) != 1:
        raise ValidationError(f"Specctra requires exactly one {name} section.")
    return matches[0]
