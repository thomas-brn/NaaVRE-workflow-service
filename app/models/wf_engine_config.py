import logging
from typing import Optional

from pydantic import BaseModel

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


class WfEngineConfig(BaseModel):
    name: str
    api_endpoint: str
    access_token: str
    service_account: Optional[str] | None = None
    namespace: str
    workdir_storage_size: str = '1Gi'
    extraVolumeMounts: Optional[list[dict]] | None = None
    # Extra environment variables (Argo `env` entries: {name, value} or
    # {name, valueFrom}) injected into every cell container for this virtual
    # lab. Added to support the in-lab visualisation feature, whose fdo-writer
    # cells and any storage-mounting cell may need env-based configuration
    # (e.g. storage endpoint) that isn't a per-cell secret.
    extraEnv: Optional[list[dict]] | None = None
    secrets_creator_api_endpoint: Optional[str] | None = None
    secrets_creator_api_token: Optional[str] | None = None
    default_max_branches: Optional[int] = None
