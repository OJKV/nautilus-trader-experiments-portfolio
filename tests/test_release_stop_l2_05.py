"""L2-05: once the cancel has gone through, nothing may raise.

`release_stop` cancels the pre-restart venue stop and the caller places a fresh
one on the very next line (L-10). Between those two lines the position has no
protection at all. Anything that raises in that window does not "abort safely" --
it aborts with the stop already gone:

    _adopt catches it, raises "refusing to place a second one" (base.py:530-546)
    -> on_start fails -> watch_startup exits 3 -> systemd RestartSec=120
    -> a live, leveraged position sits unprotected for minutes,
       and on restart it has no journal row and no stop, so adoption FLATTENS it.

The verification after the cancel is two more REST calls. A timeout, a rate
limit, or one call leaving over IPv6 (F061) is enough to trigger all of that --
for a cancel that actually succeeded.

So: before the cancel, raise freely (nothing has changed at the venue). After an
accepted cancel, never raise. Hand the price back, let the replacement go out,
and record the ambiguity for the sweep to deal with. If the cancel truly did not
take effect, OKX rejects the replacement (one close-position stop per position,
F036) -- which is loud, counted and repairable, unlike a node that has exited.
"""

from __future__ import annotations

import pytest

from ntx.config.settings import Settings

SOL = "SOL-USDT-SWAP.OKX"
OUR = {"algoId": "a1", "slTriggerPx": "95.0", "reduceOnly": "true", "closeFraction": "1"}


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        NTX_VENUE="okx_demo",
        OKX_DEMO_API_KEY="k",
        OKX_DEMO_API_SECRET="s",
        OKX_DEMO_API_PASSPHRASE="p",
        NTX_DATA_DIR=str(tmp_path),
        NTX_EQUITY_OVERRIDE_USD="123.45",
    )


def _venue(monkeypatch, *, after_cancel, cancel=None):
    """A venue whose pre-cancel lookup finds our stop, and whose post-cancel
    lookup behaves as `after_cancel` says."""
    from ntx.ops import venue_stops

    calls = {"get": 0, "cancelled": []}

    def get(base, path, creds, demo):
        calls["get"] += 1
        if calls["get"] <= 2:  # the pre-cancel conditional + oco lookup
            return {"code": "0", "data": [OUR] if "conditional" in path else []}
        if after_cancel == "raises":
            raise TimeoutError("read timed out")
        if after_cancel == "error-code":
            return {"code": "50011", "msg": "Too Many Requests"}
        if after_cancel == "still-there":
            return {"code": "0", "data": [OUR] if "conditional" in path else []}
        return {"code": "0", "data": []}

    def post(base, path, body, creds, demo):
        calls["cancelled"].append(body)
        return cancel or {"code": "0", "data": [{"sCode": "0"}]}

    monkeypatch.setattr(venue_stops, "_signed_get", get)
    monkeypatch.setattr(venue_stops, "_signed_post", post)
    return venue_stops, calls


@pytest.mark.parametrize("failure", ["raises", "error-code", "still-there"])
def test_verification_after_a_successful_cancel_never_strands_the_position(
    monkeypatch, tmp_path, failure
):
    """The stop is already cancelled. Returning the price is the only safe move."""
    vs, calls = _venue(monkeypatch, after_cancel=failure)
    price = vs.release_stop(SOL, _settings(tmp_path), True)
    assert price == 95.0, "the caller needs the price to re-place the stop"
    assert calls["cancelled"], "the cancel must still have been sent"


def test_a_refused_cancel_still_raises(monkeypatch, tmp_path):
    """Nothing was lost -- the old stop is still protecting the position -- so
    aborting is correct here and must not be swallowed by the fix above."""
    vs, _ = _venue(
        monkeypatch, after_cancel="clean", cancel={"code": "1", "msg": "Operation failed"}
    )
    with pytest.raises(RuntimeError, match="cancel failed"):
        vs.release_stop(SOL, _settings(tmp_path), True)


def test_a_per_order_cancel_failure_still_raises(monkeypatch, tmp_path):
    vs, _ = _venue(
        monkeypatch,
        after_cancel="clean",
        cancel={"code": "2", "data": [{"algoId": "a1", "sCode": "51400", "sMsg": "nope"}]},
    )
    with pytest.raises(RuntimeError, match="cancel of a1 failed"):
        vs.release_stop(SOL, _settings(tmp_path), True)


def test_an_ambiguous_venue_still_raises_before_anything_is_cancelled(monkeypatch, tmp_path):
    """Pre-cancel raises keep their old behaviour: nothing has changed yet."""
    from ntx.ops import venue_stops

    monkeypatch.setattr(
        venue_stops,
        "_signed_get",
        lambda base, path, creds, demo: {
            "code": "0",
            "data": [OUR, dict(OUR, algoId="a2")] if "conditional" in path else [],
        },
    )
    sent = []
    monkeypatch.setattr(
        venue_stops, "_signed_post", lambda *a, **k: sent.append(1) or {"code": "0"}
    )
    with pytest.raises(RuntimeError, match="expected one"):
        venue_stops.release_stop(SOL, _settings(tmp_path), True)
    assert sent == [], "nothing may be cancelled when the venue is ambiguous"


def test_a_leftover_stop_is_reported_not_silent(monkeypatch, tmp_path):
    """Proceeding is right, but the operator must be able to see that the venue
    still lists a stop -- that is what the stale-stop sweep exists for."""
    vs, _ = _venue(monkeypatch, after_cancel="still-there")
    seen: list[str] = []
    vs.release_stop(SOL, _settings(tmp_path), True, on_ambiguity=seen.append)
    assert seen and "still" in seen[0].lower()
