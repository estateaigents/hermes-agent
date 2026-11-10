"""Token-aware, fail-closed L3-1 evidence receipts (t_d2c33545).

The bug: the receipt shape `{claim, file, evidence_cmd, exit_code}` read
"exit_code (0 = pass)", but the probe convention is that a FAILED leg prints
one `FAIL: <leg>: <detail>` line and still exits 0 (so the output can never
carry the PASS token).  A receipt for a FAILED leg therefore read as a pass.

`probe_contract.receipt_verdict` is the one shared judge; the plugin's receipt
consumer (`_gate9_h2_receipts_are_valid`, used by the Gate 2 no_repo_sha
exemption and the Gate 9 Check H2 host-config skip) must route through it.

Four fixtures, both directions:
  A. exit 0, prints `true`                              -> pass
  B. exit 0, prints `FAIL: leg: x` (the bug)            -> NOT a pass
  C. exit 1, ACCEPTANCE v2 `expect_exit: 1`             -> pass
  D. exit 127                                           -> error (probe_repair)
"""

import importlib.util
from pathlib import Path

import pytest

_HERE = Path(__file__).parent
PLUGIN_PATH = _HERE.parent / "__init__.py"
CONTRACT_PATH = _HERE.parent / "probe_contract.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def pc():
    return _load("probe_contract_receipt", CONTRACT_PATH)


@pytest.fixture()
def plugin():
    # The consumer these wiring legs exercise lives in the tooling package's
    # __init__.py, which is not regrafted into this repo yet — skip, don't fail.
    if not PLUGIN_PATH.exists():
        pytest.skip("plugin __init__.py absent in this repo; wiring legs run where the plugin lives")
    return _load("completion_verifier_receipt", PLUGIN_PATH)


def _receipt(**kw):
    base = {
        "claim": "did the thing",
        "file": "/root/.hermes/plugins/completion-verifier/__init__.py",
        "evidence_cmd": "bash /root/.hermes/scripts/verify/probes/x_probe.sh",
        "exit_code": 0,
    }
    base.update(kw)
    return base


V1_BLOCK = {"legacy": True, "expected": "true", "schema_version": 1}
V2_BLOCK = {"legacy": False, "expect_exit": 1, "schema_version": 2}


# ── Fixture A ─────────────────────────────────────────────────────────────

def test_fixture_a_exit0_with_pass_token_is_a_pass(pc):
    v = pc.receipt_verdict(_receipt(exit_code=0, stdout="true\n"), V1_BLOCK)
    assert v["verdict"] == "pass", v


# ── Fixture B: the bug ────────────────────────────────────────────────────

def test_fixture_b_exit0_with_fail_line_is_not_a_pass(pc):
    v = pc.receipt_verdict(_receipt(exit_code=0, stdout="FAIL: leg: x\n"), V1_BLOCK)
    assert v["verdict"] == "false", v
    assert "FAIL" in v["reason"]


def test_fixture_b_fail_line_wins_over_exit_code_without_a_block(pc):
    """No ACCEPTANCE block at all: the FAIL token still forces false."""
    v = pc.receipt_verdict(_receipt(exit_code=0, stdout="noise\nFAIL: leg: x\n"))
    assert v["verdict"] == "false", v


def test_fixture_b_accepts_alternate_output_keys(pc):
    for key in ("output", "out", "probe_output"):
        v = pc.receipt_verdict(_receipt(**{key: "FAIL: leg: x"}))
        assert v["verdict"] == "false", (key, v)


def test_self_declared_pass_false_is_never_a_pass(pc):
    v = pc.receipt_verdict(_receipt(exit_code=0, **{"pass": False}))
    assert v["verdict"] == "false", v


# ── Fixture C ─────────────────────────────────────────────────────────────

def test_fixture_c_v2_expect_exit_1_on_exit_1_is_a_pass(pc):
    v = pc.receipt_verdict(_receipt(exit_code=1), V2_BLOCK)
    assert v["verdict"] == "pass", v


def test_fixture_c_v2_bound_still_rejects_the_wrong_nonzero(pc):
    v = pc.receipt_verdict(_receipt(exit_code=2), V2_BLOCK)
    assert v["verdict"] == "false", v


# ── Fixture D ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("rc", [127, 126, 124])
def test_fixture_d_machinery_exit_is_error_not_false_not_pass(pc, rc):
    v = pc.receipt_verdict(_receipt(exit_code=rc), V2_BLOCK)
    assert v["verdict"] == "error", v


# ── Fail-closed edges ─────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [None, "x", True])
def test_malformed_exit_code_is_never_a_pass(pc, bad):
    v = pc.receipt_verdict(_receipt(exit_code=bad))
    assert v["verdict"] == "false", v


def test_v1_block_without_its_token_is_not_a_pass(pc):
    """Absence of the PASS token must never read as passed."""
    v = pc.receipt_verdict(_receipt(exit_code=0, stdout="something else"), V1_BLOCK)
    assert v["verdict"] == "false", v


def test_v1_block_with_empty_recorded_output_is_not_a_pass(pc):
    v = pc.receipt_verdict(_receipt(exit_code=0, stdout=""), V1_BLOCK)
    assert v["verdict"] == "false", v


# ── Wiring: the plugin's receipt consumer must use it ────────────────────

def test_consumer_rejects_fail_line_with_exit_zero(plugin):
    ok, reason = plugin._gate9_h2_receipts_are_valid(
        {"receipts": [_receipt(exit_code=0, stdout="FAIL: leg: x")]})
    assert ok is False
    assert "FAIL" in reason


def test_consumer_still_accepts_grandfathered_exit_zero_receipts(plugin):
    """Historical receipts record no output; exit-0 grandfather must survive."""
    ok, reason = plugin._gate9_h2_receipts_are_valid({"receipts": [_receipt()]})
    assert ok is True, reason


def test_consumer_rejects_nonzero_exit(plugin):
    ok, reason = plugin._gate9_h2_receipts_are_valid(
        {"receipts": [_receipt(exit_code=3)]})
    assert ok is False
    assert "3" in reason


def test_consumer_rejects_missing_claim(plugin):
    ok, _reason = plugin._gate9_h2_receipts_are_valid(
        {"receipts": [_receipt(claim="  ")]})
    assert ok is False
