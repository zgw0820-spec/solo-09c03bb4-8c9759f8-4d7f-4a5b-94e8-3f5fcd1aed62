"""Request/response schemas for the build API."""
from __future__ import annotations

from pydantic import BaseModel, Field

from .builder import MAX_FILE_BYTES, MAX_FILES

_EXAMPLE_MAIN = (
    '#include <stdio.h>\n'
    '#include "util.h"\n'
    'int main(void) {\n'
    '    printf("answer=%d\\n", util_answer());\n'
    '    return 0;\n'
    '}\n'
)
_EXAMPLE_UTIL_C = (
    '#include "util.h"\n'
    'int util_answer(void) { return UTIL_ANSWER; }\n'
)
_EXAMPLE_UTIL_H = (
    '#ifndef UTIL_H\n'
    '#define UTIL_H\n'
    '#define UTIL_ANSWER 42\n'
    'int util_answer(void);\n'
    '#endif\n'
)


class SourceFile(BaseModel):
    path: str = Field(
        min_length=1, max_length=255,
        description="Project-relative POSIX path, e.g. src/main.c or include/util.h",
        examples=["main.c"],
    )
    content: str = Field(
        max_length=MAX_FILE_BYTES,
        description="Full text content of the file",
        examples=[_EXAMPLE_MAIN],
    )


class BuildRequest(BaseModel):
    files: list[SourceFile] = Field(
        min_length=1, max_length=MAX_FILES,
        description="All .c sources and .h headers of the project, by relative path",
    )
    output: str = Field(
        default="app", pattern=r"^[A-Za-z0-9_.-]{1,64}$",
        description="Download filename for the built executable",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "output": "app",
                    "files": [
                        {"path": "main.c", "content": _EXAMPLE_MAIN},
                        {"path": "util.c", "content": _EXAMPLE_UTIL_C},
                        {"path": "util.h", "content": _EXAMPLE_UTIL_H},
                    ],
                }
            ]
        }
    }


class ErrorDetail(BaseModel):
    stage: str = Field(description="validation | compile | link")
    message: str
    unit: str | None = Field(default=None, description="Source file that failed, if any")
    diagnostics: str = Field(description="GCC stderr or validation message")


class ErrorResponse(BaseModel):
    detail: ErrorDetail
