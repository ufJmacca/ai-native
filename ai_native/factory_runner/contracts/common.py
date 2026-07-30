from __future__ import annotations

from datetime import datetime, timezone
import posixpath
import re
from typing import Annotated, Any, Literal, Never, TypeVar

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    WithJsonSchema,
)


PROTOCOL_V1 = "factory-runner-protocol/v1"
MAX_SAFE_INTEGER = 9_007_199_254_740_991
SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"
GIT_COMMIT_PATTERN = r"^[0-9a-f]{40}$"
UTC_TIMESTAMP_PATTERN = (
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z$"
)
ENVIRONMENT_KEY_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"
MEDIA_TYPE_PATTERN = (
    r"^[a-z0-9][a-z0-9!#$&^_.+-]*/"
    r"[a-z0-9][a-z0-9!#$&^_.+-]*$"
)
PROFILE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$"
SEMANTIC_VERSION_PATTERN = (
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)$"
)
_OPAQUE_CONTROL_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
_WILDCARD_PATTERN = re.compile(r"[*?[]")


class StrictContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        serialize_by_alias=True,
        str_strip_whitespace=False,
        validate_by_alias=True,
        validate_by_name=True,
        validate_default=True,
    )


class FrozenDict(dict[str, Any]):
    """A JSON-serialisable mapping that cannot change after validation."""

    @staticmethod
    def _immutable(*args: Any, **kwargs: Any) -> Never:
        del args, kwargs
        raise TypeError("durable contract mappings are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable

    def __copy__(self) -> FrozenDict:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> FrozenDict:
        del memo
        return self


def freeze_json_value(value: Any) -> Any:
    """Recursively detach and freeze a previously validated JSON value."""

    if isinstance(value, dict):
        return FrozenDict(
            {str(key): freeze_json_value(item) for key, item in value.items()}
        )
    if isinstance(value, list | tuple):
        return tuple(freeze_json_value(item) for item in value)
    return value


def freeze_mapping(value: dict[str, Any]) -> FrozenDict:
    frozen = freeze_json_value(value)
    if not isinstance(frozen, FrozenDict):
        raise TypeError("expected a JSON object")
    return frozen


def bounded_json_object_schema(
    schema: dict[str, Any],
    *,
    field_name: str,
    definition_prefix: str,
    max_depth: int,
    max_string_length: int | None = None,
    prohibited_keys: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Add a recursive, RFC-8785-compatible object schema to a model schema."""

    definitions = schema.setdefault("$defs", {})
    property_name_rule: dict[str, Any] = {}
    if prohibited_keys:
        property_name_rule = {
            "propertyNames": {"not": {"enum": sorted(prohibited_keys)}}
        }

    def scalar_branches() -> list[dict[str, Any]]:
        string_schema: dict[str, Any] = {"type": "string"}
        if max_string_length is not None:
            string_schema["maxLength"] = max_string_length
        return [
            {"type": "null"},
            {"type": "boolean"},
            {
                "type": "integer",
                "minimum": -MAX_SAFE_INTEGER,
                "maximum": MAX_SAFE_INTEGER,
            },
            {
                "type": "number",
                "minimum": -MAX_SAFE_INTEGER,
                "maximum": MAX_SAFE_INTEGER,
            },
            string_schema,
        ]

    for depth in range(1, max_depth + 1):
        branches = scalar_branches()
        if depth == max_depth:
            branches.extend(
                (
                    {"type": "array", "maxItems": 0},
                    {
                        "type": "object",
                        "maxProperties": 0,
                        **property_name_rule,
                    },
                )
            )
        else:
            child_ref = f"#/$defs/{definition_prefix}{depth + 1}"
            branches.extend(
                (
                    {"type": "array", "items": {"$ref": child_ref}},
                    {
                        "type": "object",
                        "additionalProperties": {"$ref": child_ref},
                        **property_name_rule,
                    },
                )
            )
        definitions[f"{definition_prefix}{depth}"] = {"anyOf": branches}

    if max_depth == 0:
        field_schema: dict[str, Any] = {
            "type": "object",
            "maxProperties": 0,
            **property_name_rule,
        }
    else:
        field_schema = {
            "type": "object",
            "additionalProperties": {"$ref": f"#/$defs/{definition_prefix}1"},
            **property_name_rule,
        }
    schema["properties"][field_name] = field_schema
    return schema


def _validate_opaque_id(value: str) -> str:
    if _OPAQUE_CONTROL_PATTERN.search(value):
        raise ValueError("opaque identifiers may not contain control characters")
    return value


def _validate_schema_version(value: object) -> object:
    if type(value) is not int or value != 1:
        raise ValueError("schema_version must be the integer 1")
    return value


def _validate_utc_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ValueError("timestamp must be a valid UTC RFC 3339 value") from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("timestamp must use UTC")
    return value


def parse_utc_timestamp(value: str) -> datetime:
    _validate_utc_timestamp(value)
    return datetime.fromisoformat(value.removesuffix("Z") + "+00:00")


def utc_timestamp_sort_key(value: str) -> tuple[datetime, int]:
    """Return an exact ordering key without truncating nanoseconds."""

    _validate_utc_timestamp(value)
    without_suffix = value.removesuffix("Z")
    whole_seconds, separator, fraction = without_suffix.partition(".")
    parsed = datetime.fromisoformat(whole_seconds + "+00:00")
    nanoseconds = int(fraction.ljust(9, "0")) if separator else 0
    return parsed, nanoseconds


def _path_parts_are_safe(path: str, *, allow_git: bool) -> None:
    if "\x00" in path or "\\" in path:
        raise ValueError("path must use POSIX separators and contain no NUL")
    if path.startswith("/") or path.endswith("/") or "//" in path:
        raise ValueError("repository path must be relative and normalised")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("repository path must not contain dot segments")
    if not allow_git and ".git" in parts:
        raise ValueError("repository path must not address .git internals")


def _validate_repository_path(value: str) -> str:
    _path_parts_are_safe(value, allow_git=False)
    if _WILDCARD_PATTERN.search(value):
        raise ValueError("artifact and repository paths may not contain wildcards")
    if posixpath.normpath(value) != value:
        raise ValueError("repository path must already be normalised")
    return value


def _validate_policy_path(value: str) -> str:
    if value == "**":
        return value
    subtree = value.endswith("/**")
    path = value[:-3] if subtree else value
    _path_parts_are_safe(path, allow_git=False)
    if _WILDCARD_PATTERN.search(path):
        raise ValueError("only a terminal /** subtree wildcard is supported")
    if posixpath.normpath(path) != path:
        raise ValueError("policy path must already be normalised")
    return value


def _validate_prohibited_path(value: str) -> str:
    if value == "**":
        return value
    subtree = value.endswith("/**")
    path = value[:-3] if subtree else value
    _path_parts_are_safe(path, allow_git=True)
    if _WILDCARD_PATTERN.search(path):
        raise ValueError("only a terminal /** subtree wildcard is supported")
    if posixpath.normpath(path) != path:
        raise ValueError("policy path must already be normalised")
    return value


def _validate_absolute_path(value: str) -> str:
    if "\x00" in value or "\\" in value:
        raise ValueError("absolute path must use POSIX separators and contain no NUL")
    if not value.startswith("/") or value == "/":
        raise ValueError("path must be an absolute non-root POSIX path")
    if value.endswith("/") or "//" in value:
        raise ValueError("absolute path must already be normalised")
    if any(part in {".", ".."} for part in value.split("/")[1:]):
        raise ValueError("absolute path must not contain dot segments")
    if posixpath.normpath(value) != value:
        raise ValueError("absolute path must already be normalised")
    return value


OpaqueId = Annotated[
    StrictStr,
    Field(min_length=1, max_length=512),
    AfterValidator(_validate_opaque_id),
]
NonEmptyString = Annotated[StrictStr, Field(min_length=1, max_length=16_384)]
Sha256Digest = Annotated[StrictStr, Field(pattern=SHA256_PATTERN)]
GitCommitSha = Annotated[StrictStr, Field(pattern=GIT_COMMIT_PATTERN)]
UtcTimestamp = Annotated[
    StrictStr,
    Field(
        pattern=UTC_TIMESTAMP_PATTERN,
        json_schema_extra={"format": "date-time"},
    ),
    AfterValidator(_validate_utc_timestamp),
]
_REPOSITORY_PATH_JSON_SCHEMA = {
    "type": "string",
    "minLength": 1,
    "maxLength": 4096,
    "pattern": (
        r"^(?!/)(?!.*//)(?!.*\\)(?!.*\u0000)"
        r"(?!.*(?:^|/)\.\.?($|/))(?!.*(?:^|/)\.git($|/))"
        r"(?!.*[?*\[])(?!.*\/$).+$"
    ),
}
_POLICY_PATH_JSON_SCHEMA = {
    "type": "string",
    "minLength": 1,
    "maxLength": 4096,
    "pattern": (
        r"^(?!/)(?!.*//)(?!.*\\)(?!.*\u0000)"
        r"(?!.*(?:^|/)\.\.?($|/))(?!.*(?:^|/)\.git($|/))"
        r"(?!.*[?\[])(?!.*\/$)(?:\*\*|[^*]+(?:/\*\*)?)$"
    ),
}
_PROHIBITED_PATH_JSON_SCHEMA = {
    "type": "string",
    "minLength": 1,
    "maxLength": 4096,
    "pattern": (
        r"^(?!/)(?!.*//)(?!.*\\)(?!.*\u0000)"
        r"(?!.*(?:^|/)\.\.?($|/))(?!.*[?\[])"
        r"(?!.*\/$)(?:\*\*|[^*]+(?:/\*\*)?)$"
    ),
}
_ABSOLUTE_PATH_JSON_SCHEMA = {
    "type": "string",
    "minLength": 2,
    "maxLength": 4096,
    "pattern": (
        r"^/(?!$)(?!.*//)(?!.*\\)(?!.*\u0000)"
        r"(?!.*(?:^|/)\.\.?($|/))(?!.*\/$).+$"
    ),
}
RepositoryPath = Annotated[
    StrictStr,
    AfterValidator(_validate_repository_path),
    WithJsonSchema(_REPOSITORY_PATH_JSON_SCHEMA),
]
PolicyPath = Annotated[
    StrictStr,
    AfterValidator(_validate_policy_path),
    WithJsonSchema(_POLICY_PATH_JSON_SCHEMA),
]
ProhibitedPath = Annotated[
    StrictStr,
    AfterValidator(_validate_prohibited_path),
    WithJsonSchema(_PROHIBITED_PATH_JSON_SCHEMA),
]
AbsolutePosixPath = Annotated[
    StrictStr,
    AfterValidator(_validate_absolute_path),
    WithJsonSchema(_ABSOLUTE_PATH_JSON_SCHEMA),
]
EnvironmentKey = Annotated[
    StrictStr,
    Field(pattern=ENVIRONMENT_KEY_PATTERN, max_length=256),
]
MediaType = Annotated[StrictStr, Field(pattern=MEDIA_TYPE_PATTERN, max_length=256)]
ProfileName = Annotated[StrictStr, Field(pattern=PROFILE_PATTERN)]
SemanticVersion = Annotated[
    StrictStr,
    Field(pattern=SEMANTIC_VERSION_PATTERN, max_length=128),
]
ByteSize = Annotated[StrictInt, Field(ge=0, le=MAX_SAFE_INTEGER)]
PositiveSeconds = Annotated[StrictInt, Field(gt=0, le=MAX_SAFE_INTEGER)]
NonNegativeSeconds = Annotated[
    StrictFloat | StrictInt,
    Field(ge=0, le=MAX_SAFE_INTEGER),
]
SafeInteger = Annotated[
    StrictInt,
    Field(ge=-MAX_SAFE_INTEGER, le=MAX_SAFE_INTEGER),
]
PositiveSequence = Annotated[
    StrictInt,
    Field(gt=0, le=MAX_SAFE_INTEGER),
]
SchemaVersion = Annotated[Literal[1], BeforeValidator(_validate_schema_version)]
FactoryStage = Literal[
    "intake",
    "recon",
    "plan",
    "architecture",
    "prd",
    "slice",
    "loop",
    "verify",
]


class RunIdentity(StrictContractModel):
    work_item_id: OpaqueId
    work_item_revision_id: OpaqueId
    delivery_phase_id: OpaqueId
    run_id: OpaqueId
    attempt_id: OpaqueId
    correlation_id: OpaqueId


class RepositoryIdentity(StrictContractModel):
    repository_id: OpaqueId
    display_name: NonEmptyString
    base_commit_sha: GitCommitSha


class DocumentEnvelope(StrictContractModel):
    protocol: Literal["factory-runner-protocol/v1"]
    schema_: StrictStr = Field(alias="schema")
    schema_version: SchemaVersion
    created_at: UtcTimestamp
    identity: RunIdentity
    repository: RepositoryIdentity


class ArtifactReference(StrictContractModel):
    path: RepositoryPath
    media_type: MediaType
    byte_size: ByteSize
    digest: Sha256Digest


ArtifactRef = ArtifactReference


class RunnerBuildIdentity(StrictContractModel):
    version: NonEmptyString
    image: NonEmptyString | None = None
    source_commit: GitCommitSha | None = None


T = TypeVar("T")


def require_unique(values: tuple[T, ...], field_name: str) -> tuple[T, ...]:
    try:
        unique_count = len(set(values))
    except TypeError:
        unique_count = len({repr(item) for item in values})
    if len(values) != unique_count:
        raise ValueError(f"{field_name} must not contain duplicates")
    return values


def ensure_started_before_finished(started_at: str, finished_at: str) -> None:
    if utc_timestamp_sort_key(finished_at) < utc_timestamp_sort_key(started_at):
        raise ValueError("finished_at must not precede started_at")


def contains_secret_reference(value: str) -> bool:
    lowered = value.casefold()
    return "://" in value or any(
        marker in lowered
        for marker in (
            "api_key",
            "apikey",
            "authorization",
            "bearer ",
            "password",
            "secret",
            "token=",
        )
    )


JsonObject = dict[str, Any]


__all__ = [
    "AbsolutePosixPath",
    "ArtifactRef",
    "ArtifactReference",
    "ByteSize",
    "DocumentEnvelope",
    "EnvironmentKey",
    "FactoryStage",
    "FrozenDict",
    "GitCommitSha",
    "JsonObject",
    "MediaType",
    "MAX_SAFE_INTEGER",
    "NonEmptyString",
    "NonNegativeSeconds",
    "OpaqueId",
    "PROTOCOL_V1",
    "PolicyPath",
    "PositiveSeconds",
    "PositiveSequence",
    "ProfileName",
    "ProhibitedPath",
    "RepositoryIdentity",
    "RepositoryPath",
    "RunIdentity",
    "RunnerBuildIdentity",
    "SHA256_PATTERN",
    "SchemaVersion",
    "SafeInteger",
    "SemanticVersion",
    "Sha256Digest",
    "StrictBool",
    "StrictContractModel",
    "StrictInt",
    "StrictStr",
    "UtcTimestamp",
    "bounded_json_object_schema",
    "contains_secret_reference",
    "ensure_started_before_finished",
    "freeze_json_value",
    "freeze_mapping",
    "parse_utc_timestamp",
    "require_unique",
    "utc_timestamp_sort_key",
]
