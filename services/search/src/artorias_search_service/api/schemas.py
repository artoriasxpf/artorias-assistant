from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class CollectionRequest(StrictModel):
    collection: str = Field(min_length=1, max_length=200)
    projection_version: str = Field(min_length=1, max_length=200)


class SearchRequest(CollectionRequest):
    partition: str = Field(min_length=1, max_length=500)
    query: str = Field(max_length=30000)
    mode: Literal['any', 'all'] = 'any'
    limit: int = Field(default=20, ge=1, le=100)


class IndexedDocument(StrictModel):
    id: str = Field(min_length=1, max_length=500)
    title: str = ''
    keywords: str = ''
    body: str = ''


class Entry(StrictModel):
    partition: str = Field(min_length=1, max_length=500)
    document: IndexedDocument


class Event(CollectionRequest):
    source_id: str = Field(min_length=1, max_length=200)
    seq: int = Field(ge=1)
    operation: Literal['snapshot', 'upsert', 'delete']
    entries: list[Entry] = Field(default_factory=list)


class HitResponse(StrictModel):
    id: str
    score: float


class SearchResponse(StrictModel):
    hits: list[HitResponse]


class ConsumerResponse(StrictModel):
    source_id: str
    last_seq: int


class StatusResponse(StrictModel):
    current: bool
    tokenizer_version: str
    consumer: ConsumerResponse | None


class EventResponse(StrictModel):
    ack: int
    duplicate: bool
