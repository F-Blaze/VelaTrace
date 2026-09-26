"""Bounded, non-evaluating Specctra S-expression reader."""
from .errors import ValidationError


class QuotedAtom(str):
    """Preserve lexical quoting for safe DSN reserialization."""


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
                token = ''
                while i < len(text) and text[i] != '"':
                    if kicad and text[i] == '\\':
                        i += 1
                        if i >= len(text):
                            break
                        token += KICAD_ESCAPES.get(text[i], text[i])
                    else:
                        token += text[i]
                    i += 1
                if i == len(text):
                    raise ValidationError("Unterminated Specctra string.")
                i += 1
                token = QuotedAtom(token)
            else:
                start = i
                while i < len(text) and not text[i].isspace() and text[i] not in '()':
                    i += 1
                token = text[start:i]
            stack[-1].append(token)
        count += 1
        if count > 2_000_000:
            raise ValidationError("Specctra input contains too many tokens.")
    if stack or len(roots) != 1 or not roots[0]:
        raise ValidationError("Malformed Specctra document; nothing was applied.")
    return roots[0]


def children(node: list, name: str) -> list[list]:
    return [value for value in node[1:] if isinstance(value, list) and value and value[0] == name]


def one(node: list, name: str) -> list:
    matches = children(node, name)
    if len(matches) != 1:
        raise ValidationError(f"Specctra requires exactly one {name} section.")
    return matches[0]
