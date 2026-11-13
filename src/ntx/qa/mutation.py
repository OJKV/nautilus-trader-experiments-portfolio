"""Mutation testing -- testing the tests.

Ten of the twelve bugs found in Phase 2 made results look **better** than they
were. That is the signature of a test suite that confirms rather than
challenges. Mutation testing attacks that directly: deliberately break the
source, and check the tests notice.

    surviving mutant = a change to the code that NO test objected to
                     = a gap in the suite, at that exact line

Related to the random-entry control, and worth distinguishing:

  * random-entry control breaks the **signal** to test whether a **result**
    has edge;
  * mutation testing breaks the **code** to test whether the **tests** have teeth.

Both are null-hypothesis tests against a deliberately sabotaged version of
ourselves.

Custom rather than `mutmut` deliberately: the targets here are a handful of
load-bearing numeric functions, and a 200-line harness we can read beats a
dependency whose behaviour we would also have to verify.

    python -m ntx.qa.mutation                    # default targets
    python -m ntx.qa.mutation --module risk/sizing.py --max 40
"""

from __future__ import annotations

import argparse
import ast
import random
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "src" / "ntx"

# Each target pairs a module with the tests that are supposed to defend it, so a
# survivor points at a specific missing test rather than "somewhere in the suite".
TARGETS: dict[str, str] = {
    "risk/sizing.py": "tests/test_sizing.py",
    "strategies/exits.py": "tests/test_exits.py",
    "live/reconcile.py": "tests/test_reconcile.py",
    "data/integrity.py": "tests/test_data_pipeline.py",
    "data/aggregate.py": "tests/test_data_pipeline.py",
    "backtest/metrics.py": "tests/test_metrics.py",
    "backtest/sweep.py": "tests/test_sweep.py",
    "config/geo.py": "tests/test_geo.py",
    "strategies/watchdog.py": "tests/test_watchdog.py",
    "strategies/bars.py": "tests/test_bars.py",
    "obs/alerts.py": "tests/test_alerts.py",
}

CMP_SWAP = {
    ast.Lt: ast.GtE,
    ast.GtE: ast.Lt,
    ast.Gt: ast.LtE,
    ast.LtE: ast.Gt,
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
}
BOOL_SWAP = {ast.And: ast.Or, ast.Or: ast.And}
ARITH_SWAP = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Div, ast.Div: ast.Mult}


@dataclass
class Mutant:
    module: str
    line: int
    description: str
    killed: bool = False
    error: str = ""


class _Collector(ast.NodeVisitor):
    """Find mutable sites. Records (node id, kind, line, description)."""

    def __init__(self) -> None:
        self.sites: list[tuple[int, str, int, str]] = []

    def visit_Compare(self, node: ast.Compare) -> None:
        for op in node.ops:
            if type(op) in CMP_SWAP:
                self.sites.append(
                    (
                        id(op),
                        "cmp",
                        node.lineno,
                        f"{type(op).__name__} -> {CMP_SWAP[type(op)].__name__}",
                    )
                )
        self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        if type(node.op) in BOOL_SWAP:
            self.sites.append(
                (
                    id(node.op),
                    "bool",
                    node.lineno,
                    f"{type(node.op).__name__} -> {BOOL_SWAP[type(node.op)].__name__}",
                )
            )
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if type(node.op) in ARITH_SWAP:
            self.sites.append(
                (
                    id(node.op),
                    "arith",
                    node.lineno,
                    f"{type(node.op).__name__} -> {ARITH_SWAP[type(node.op)].__name__}",
                )
            )
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, bool):
            self.sites.append(
                (id(node), "const_bool", node.lineno, f"{node.value} -> {not node.value}")
            )
        elif isinstance(node.value, (int, float)) and node.value not in (0, 1):
            self.sites.append(
                (id(node), "const_num", node.lineno, f"{node.value} -> {node.value * 2}")
            )
        self.generic_visit(node)


