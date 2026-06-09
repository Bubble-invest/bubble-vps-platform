"""sshd hardening sub-module.

Drops `/etc/ssh/sshd_config.d/00-bubble-hardening.conf` from a jinja2 template,
validates it with `sshd -t -f` BEFORE reloading, then reloads ssh only if the
file changed.

Idempotent: pyinfra's files.template hashes the rendered content vs the remote
file and is a no-op when they match. The validation+reload only run on change
(via the _if=op.did_change predicate).

Drift discovery (2026-05-08):
    The file on joris-cx33 is named 00-bubble-hardening.conf (NOT 99-bubble.conf
    as SPEC-005 §_sshd.py suggests). The 00- prefix is intentional: it loads
    BEFORE /etc/ssh/sshd_config.d/50-cloud-init.conf which Ubuntu's cloud-init
    writes with PasswordAuthentication yes. For the auth-mode directives the
    first match wins, so 00- locks them down before cloud-init's value is read.

    The file also includes KbdInteractiveAuthentication, ChallengeResponseAuthentication,
    and LoginGraceTime which the spec omits. We match reality to keep the
    dogfood clean. These are reasonable hardening additions that could be promoted
    to tenant.yaml in v2.
"""

from __future__ import annotations

from pathlib import Path

from pyinfra.operations import files, server, systemd


_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2] / "templates" / "sshd_99-bubble.conf.j2"
)
_REMOTE_PATH = "/etc/ssh/sshd_config.d/00-bubble-hardening.conf"


def apply(sshd_cfg) -> None:
    """Apply sshd hardening for the current host.

    Args:
        sshd_cfg: SshdConfig dataclass instance from tenant.yaml.

    The full security policy is captured in the template; sshd_cfg drives the
    three directives that vary per tenant: PermitRootLogin, PasswordAuthentication,
    MaxAuthTries.
    """
    # Render + write the drop-in. files.template returns an OperationMeta whose
    # `.did_change` method (bound, callable) is used as a predicate by `_if=`.
    template_op = files.template(
        name="hardening/sshd: write /etc/ssh/sshd_config.d/00-bubble-hardening.conf",
        src=str(_TEMPLATE_PATH),
        dest=_REMOTE_PATH,
        user="root",
        group="root",
        mode="644",
        # template variables (passed as **data)
        permit_root_login=sshd_cfg.permit_root_login,
        password_authentication=sshd_cfg.password_authentication,
        max_auth_tries=(
            sshd_cfg.max_auth_tries if sshd_cfg.max_auth_tries is not None else 3
        ),
    )

    # Validate the new config BEFORE asking sshd to reload. If sshd -t fails,
    # the deploy errors here and we never restart ssh. Manual recovery via
    # Hetzner web console only needed if sshd -t passes but reload still fails
    # (~never).
    server.shell(
        name="hardening/sshd: validate sshd config (sshd -t)",
        commands=[f"sshd -t -f {_REMOTE_PATH}"],
        _if=template_op.did_change,
    )

    # Reload ssh ONLY if the drop-in changed. On Ubuntu 24.04 the SSH unit is
    # socket-activated (ssh.socket starts ssh.service on connect). Reloading
    # ssh.service sends SIGHUP to running sshd processes so they re-read config.
    systemd.service(
        name="hardening/sshd: reload ssh.service if config changed",
        service="ssh.service",
        reloaded=True,
        _if=template_op.did_change,
    )
