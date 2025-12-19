"""Pydantic models for search results and reranking"""

import uuid

from langchain.messages import AIMessage
from pydantic import BaseModel, Field, computed_field

from data_commons_search.config import settings

# OpenSearch result models


class SearchHitSrcCreator(BaseModel):
    creator_name: str = Field(..., alias="creatorName")


class SearchHitSrcSubject(BaseModel):
    subject: str
    lang: str | None = None
    subject_scheme: str | None = Field(None, alias="subjectScheme")
    schema_uri: str | None = Field(None, alias="schemaUri")
    value_uri: str | None = Field(None, alias="valueUri")
    classification_code: str | None = Field(None, alias="classificationCode")


class SearchHitSrcTitle(BaseModel):
    title: str
    lang: str | None = None


class SearchHitSrcDescription(BaseModel):
    description: str | None = None
    lang: str | None = None
    description_type: str | None = Field(None, alias="descriptionType")


class SearchHitSrcDate(BaseModel):
    date: str
    """Date in format yyyy-MM-dd"""
    date_type: str = Field(..., alias="dateType")
    """Type of date, e.g., Issued, Available, Updated, Submitted"""


class SearchHitSrc(BaseModel):
    """Source metadata for an OpenSearch result hit."""

    doi: str | None = None
    url: str | None = None
    harvest_url: str | None = Field(None, alias="_harvestUrl")
    repo: str | None = Field(None, alias="_repo")
    titles: list[SearchHitSrcTitle] = Field(default_factory=list)
    descriptions: list[SearchHitSrcDescription] = Field(default_factory=list)
    publication_year: str | None = Field(None, alias="publicationYear")
    dates: list[SearchHitSrcDate] | None = None
    subjects: list[SearchHitSrcSubject] | None = None
    creators: list[SearchHitSrcCreator] | None = None
    resource_type: str = Field("dataset", alias="resourceType")

    model_config = {"populate_by_name": True}


# https://github.com/EOSC-Data-Commons/metadata-warehouse/blob/main/src/config/opensearch_mapping.json
class ToolRegistryTool(BaseModel):
    """A tool from the tool registry."""

    tool_uri: str = Field(..., alias="toolURI")
    tool_label: str = Field(..., alias="toolLabel")
    tool_description: str = Field(..., alias="toolDescription")

    model_config = {"populate_by_name": True}


class SearchHit(BaseModel):
    """A single search result hit from OpenSearch, enriched with optional additional metadata."""

    id: str = Field(..., alias="_id")
    source: SearchHitSrc = Field(..., alias="_source")
    opensearch_score: float = Field(..., alias="_score")  # OpenSearch relevance score
    # Reranking score and file extensions
    score: float | None = None
    file_extensions: list[str] = Field(default_factory=list, alias="fileExtensions")
    relevant_tools: list[ToolRegistryTool] = Field(default_factory=list, alias="relevantTools")

    # Allow population by field name (useful when constructing instances programmatically)
    # and keep default alias handling so input with `_id`, `_source`, `_score` will map correctly
    model_config = {"populate_by_name": True}

    # Precompute a few field values for easier access
    @computed_field
    def title(self) -> str | None:
        """Get the first title, prioritizing English language titles."""
        titles = self.source.titles
        return next(
            (item.title for item in titles if item.lang == "en"),
            titles[0].title if titles else None,
        )

    @computed_field
    def description(self) -> str | None:
        """Get the first description, prioritizing English language descriptions."""
        descriptions = self.source.descriptions
        return next(
            (item.description for item in descriptions if item.lang == "en"),
            descriptions[0].description if descriptions else None,
        )

    @computed_field
    def creator(self) -> str | None:
        """Get the first creator name if available."""
        creators = self.source.creators
        return next((creator.creator_name for creator in creators), None) if creators else None

    @computed_field
    def publication_date(self) -> str | None:
        """Get publication date from dates with dateType `Issued`."""
        dates = self.source.dates
        if not dates:
            return None
        return next(
            (date.date for date in dates if date.date_type == "Issued"),
            None,
        )


class OpenSearchResults(BaseModel):
    """Search results from OpenSearch."""

    total_found: int
    hits: list[SearchHit]


# Final ranked search response model


class RankedSearchResponse(BaseModel):
    """Final response containing ranked search results and summary."""

    summary: str
    hits: list[SearchHit]


# Structured output models for reranking


