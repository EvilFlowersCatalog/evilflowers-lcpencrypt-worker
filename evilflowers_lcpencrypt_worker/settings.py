"""What the worker is configured with: the kit's `RuntimeSettings` plus lcpencrypt's own fields (worker #8).

A *catalog job* in the kit's terms (evilflowers-worker `docs/architecture.md` §2): no catalog URL and no API key in
the pod. The outcome still reaches the catalog through the task's `report` URL (`report.py`); moving that to an
outcome file is a catalog decision first (issue #8, "Out").

The variable names are the ones the k8s role already sets: `BROKER` (the kit's `REDIS_URL` is accepted too),
`STORAGE_PATH`, `BROKER_VISIBILITY_TIMEOUT`, `READIUM_LCPENCRYPT_BIN`, `MANIFEST_SIDECAR`, `LOG_FORMAT`.
"""

from typing import Literal

from evilflowers_worker.settings import RuntimeSettings
from pydantic import AliasChoices, Field

QUEUE = "evilflowers_lcpencrypt_worker"


# The kit ships no py.typed: its base is Any to mypy.
class Settings(RuntimeSettings):  # type: ignore[misc]
    #: The broker (dev: db 7 of the shared Redis). `BROKER` first: the role and Compose set it.
    redis_url: str = Field("redis://localhost:6379/7", validation_alias=AliasChoices("BROKER", "REDIS_URL"))
    #: The catalog routes `evilflowers_lcpencrypt_worker.*` here; the name is a contract with it.
    queue: str = Field(QUEUE, validation_alias=AliasChoices("LCPENCRYPT_QUEUE", "QUEUE"))

    #: SIGTERM: the run in hand gets this long, then it is cancelled and requeued at once. The pod's
    #: `terminationGracePeriodSeconds` is the default 30 s, so grace + cancel (25 s) must stay under it, or the
    #: kubelet's SIGKILL lands first and the message only comes back after the visibility timeout.
    worker_soft_shutdown_timeout: float = Field(15.0, ge=0)
    worker_cancel_timeout: float = Field(10.0, ge=0)

    #: `text` outside the image (a terminal); the image sets `json`.
    log_format: Literal["json", "text"] = "text"

    #: The lcpencrypt binary.
    readium_lcpencrypt_bin: str = "lcpencrypt"
    #: `false` turns off `{filename}.manifest.json` next to the package.
    manifest_sidecar: bool = True


def settings() -> Settings:
    """The settings as the environment has them now: cheap, and a test's `monkeypatch.setenv` takes effect."""
    return Settings()


__all__ = ["QUEUE", "Settings", "settings"]
