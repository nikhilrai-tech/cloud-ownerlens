"""Run gcloud/az read-only. Same promise as guard.py for AWS: anything that is not list/show/describe is refused."""
from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any, Callable, Optional

ALLOWED_VERBS = {"list", "show", "describe"}
DENIED_WORDS = {"delete", "create", "update", "start", "stop", "deallocate", "restart", "set", "add", "remove",
                "attach", "detach", "resize", "reset", "tag", "untag", "apply", "import", "move", "reimage",
                "redeploy", "capture", "generalize", "login", "revoke", "deploy", "run", "ssh", "scp"}
PROGRAMS = {"gcloud", "az"}


class ReadOnlyCliViolation(RuntimeError):
    pass


class CliMissing(RuntimeError):
    pass


def check_read_only(cmd: list[str]) -> None:
    # flags count too: `--set tags.a=b` is a write even though the command word is `show`
    words = {w.lstrip("-").split("=", 1)[0] for w in cmd[1:]}
    if not cmd or cmd[0] not in PROGRAMS:
        raise ReadOnlyCliViolation(f"only {sorted(PROGRAMS)} may be called, got {cmd[:1]}")
    if not (words & ALLOWED_VERBS) or (words & DENIED_WORDS):
        raise ReadOnlyCliViolation(f"ownerlens is read-only; refusing to run: {' '.join(cmd)}")


def run_json(cmd: list[str], runner: Optional[Callable[..., Any]] = None) -> Any:
    check_read_only(cmd)
    if runner is None:
        if not shutil.which(cmd[0]):
            raise CliMissing(f"`{cmd[0]}` is not installed or not on PATH")
        runner = subprocess.run
    proc = runner(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {proc.stderr.strip()[:300]}")
    return json.loads(proc.stdout or "[]")
