from typing import Literal

OpenAIModelType = Literal["gpt-3.5-turbo", "gpt-4.0-turbo"]
ClaudeAIModelType = Literal[
    "claude-3-opus-20240229",
    "claude-3-sonnet-20240229",
    "claude-3-haiku-20240307",
    "claude-3-5-sonnet-20240620",
]
ModelType = OpenAIModelType | ClaudeAIModelType
