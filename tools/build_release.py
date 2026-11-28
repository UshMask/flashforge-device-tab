"""Build the single-file plugin with compact translation tables."""

import argparse
import ast
from pathlib import Path


def build(source):
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    tables = [
        node for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id in {"CORE_MESSAGES", "EXTENDED_MESSAGES"}
    ]
    if len(tables) != 2:
        raise ValueError("Expected two message tables")

    def spelling(node):
        if node.lineno != node.end_lineno:
            raise ValueError("Expected a single-line message or translation")
        # Python AST column offsets count UTF-8 bytes.
        return lines[node.lineno - 1][node.col_offset:node.end_col_offset]

    for node in reversed(tables):
        compact = [lines[node.lineno - 1]]
        for key, row in zip(node.value.keys, node.value.values):
            values = b", ".join(spelling(value) for value in row.elts)
            compact.append(b"    " + spelling(key) + b": (" + values + b",),\n")
        compact.append(lines[node.end_lineno - 1])
        lines[node.lineno - 1:node.end_lineno] = compact

    result = b"".join(lines)
    if ast.dump(ast.parse(result)) != ast.dump(tree):
        raise ValueError("Compact build changed the Python syntax tree")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.source.resolve() == args.output.resolve():
        parser.error("Use separate source and output paths")
    source = args.source.read_text(encoding="utf-8").encode("utf-8")
    args.output.write_bytes(build(source))
