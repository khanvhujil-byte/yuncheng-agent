"""Parse planner JSON; repair only the observed extra quote after a number.

Never complete truncated arrays, invent IDs, drop fields or change string data.
All repaired data still passes identity, structure and domain audits.
"""
import json
import logging
import re

logger = logging.getLogger(__name__)
NUMBER = re.compile(r'-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?')


def loads(raw):
    text = raw.strip()
    if text.startswith('```') and text.endswith('```'):
        text = text[3:-3].removeprefix('json').strip()

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise json.JSONDecodeError('Duplicate object key: ' + key, text, 0)
            result[key] = value
        return result

    def finite(value):
        raise json.JSONDecodeError('Nonfinite JSON number: ' + value, text, 0)

    try:
        return json.loads(text, object_pairs_hook=unique, parse_constant=finite)
    except json.JSONDecodeError:
        # Scan only outside strings: a regex alone could corrupt prose or IDs
        # containing numeric-looking substrings.
        quoted = escaped = False
        removals = set()
        for index, char in enumerate(text):
            if index in removals:
                continue
            if quoted:
                if escaped:
                    escaped = False
                elif char == '\\':
                    escaped = True
                elif char == '"':
                    quoted = False
                continue
            if char == ':':
                start = index + 1
                while start < len(text) and text[start].isspace():
                    start += 1
                number = NUMBER.match(text, start)
                if number and number.end() < len(text) and text[number.end()] == '"':
                    after = number.end() + 1
                    while after < len(text) and text[after].isspace():
                        after += 1
                    if after < len(text) and text[after] in ',}]':
                        removals.add(number.end())
            elif char == '"':
                quoted = True
        if not removals or len(removals) > 16:
            raise
        repaired = ''.join(c for index, c in enumerate(text) if index not in removals)
        result = json.loads(repaired, object_pairs_hook=unique, parse_constant=finite)
        logger.warning('Planner JSON syntax repaired: %s extra numeric-value quote(s); domain validation remains required', len(removals))
        return result
