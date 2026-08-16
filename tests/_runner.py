"""Shared standalone test runner.

Each test file can be run directly (`python3 tests/test_x.py`) as well as under
pytest. The runner lived duplicated verbatim in every test file; this is that
code, once.

Imported only from inside `if __name__ == "__main__"`, so pytest never touches
it and needs no path setup.
"""

import sys


def run_tests(namespace: dict) -> int:
    """Run every test_* callable in `namespace`. Returns a process exit code."""
    tests = [v for k, v in sorted(namespace.items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {fn.__name__}: {exc}")
        except Exception as exc:            # a broken test must report, not abort
            failed += 1
            print(f"  ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


def main(namespace: dict) -> None:
    sys.exit(run_tests(namespace))
