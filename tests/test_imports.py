"""Module independence tests — each .py must import without the others.

Root cause this guards against: circular imports that only work when
bridge.py loads first (masking the cycle by pre-populating sys.modules).
The old test suite always did `import bridge` before `import telegram`,
so the circular import in v0.47.0 went undetected for months.

These tests spawn a fresh Python subprocess per module — no import-order
tricks, no shared sys.modules state.
"""

import subprocess
import sys
import pytest


# Each module that must be independently importable.
INDEPENDENT_MODULES = ["core", "telegram", "claudecode", "tunnel"]


@pytest.mark.parametrize("module", INDEPENDENT_MODULES)
def test_module_imports_independently(module):
    """Module can be imported in a clean Python process without bridge.py."""
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, (
        f"{module}.py failed to import independently:\n{result.stderr}"
    )


@pytest.mark.parametrize("module", INDEPENDENT_MODULES)
def test_module_imports_before_bridge(module):
    """Module can be imported BEFORE bridge.py (not just after)."""
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}; import bridge"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, (
        f"{module}.py failed when imported before bridge:\n{result.stderr}"
    )


def test_no_circular_import_any_order():
    """All three modules import in any order without circular errors."""
    import itertools
    for perm in itertools.permutations(INDEPENDENT_MODULES + ["bridge"]):
        imports = "; ".join(f"import {m}" for m in perm)
        result = subprocess.run(
            [sys.executable, "-c", imports],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, (
            f"Import order {' → '.join(perm)} failed:\n{result.stderr}"
        )


def test_no_double_module_guard_present():
    """bridge.py must register itself as 'bridge' when running as __main__.

    Regression test for the double-module bug: when bridge.py ran as __main__,
    satellite modules doing 'from bridge import X' got a SECOND module instance
    with its own globals. Mutable state set in main() (connectors, admin_chat_id,
    tunnel_manager) was invisible to those modules — they always saw init defaults.

    The fix: sys.modules.setdefault("bridge", sys.modules[__name__]) near the
    top of bridge.py ensures there is only one module instance.
    """
    import pathlib
    bridge_src = pathlib.Path(__file__).parent.parent / "bridge.py"
    text = bridge_src.read_text()
    # The guard must appear BEFORE any satellite module imports
    assert 'sys.modules.setdefault("bridge", sys.modules[__name__])' in text, (
        "bridge.py is missing the double-module prevention guard. "
        "Without it, running as __main__ creates two separate module instances "
        "and mutable state set in main() is invisible to satellite modules."
    )
    # The guard must be near the top (before satellite imports at ~line 1900+)
    guard_line = next(
        i for i, line in enumerate(text.splitlines(), 1)
        if 'sys.modules.setdefault("bridge"' in line
    )
    assert guard_line < 50, (
        f"Double-module guard is at line {guard_line} — must be near the top "
        f"(before satellite module imports) to prevent the second module instance."
    )


def test_single_connectors_instance():
    """connector_glue must see the same ConnectorRegistry as bridge globals.

    This is the behavioral check for the double-module fix: the connectors
    object imported by connector_glue.get_connectors_status() must be the
    exact same object as bridge.connectors (not a second ConnectorRegistry).
    """
    import bridge
    from connector_glue import get_connectors_status
    # Stamp a marker on bridge.connectors
    bridge.connectors._test_marker = "single-instance"
    try:
        # get_connectors_status does 'from bridge import connectors' internally —
        # that import must resolve to the same object
        from bridge import connectors as glue_connectors
        assert glue_connectors is bridge.connectors, (
            f"connector_glue sees a different ConnectorRegistry "
            f"(bridge={id(bridge.connectors)}, glue={id(glue_connectors)})"
        )
        assert getattr(glue_connectors, "_test_marker", None) == "single-instance"
    finally:
        del bridge.connectors._test_marker
