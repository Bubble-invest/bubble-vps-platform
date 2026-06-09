"""Static doc-consistency tests (Step 7d / SPEC-019).

Light text-presence checks against the operator-facing docs:
  1. INSTALL.md lists all required secret keys
  2. INSTALL.md and ONBOARDING.md describe the same command sequence
  3. SECURITY.md mentions both SOPS recipients (operator master + per-tenant box)
  4. ARCHITECTURE.md links to SPEC-* docs in each major section
  5. README.md contains a numeric test count (e.g. "204 / 204")

These tests catch DRIFT — they do not enforce content quality. If we add a 6th
required secret key but forget to update INSTALL.md, test #1 fails.

Run with: python3 -m pytest lib/test_docs_consistency.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = REPO_ROOT / "docs"
README = REPO_ROOT / "README.md"
INSTALL = DOCS_DIR / "INSTALL.md"
ONBOARDING = DOCS_DIR / "ONBOARDING.md"
ARCHITECTURE = DOCS_DIR / "ARCHITECTURE.md"
SECURITY = DOCS_DIR / "SECURITY.md"

# The five required secret keys per the per-tenant happy path in SPEC-019.
# If a sixth required key is added (or one is removed/renamed), these tests
# will start failing — that's the point: they catch drift between the platform
# and the operator-facing docs.
REQUIRED_SECRET_KEYS = [
    "TELEGRAM_BOT_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "TAILSCALE_AUTHKEY",
    "PHONEHOME_TOKEN",
    "GITHUB_TOKEN",
]

# The operator scripts that the deploy command sequence walks through.
DEPLOY_SCRIPTS = [
    "new-tenant.sh",
    "provision-tenant.sh",
    "operator-set-secret.sh",
    "deploy.sh",
]


# ─── Helpers ────────────────────────────────────────────────────────────────


def _read(path: Path) -> str:
    assert path.is_file(), f"expected doc to exist: {path}"
    return path.read_text(encoding="utf-8")


# ─── Tests ──────────────────────────────────────────────────────────────────


def test_install_md_lists_all_required_keys() -> None:
    """INSTALL.md must mention every required secret key.

    Catches drift when a new required key is added but INSTALL.md is not
    updated. The operator would otherwise follow the doc and miss a step.
    """
    content = _read(INSTALL)
    missing = [key for key in REQUIRED_SECRET_KEYS if key not in content]
    assert not missing, (
        f"INSTALL.md is missing references to required secret keys: {missing}. "
        f"If a key was renamed or removed, update REQUIRED_SECRET_KEYS in "
        f"{__file__} and the doc accordingly."
    )


def test_onboarding_md_command_sequence_matches_install() -> None:
    """ONBOARDING.md and INSTALL.md must reference the same deploy scripts.

    Both docs walk the operator through the same provisioning flow; if they
    drift, the operator gets contradictory instructions depending on which
    doc they read first.
    """
    install = _read(INSTALL)
    onboarding = _read(ONBOARDING)

    install_missing = [s for s in DEPLOY_SCRIPTS if s not in install]
    onboarding_missing = [s for s in DEPLOY_SCRIPTS if s not in onboarding]

    assert not install_missing, (
        f"INSTALL.md does not reference these scripts: {install_missing}"
    )
    assert not onboarding_missing, (
        f"ONBOARDING.md does not reference these scripts: {onboarding_missing}"
    )

    # Both docs should also mention the per-key set-secret command for at
    # least one of the required keys (the canonical first one).
    canonical_key = REQUIRED_SECRET_KEYS[0]
    assert canonical_key in install, (
        f"INSTALL.md must show the operator-set-secret.sh command with at "
        f"least {canonical_key}"
    )
    assert canonical_key in onboarding, (
        f"ONBOARDING.md must show the operator-set-secret.sh command with at "
        f"least {canonical_key}"
    )


def test_security_md_lists_all_sops_recipients() -> None:
    """SECURITY.md must describe both SOPS recipients in the trust model.

    The two-recipient model (operator master + per-tenant box) is the core of
    the offboarding handoff design. Losing either reference in the doc means
    the trust model is no longer self-explanatory to a new operator.
    """
    content = _read(SECURITY).lower()

    assert "operator" in content and "master" in content, (
        "SECURITY.md must mention the 'operator master' age key (root of trust)"
    )
    assert "tenant" in content and ("box" in content or "per-tenant" in content), (
        "SECURITY.md must mention the per-tenant box age key"
    )
    assert "recipient" in content, (
        "SECURITY.md must explain the SOPS recipient model (the word 'recipient' "
        "should appear at least once)"
    )
    # Both age private key paths from SPEC-006 / SPEC-008 should be cited.
    assert "/etc/age/key.txt" in _read(SECURITY), (
        "SECURITY.md must reference the on-box age private key path "
        "(/etc/age/key.txt) so operators can find it during incident response"
    )


def test_architecture_md_links_to_specs() -> None:
    """ARCHITECTURE.md must link to SPEC-* docs in each major section.

    The architecture doc orients; the SPEC docs detail. Each major section
    should give the reader a way to dive deeper. We assert that every level-3
    heading (### Section name) has at least one SPEC-* link in its body.
    """
    content = _read(ARCHITECTURE)

    # Find every ### section. A section's body is everything until the next
    # ### or the end of the file. We exclude top-level (#, ##) sections.
    sections = re.split(r"^### ", content, flags=re.MULTILINE)
    # First chunk is the preamble before any ### (skip it).
    section_bodies = sections[1:]

    assert section_bodies, (
        "ARCHITECTURE.md must have at least one ### subsection — the layers"
    )

    spec_link_pattern = re.compile(r"\[SPEC-\d+\]\(\.\./specs/SPEC-\d+[^\)]*\)")

    sections_without_spec_link = []
    for body in section_bodies:
        # The first line of `body` is the section heading; the rest is content.
        heading = body.split("\n", 1)[0].strip()
        if not spec_link_pattern.search(body):
            sections_without_spec_link.append(heading)

    assert not sections_without_spec_link, (
        f"ARCHITECTURE.md sections missing a SPEC-XXX link: "
        f"{sections_without_spec_link}. Each ### section should reference at "
        f"least one SPEC for deeper reading."
    )


def test_readme_lists_test_count() -> None:
    """README.md must include a numeric test count in the status section.

    Operator updates this manually when the count changes (per SPEC-019). We
    only assert presence of an X / Y pattern, not a specific number — that
    way bumping the count doesn't fail the test, but removing the badge does.
    """
    content = _read(README)

    # Match patterns like "204 / 204", "204/204", "204 of 204".
    pattern = re.compile(r"\b(\d{2,4})\s*[/]\s*(\d{2,4})\b")
    match = pattern.search(content)

    assert match is not None, (
        "README.md must include a test-count badge in the form 'N / M' "
        "(e.g. '204 / 204 tests passing'). Update via SPEC-019 §README.md."
    )

    passing, total = int(match.group(1)), int(match.group(2))
    assert passing == total, (
        f"README.md test count shows {passing} / {total} — passing must "
        f"equal total (or update the badge after fixing the failing tests)"
    )
    assert total >= 100, (
        f"README.md test count {total} looks suspicious (we should have "
        f"at least 100 tests). Possible accidental edit?"
    )