class _Mutator(ast.NodeTransformer):
    def __init__(self, target_id: int) -> None:
        self.target = target_id
        self.applied = False

    def _swap_op(self, op, table):
        if id(op) == self.target and type(op) in table:
            self.applied = True
            return table[type(op)]()
        return op

    def visit_Compare(self, node):
        self.generic_visit(node)
        node.ops = [self._swap_op(o, CMP_SWAP) for o in node.ops]
        return node

    def visit_BoolOp(self, node):
        self.generic_visit(node)
        node.op = self._swap_op(node.op, BOOL_SWAP)
        return node

    def visit_BinOp(self, node):
        self.generic_visit(node)
        node.op = self._swap_op(node.op, ARITH_SWAP)
        return node

    def visit_Constant(self, node):
        if id(node) == self.target:
            self.applied = True
            if isinstance(node.value, bool):
                return ast.copy_location(ast.Constant(value=not node.value), node)
            return ast.copy_location(ast.Constant(value=node.value * 2), node)
        return node


def _run_tests(test_path: str, timeout: int) -> bool:
    """True if the suite PASSED (i.e. the mutant survived)."""
    r = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            test_path,
            "-x",
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return r.returncode == 0


def mutate_module(
    rel: str, test_path: str, *, max_mutants: int, seed: int, timeout: int
) -> list[Mutant]:
    path = SRC / rel
    original = path.read_text()
    tree = ast.parse(original)
    collector = _Collector()
    collector.visit(tree)
    sites = collector.sites
    rng = random.Random(seed)
    if len(sites) > max_mutants:
        sites = rng.sample(sites, max_mutants)

    results: list[Mutant] = []
    for node_id, _kind, line, desc in sites:
        fresh = ast.parse(original)
        mut = _Mutator(node_id)
        new_tree = mut.visit(fresh)
        if not mut.applied:
            continue
        try:
            source = ast.unparse(ast.fix_missing_locations(new_tree))
        except Exception as e:
            results.append(Mutant(rel, line, desc, killed=True, error=f"unparse: {e}"))
            continue
        try:
            path.write_text(source)
            survived = _run_tests(test_path, timeout)
            results.append(Mutant(rel, line, desc, killed=not survived))
        except subprocess.TimeoutExpired:
            # A hang means the mutation changed behaviour drastically -> killed.
            results.append(Mutant(rel, line, desc, killed=True, error="timeout"))
        finally:
            path.write_text(original)  # ALWAYS restore
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description="mutation testing: do the tests have teeth?")
    ap.add_argument(
        "--module",
        action="append",
        default=None,
        help="module path relative to src/ntx (repeatable)",
    )
    ap.add_argument("--max", type=int, default=25, help="mutants per module")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args()

    targets = {m: TARGETS[m] for m in args.module} if args.module else TARGETS
    all_results: list[Mutant] = []
    t0 = time.time()
    for rel, test_path in targets.items():
        print(f"\n### {rel}  (defended by {test_path})")
        res = mutate_module(
            rel, test_path, max_mutants=args.max, seed=args.seed, timeout=args.timeout
        )
        killed = sum(1 for r in res if r.killed)
        score = killed / len(res) * 100 if res else 0.0
        print(f"  {killed}/{len(res)} killed = {score:.0f}% mutation score")
        for r in res:
            if not r.killed:
                print(f"  SURVIVED  line {r.line:>4}  {r.description}")
        all_results += res

    killed = sum(1 for r in all_results if r.killed)
    total = len(all_results)
    print(f"\n{'=' * 64}")
    print(
        f"overall: {killed}/{total} killed = {killed / total * 100:.0f}% in {time.time() - t0:.0f}s"
    )
    survivors = [r for r in all_results if not r.killed]
    if survivors:
        print(f"\n{len(survivors)} SURVIVING mutant(s) -- each is a missing test:")
        for r in survivors:
            print(f"  {r.module}:{r.line}  {r.description}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