class RankedHit(BaseModel):
    """A search result with relevance score from the reranking step."""

    url: str
    score: float


class RerankingOutput(BaseModel):
    """Structured output from the LLM reranking step."""

    summary: str
    hits: list[RankedHit]


# FileMetrix API response models


class FileMetrixExtensionsResponse(BaseModel):
    """Response model for the FileMetrix extensions endpoint."""

    extensions: list[str] = Field(default_factory=list)


class FileMetrixChecksum(BaseModel):
    """Checksum object nested in raw_metadata.checksum."""

    type: str
    value: str


class FileMetrixRawFileMetadata(BaseModel):
    """Raw metadata returned by the storage system for a file."""

    id: int
    persistent_id: str | None = Field(None, alias="persistentId")
    filename: str | None = None
    content_type: str | None = Field(None, alias="contentType")
    friendly_type: str | None = Field(None, alias="friendlyType")
    filesize: int | None = Field(None, alias="filesize")
    description: str | None = None
    storage_identifier: str | None = Field(None, alias="storageIdentifier")
    original_file_format: str | None = Field(None, alias="originalFileFormat")
    original_format_label: str | None = Field(None, alias="originalFormatLabel")
    original_file_size: int | None = Field(None, alias="originalFileSize")
    original_file_name: str | None = Field(None, alias="originalFileName")
    UNF: str | None = None
    root_data_file_id: int | None = Field(None, alias="rootDataFileId")
    checksum: FileMetrixChecksum | None = None
    tabular_data: bool | None = Field(None, alias="tabularData")
    creation_date: str | None = Field(None, alias="creationDate")
    publication_date: str | None = Field(None, alias="publicationDate")
    file_access_request: bool | None = Field(None, alias="fileAccessRequest")


class FileMetrixFileEntry(BaseModel):
    """A single file entry from the FileMetrix/files endpoint."""

    link: str
    name: str
    size: int | None = None
    hash: str | None = None
    hash_type: str | None = Field(None, alias="hash_type")
    raw_metadata: FileMetrixRawFileMetadata | None = None

    model_config = {"populate_by_name": True}


# # https://filemetrix.labs.dansdemo.nl/api/v1/10.17026%2FSS%2FR5XWCC
class FileMetrixFilesResponse(BaseModel):
    """Response model for the FileMetrix files endpoint.

    Expected input shape (example):
    {
      "files": [ { ... }, ... ]
    }
    """

    files: list[FileMetrixFileEntry] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


# Chat agent input models


class AgentMsg(BaseModel):
    role: str  # Literal["user", "assistant", "system", "tool"]
    content: str


# class AgentInput(RunAgentInput): https://docs.ag-ui.com/sdk/python/core/types#runagentinput
class AgentInput(BaseModel):
    messages: list[AgentMsg]
    model: str = settings.default_llm_model
    thread_id: str = str(uuid.uuid4())
    run_id: str = str(uuid.uuid4())
    # NOTE: additional fields from RunAgentInput can be added if needed
    # tools: list[Tool] = Field(default_factory=list)
    # context: list[Context] = Field(default_factory=list)
    # state: Any = None
    # forwarded_props: Any = None
    # messages: List[Message]


# # Response metadata from LangChain LLM calls


class TokenUsageMetadata(BaseModel):
    """Metadata about LLM usage, e.g., token counts."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    reasoning_tokens: int = 0

    def __iadd__(self, other: "TokenUsageMetadata") -> "TokenUsageMetadata":
        """In-place add other usage counts into this instance and return self."""
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens
        self.reasoning_tokens += other.reasoning_tokens
        return self


class LangChainResponseMetadata(BaseModel):
    """Metadata about a LangChain response, e.g. LLM usage."""

    token_usage: TokenUsageMetadata

    # # NOTE: needed to convert LiteLLM Usage object to dict if we use `ChatLiteLLM`
    # @field_validator("token_usage", mode="before")
    # @classmethod
    # def convert_usage_object(cls, v: Any) -> dict[str, Any]:
    #     """Convert LiteLLM Usage object to dict if needed."""
    #     if isinstance(v, dict):
    #         return v
    #     if hasattr(v, "model_dump"):
    #         return v.model_dump()
    #     if hasattr(v, "__dict__"):
    #         return v.__dict__
    #     return v


class LangChainRerankingOutputMsg(BaseModel):
    """Structured output response for reranking from LangChain."""

    raw: AIMessage
    parsed: RerankingOutput
