from pathlib import PurePosixPath
from typing import Literal
from pydantic import BaseModel, Field, ConfigDict, field_validator


def safe_relative(value: str) -> str:
    p = PurePosixPath(value)
    if not value or value.startswith('/') or any(x in value for x in ('\\', '\x00', ':')) or '..' in p.parts or str(p)=='.':
        raise ValueError('需要无越界的相对文件路径')
    return str(p)


class InputResource(BaseModel):
    model_config = ConfigDict(extra='forbid')
    resource: str
    mount_path: str
    directory: bool = False

    @field_validator('mount_path')
    @classmethod
    def validate_path(cls, value):return safe_relative(value)


class OutputPolicy(BaseModel):
    model_config = ConfigDict(extra='forbid')
    target: str | None = None
    publish: Literal['auto_if_authorized', 'stage_only'] = 'auto_if_authorized'
    overwrite: Literal['deny', 'if_unchanged'] = 'deny'
    include: list[str] = Field(default_factory=lambda:['*'], max_length=32)
    expected_files: list[str] = Field(default_factory=list, max_length=500)

    @field_validator('expected_files')
    @classmethod
    def validate_files(cls, values):return [safe_relative(v) for v in values]


class PythonRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    path: str
    args: list[str] = Field(default_factory=list, max_length=100)
    mode: Literal['isolated', 'workspace_copy'] = 'isolated'
    inputs: list[InputResource] = Field(default_factory=list, max_length=100)
    output: OutputPolicy = Field(default_factory=OutputPolicy)
    working_directory: str | None = None
    code_directory: str | None = None
    allow_delete: bool = False
    execution_hint: Literal['auto','device','cloud'] = 'auto'
    environment: Literal['python-default'] = 'python-default'
    timeout_seconds: int = Field(default=120, ge=1, le=300)

    @field_validator('args')
    @classmethod
    def validate_args(cls, args):
        if any('\x00' in a for a in args) or sum(map(len,args))>16000:raise ValueError('参数过长或非法')
        return args
