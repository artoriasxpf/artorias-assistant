"""Wire format shared by the runtime and workspace owner."""
import hashlib
import json
from pathlib import PurePosixPath
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import PythonRequest, CommandRequest


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class WorkspaceJob(BaseModel):
    model_config = ConfigDict(extra='forbid')
    execution_id: str = Field(pattern=r'^exec_[a-f0-9]{40}$')
    workspace_id: str
    location_id: str
    kind: Literal['device_fs', 'cloud_storage']
    request: CommandRequest | PythonRequest

    @model_validator(mode='after')
    def validate_scope(self):
        req = self.request
        if isinstance(req, PythonRequest):
            if req.mode in ('workspace_ro','workspace_rw') and (
                    req.inputs or req.code_directory or req.allow_delete or req.output.model_fields_set):
                raise ValueError('就地执行不能使用副本或产物参数')
            location = 'device' if self.kind == 'device_fs' else 'cloud'
            if req.execution_hint not in ('auto', location):
                raise ValueError('执行位置与 workspace 不符')
        for value in (req.working_directory, getattr(req, 'path', None)):
            if value is not None and (not value or PurePosixPath(value).is_absolute()
                                      or '..' in PurePosixPath(value).parts or '\\' in value or '\x00' in value):
                raise ValueError('执行路径必须是 workspace 内相对路径')
        return self
