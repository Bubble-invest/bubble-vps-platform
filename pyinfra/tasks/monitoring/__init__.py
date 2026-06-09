from .dashboard import apply as dashboard
from .wiki_compile_alerts import apply as wiki_compile_alerts
from .restic_backup import apply as restic_backup
from .cache_sync import apply as cache_sync
from .secrets_sweep import apply as secrets_sweep
from .transcript_leak_scan import apply as transcript_leak_scan

__all__ = [
    "dashboard",
    "wiki_compile_alerts",
    "restic_backup",
    "cache_sync",
    "secrets_sweep",
    "transcript_leak_scan",
]
