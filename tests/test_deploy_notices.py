"""A to-do is not an outage, and the deploy must not page like one.

2026-10-06, on the owner's phone, both as a red siren:

    [CRITICAL] service_failed: ntx deploy: systemd unit files changed in git and
               need installing by hand: ntx-s004-demo2.service ntx-soak.service
    [CRITICAL] service_failed: ntx deploy: sudoers-ntx-deploy differs from
               /etc/sudoers.d/ntx-deploy; a managed unit may fail to restart

Neither was a failure. The first was a correct request for a root action; the
second was a false alarm from a check that has since been replaced. But every
`alert` in update.sh went through `--message`, which hard-coded
`Alert("service_failed", "critical", ...)` -- the same kind and severity as a
crashed trading node -- and skipped deduplication entirely. A deploy looping
every 65 seconds, as it did that morning, would have paged ~55 times an hour.

Two separate fixes:

* **Kind and severity are the caller's to say.** A rollback is critical; "please
  install this file" is a warning. The default stays `service_failed`/critical so
  the systemd OnFailure crash hook -- a real failure -- is unchanged.
* **One-shot messages are deduplicated on their own clock.** They cannot share
  `suppress_repeats`' state: that function treats each call as the complete set
  of current conditions and forgets anything absent, so a single deploy notice
  would wipe the main alerter's memory and re-page every standing condition.
"""

from __future__ import annotations

from pathlib import Path

from ntx.obs import alerts as A

ROOT = Path(__file__).resolve().parents[1]
UPDATE = (ROOT / "deploy" / "update.sh").read_text()
HOUR = 3600.0
# Only the lines that PUSH something; the same phrase also appears in plain logs.
PUSHES = [ln.strip() for ln in UPDATE.splitlines() if ln.strip().startswith(("alert ", "notice "))]


def _msg(text, kind="deploy_action_needed", sev="warning"):
    return A.Alert(kind, sev, text, {})


# ---- the deploy says what kind of thing happened -------------------------- #


def test_a_to_do_is_sent_as_a_warning_not_a_failure():
    line = next(ln for ln in UPDATE.splitlines() if ln.startswith("notice()"))
    assert "--severity warning" in line
    assert "service_failed" not in line


def test_install_and_grant_requests_use_notice_not_alert():
    for phrase in ("need installing by hand", "no sudo grant to restart", "missing sudoers grant"):
        line = next(ln for ln in PUSHES if phrase in ln)
        assert line.startswith("notice "), f"a to-do was sent as an incident: {line}"


def test_a_real_rollback_still_pages_as_critical():
    for phrase in ("ROLLBACK INCOMPLETE", "deploy failed and rolled back"):
        line = next(ln for ln in PUSHES if phrase in ln)
        assert line.startswith("alert "), f"an incident lost its severity: {line}"
    alert_fn = next(ln for ln in UPDATE.splitlines() if ln.startswith("alert()"))
    assert "--severity critical" in alert_fn or "--severity" not in alert_fn


# ---- one-shot messages are deduplicated on their own clock ---------------- #


def test_the_same_message_again_soon_is_not_resent(tmp_path):
    assert A.suppress_repeated_message(_msg("install X"), tmp_path, now=0.0)
    assert not A.suppress_repeated_message(_msg("install X"), tmp_path, now=65.0), (
        "a deploy looping every 65 s must not page every 65 s"
    )


def test_it_repeats_after_the_human_interval(tmp_path):
    A.suppress_repeated_message(_msg("install X"), tmp_path, now=0.0)
    assert A.suppress_repeated_message(_msg("install X"), tmp_path, now=6 * HOUR + 1)


def test_a_different_message_is_news(tmp_path):
    A.suppress_repeated_message(_msg("install X"), tmp_path, now=0.0)
    assert A.suppress_repeated_message(_msg("install Y"), tmp_path, now=60.0)


def test_it_does_not_touch_the_main_alerters_memory(tmp_path):
    """The bug this design avoids: one message wiping every standing condition."""
    standing = A.Alert("phantom_cache_position", "critical", "x", {"instrument": "SOL"})
    A.suppress_repeats([standing], tmp_path, now=0.0)
    A.suppress_repeated_message(_msg("install X"), tmp_path, now=10.0)
    assert A.suppress_repeats([standing], tmp_path, now=20.0) == [], (
        "a deploy notice made the main alerter forget, and re-page, a standing condition"
    )


def test_an_unwritable_state_dir_sends(tmp_path):
    """Never swallow on error (R75)."""
    assert A.suppress_repeated_message(_msg("install X"), tmp_path / "no" / "such", now=0.0)


def test_the_crash_hook_is_not_deduplicated():
    """OnFailure is a real failure each time; its behaviour is unchanged."""
    import inspect

    src = inspect.getsource(A._main)
    assert '"service_failed"' in src, "the crash hook's default kind must stay"
