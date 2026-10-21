"""Static route inventory and versioned scenarios; importing makes no cloud calls."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
from scripts.e2e.reporting import ROOT

CATALOG = ROOT / 'examples/e2e/scenarios.json'


def cases():
    rows = json.loads(CATALOG.read_text())['cases']
    if len({row['id'] for row in rows}) != len(rows):
        raise ValueError('duplicate scenario ID')
    ids = {row['id'] for row in rows}
    for row in rows:
        if not set(row.get('dependencies', [])) <= ids:
            raise ValueError('unknown scenario dependency: ' + row['id'])
        if not row.get('expected') or not row.get('routes'):
            raise ValueError('scenario requires routes and independent expectations: ' + row['id'])
    visiting, visited = set(), set()
    graph = {r['id']: r.get('dependencies', []) for r in rows}
    def visit(node):
        if node in visiting:
            raise ValueError('scenario dependency cycle: ' + node)
        if node in visited:
            return
        visiting.add(node)
        for dependency in graph[node]:
            visit(dependency)
        visiting.remove(node)
        visited.add(node)
    for node in graph:
        visit(node)
    return rows


def fingerprint():
    return hashlib.sha256(CATALOG.read_bytes()).hexdigest()


def routes():
    """Inspect declarations without importing FastAPI or initializing settings."""
    result = set()
    tree = ast.parse((ROOT / 'src/main.py').read_text())
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr in {'get', 'post', 'put', 'patch', 'delete'}
                    and decorator.args and isinstance(decorator.args[0], ast.Constant)):
                result.add(decorator.func.attr.upper() + ' ' + decorator.args[0].value)
    return sorted(result | {'GET /openapi.json', 'GET /docs', 'GET /redoc'})


def inventory():
    rows = cases()
    return {route: [r['id'] for r in rows if route in r['routes']] for route in routes()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    mapping = inventory()
    unknown = sorted({p for r in cases() for p in r['routes']} - set(mapping))
    missing = [p for p, ids in mapping.items() if not ids]
    print(json.dumps({'registry_hash': fingerprint(), 'cases': len(cases()),
                      'routes': mapping, 'unmapped': missing, 'unknown_routes': unknown}, indent=2))
    if args.check and (missing or unknown):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
