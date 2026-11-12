"""Overtone: a headless notebook agent built on Microsoft Agent Framework.

    from overtone import create_engine, UrlInput, ArtifactRequest

    engine = create_engine()
    result = await engine.one_shot([UrlInput("https://example.com/report")])
"""

__version__ = "0.2.0"

from .agents import ModelRuntime, create_runtime, producer, researcher, voice_researcher  # noqa: E402
from .engine import Engine, OneShotResult, create_engine  # noqa: E402
from .errors import OvertoneError  # noqa: E402
from .grounding import SourceDoc  # noqa: E402
from .live import LiveVoice  # noqa: E402
from .mock import MockChatClient  # noqa: E402
from .models import (  # noqa: E402
    Artifact,
    ArtifactRequest,
    AudioHost,
    AudioOptions,
    AudioScript,
    Capabilities,
    ChatMessage,
    Citation,
    FileInput,
    LiveDelegateRequest,
    LiveSessionRequest,
    Notebook,
    Source,
    Storage,
    TextInput,
    TtsConfig,
    TtsProvider,
    UrlInput,
)
from .storage import FileStorage, MemoryStorage  # noqa: E402
from .workflows import GenerationJob, GenerationResult, build_audio_workflow, build_document_workflow  # noqa: E402

__all__ = [
    "Artifact", "ArtifactRequest", "AudioHost", "AudioOptions", "AudioScript", "Capabilities", "ChatMessage",
    "Citation", "Engine", "FileInput", "FileStorage", "GenerationJob", "GenerationResult", "LiveDelegateRequest",
    "LiveSessionRequest", "LiveVoice", "MemoryStorage", "MockChatClient", "ModelRuntime", "Notebook",
    "OneShotResult", "OvertoneError", "Source", "SourceDoc", "Storage", "TextInput", "TtsConfig", "TtsProvider",
    "UrlInput", "build_audio_workflow", "build_document_workflow", "create_engine", "create_runtime", "producer",
    "researcher", "voice_researcher", "__version__",
]
