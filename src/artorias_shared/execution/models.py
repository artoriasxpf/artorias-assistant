from pathlib import PurePosixPath
from typing import Literal
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator


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


MAX_INLINE_CODE_BYTES = 64 * 1024


class PythonRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    path: str | None = Field(default=None, min_length=1, description='Workspace path to a Python script. Provide exactly one of path or code.')
    code: str | None = Field(default=None, min_length=1, max_length=MAX_INLINE_CODE_BYTES,
                             description='Inline Python source, up to 64 KiB in UTF-8. Provide exactly one of path or code.')
    args: list[str] = Field(default_factory=list, max_length=100)
    mode: Literal['isolated', 'workspace_copy', 'workspace_ro', 'workspace_rw'] = 'isolated'
    inputs: list[InputResource] = Field(default_factory=list, max_length=100)
    output: OutputPolicy = Field(default_factory=OutputPolicy)
    working_directory: str | None = None
    code_directory: str | None = None
    allow_delete: bool = False
    execution_hint: Literal['auto','device','cloud'] = 'auto'
    environment: Literal['python-default'] = 'python-default'
    dependencies: list[str] = Field(default_factory=list, max_length=32)
    timeout_seconds: int = Field(default=120, ge=1, le=300)

    @model_validator(mode='after')
    def validate_entrypoint(self):
        if (self.path is None) == (self.code is None):
            raise ValueError('请选择 path 或 code，且只能提供一个')
        return self

    @field_validator('code')
    @classmethod
    def validate_code(cls, code):
        if code is not None and (not code.strip() or '\x00' in code or len(code.encode()) > MAX_INLINE_CODE_BYTES):
            raise ValueError('code 需要非空 Python 源码，最多 64 KiB UTF-8')
        return code

    @field_validator('args')
    @classmethod
    def validate_args(cls, args):
        if any('\x00' in a for a in args) or sum(map(len,args))>16000:raise ValueError('参数过长或非法')
        return args


class CommandRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    argv: list[str] = Field(min_length=1, max_length=100)
    mode: Literal['isolated', 'workspace_ro', 'workspace_rw'] = 'workspace_ro'
    working_directory: str | None = None
    timeout_seconds: int = Field(default=120, ge=1, le=300)
    inputs: list[InputResource] = Field(default_factory=list, max_length=100)

    @model_validator(mode='after')
    def validate_command(self):
        if any('\x00' in a for a in self.argv) or sum(len(a.encode()) for a in self.argv) > 16384:
            raise ValueError('非法或过长的命令参数')
        if self.mode == 'isolated' and self.working_directory:
            raise ValueError('isolated 使用临时目录')
        if self.mode != 'isolated' and self.inputs:
            raise ValueError('就地执行不接受 inputs')
        return self
