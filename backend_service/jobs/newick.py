from __future__ import annotations

import math
import re

SAFE_LABEL = re.compile(r'^[A-Za-z0-9_.|+\-]+$')
INTERNAL_LABEL = re.compile(r'^[A-Za-z0-9_.+\-]*$')


def summarize_newick(text: str, *, expected_leaf_count: int) -> dict:
    source = text.strip()
    if not source:
        raise ValueError('FastTree produced an empty Newick tree')
    if any(ord(char) < 32 and char not in '\t\n\r' for char in source):
        raise ValueError('Newick tree contains control characters')

    index = 0
    leaves: list[str] = []
    branch_length_count = 0
    internal_node_count = 0

    def skip_space() -> None:
        nonlocal index
        while index < len(source) and source[index].isspace():
            index += 1

    def parse_token(stoppers: str) -> str:
        nonlocal index
        start = index
        while index < len(source) and source[index] not in stoppers:
            if source[index].isspace():
                break
            index += 1
        return source[start:index]

    def parse_branch_length() -> None:
        nonlocal index, branch_length_count
        skip_space()
        if index >= len(source) or source[index] != ':':
            return
        index += 1
        skip_space()
        token = parse_token(',);')
        try:
            value = float(token)
        except ValueError as exc:
            raise ValueError('Newick tree contains an invalid branch length') from exc
        if not math.isfinite(value) or value < 0:
            raise ValueError('Newick branch lengths must be finite and non-negative')
        branch_length_count += 1

    def parse_subtree() -> None:
        nonlocal index, internal_node_count
        skip_space()
        if index >= len(source):
            raise ValueError('Newick tree ended unexpectedly')
        if source[index] == '(':
            index += 1
            child_count = 0
            while True:
                parse_subtree()
                child_count += 1
                skip_space()
                if index < len(source) and source[index] == ',':
                    index += 1
                    continue
                break
            if child_count < 2 or index >= len(source) or source[index] != ')':
                raise ValueError('Newick internal nodes must contain at least two children')
            index += 1
            internal_node_count += 1
            skip_space()
            label = parse_token(':,);')
            if label and not INTERNAL_LABEL.fullmatch(label):
                raise ValueError('Newick tree contains an unsafe internal label')
        else:
            label = parse_token(':,();')
            if not label or not SAFE_LABEL.fullmatch(label):
                raise ValueError('Newick tree contains an unsafe or empty leaf label')
            leaves.append(label)
        parse_branch_length()

    parse_subtree()
    skip_space()
    if index >= len(source) or source[index] != ';':
        raise ValueError('Newick tree must end with a semicolon')
    index += 1
    skip_space()
    if index != len(source):
        raise ValueError('Newick tree contains trailing content')
    if len(leaves) != expected_leaf_count:
        raise ValueError(
            f'Newick leaf count {len(leaves)} does not match input count '
            f'{expected_leaf_count}'
        )
    if len(set(leaves)) != len(leaves):
        raise ValueError('Newick leaf labels must be unique')
    return {
        'leaf_count': len(leaves),
        'internal_node_count': internal_node_count,
        'branch_length_count': branch_length_count,
        'identifier_preview': leaves[:20],
    }
