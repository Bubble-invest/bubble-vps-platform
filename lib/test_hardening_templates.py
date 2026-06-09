"""Golden-file tests for the SPEC-005 hardening templates.

Each template is rendered with the bubble-internal tenant.yaml's
cfg.hardening values and compared byte-for-byte to a committed golden file
under lib/golden/hardening/. The golden file content matches what's actually
on joris-cx33 (manually hardened 2026-05-06) — this is what makes the dogfood
test pass.

Run with: python3.12 -m pytest lib/test_hardening_templates.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, StrictUndefined


REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = REPO_ROOT / "pyinfra" / "templates"
GOLDEN_DIR = Path(__file__).resolve().parent / "golden" / "hardening"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib.tenant_loader import load_tenant  # noqa: E402


# ─── Helpers ────────────────────────────────────────────────────────────────


def _jinja_env() -> Environment:
    """Same jinja2 env config pyinfra uses internally (default Environment).

    pyinfra's files.template uses jinja2's default Environment(); we mirror it
    so the template-rendering path is exactly the same. StrictUndefined gives
    us loud errors if a template variable is missing.
    """
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )


def _render(template_name: str, **kwargs) -> str:
    env = _jinja_env()
    return env.get_template(template_name).render(**kwargs)


def _golden(name: str) -> str:
    return (GOLDEN_DIR / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def bubble_internal_cfg():
    """Load the bubble-internal tenant.yaml (the dogfood tenant)."""
    data_repo = (REPO_ROOT / ".." / "bubble-vps-data").resolve()
    return load_tenant("bubble-internal", data_repo)


# ─── sshd template ──────────────────────────────────────────────────────────


def test_sshd_template_matches_golden(bubble_internal_cfg):
    """Rendering the sshd template with bubble-internal values must match the
    committed golden file (byte-for-byte) — which is the live content on
    joris-cx33. This is what makes the dogfood pyinfra run report zero changes.
    """
    sshd = bubble_internal_cfg.hardening.sshd
    rendered = _render(
        "sshd_99-bubble.conf.j2",
        permit_root_login=sshd.permit_root_login,
        password_authentication=sshd.password_authentication,
        max_auth_tries=sshd.max_auth_tries,
    )
    expected = _golden("sshd_99-bubble.conf")
    assert rendered == expected, (
        f"sshd template diverged from golden. Diff:\n"
        f"--- expected ---\n{expected!r}\n--- got ---\n{rendered!r}"
    )


def test_sshd_template_handles_custom_values():
    """Override permit_root_login=prohibit-password — the rendered output
    reflects the custom value.
    """
    rendered = _render(
        "sshd_99-bubble.conf.j2",
        permit_root_login="prohibit-password",
        password_authentication="no",
        max_auth_tries=5,
    )
    assert "PermitRootLogin prohibit-password" in rendered
    assert "MaxAuthTries 5" in rendered
    # And the static directives remain
    assert "PubkeyAuthentication yes" in rendered
    assert "ChallengeResponseAuthentication no" in rendered


# ─── fail2ban template ──────────────────────────────────────────────────────


def test_fail2ban_template_matches_golden(bubble_internal_cfg):
    f2b = bubble_internal_cfg.hardening.fail2ban
    bans = f2b.bans
    rendered = _render(
        "fail2ban_bubble.conf.j2",
        sshd_jail=f2b.sshd_jail,
        maxretry=bans.maxretry,
        findtime_minutes=bans.findtime_minutes,
        bantime_hours=bans.bantime_hours,
    )
    expected = _golden("fail2ban_bubble.conf")
    assert rendered == expected, (
        f"fail2ban template diverged from golden. Diff:\n"
        f"--- expected ---\n{expected!r}\n--- got ---\n{rendered!r}"
    )


def test_fail2ban_template_handles_custom_values():
    rendered = _render(
        "fail2ban_bubble.conf.j2",
        sshd_jail="default",
        maxretry=10,
        findtime_minutes=30,
        bantime_hours=24,
    )
    assert "maxretry = 10" in rendered
    assert "findtime = 30m" in rendered
    assert "bantime = 24h" in rendered
    assert "mode = default" in rendered
    # Recidive jail's own (unrelated) values stay literal:
    assert "[recidive]" in rendered
    assert "maxretry = 3" in rendered.split("[recidive]")[1]


# ─── unattended-upgrades template ───────────────────────────────────────────


def test_unattended_template_matches_golden(bubble_internal_cfg):
    uu = bubble_internal_cfg.hardening.unattended_upgrades
    rendered = _render(
        "unattended-upgrades-bubble.j2",
        auto_reboot_time=uu.auto_reboot_time,
    )
    expected = _golden("unattended-upgrades-bubble")
    assert rendered == expected, (
        f"unattended template diverged from golden. Diff:\n"
        f"--- expected ---\n{expected!r}\n--- got ---\n{rendered!r}"
    )


def test_unattended_template_handles_custom_values():
    rendered = _render(
        "unattended-upgrades-bubble.j2",
        auto_reboot_time="03:30",
    )
    assert 'Automatic-Reboot-Time "03:30"' in rendered
    assert 'Automatic-Reboot "true"' in rendered  # static
