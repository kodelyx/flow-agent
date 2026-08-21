#!/usr/bin/env python3
"""Pydantic request models for the Flow Agent API server."""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


# OpenAI Request/Response Models
class ImageGenerationRequest(BaseModel):
    prompt: str = Field(..., description="The prompt to generate images from")
    model: str = Field("gem_pix_2", description="Image model name (default: gem_pix_2/pro)")
    n: int = Field(1, ge=1, le=20, description="Number of images to generate (1-20)")
    size: str = Field("1024x1024", description="Image dimensions (e.g. 1024x1024, 1024x1792, etc.)")
    response_format: str = Field("url", description="The format in which the generated images are returned (url or b64_json)")
    user: Optional[str] = None
    image_base64: Optional[str] = Field(None, description="Optional base64 reference image for image-to-image")
    ref_media_ids: Optional[List[str]] = Field(None, description="Optional reference image media IDs (up to 10)")
    seed: Optional[int] = Field(None, ge=0, le=4294967295, description="Optional explicit generation seed")


class VideoGenerationRequest(BaseModel):
    prompt: str = Field(..., description="The prompt to generate videos from")
    aspect: str = Field("portrait", description="Video aspect ratio (portrait or landscape)")
    n: int = Field(1, ge=1, le=20, description="Number of videos to generate (1-20)")
    duration: int = Field(8, description="Duration in seconds (e.g. 4, 6, 8, 10)")
    image_base64: Optional[str] = Field(None, description="Optional base64 start image for image-to-video")
    ref_media_ids: Optional[List[str]] = Field(None, description="Optional reference image media IDs (up to 10)")
    start_media_id: Optional[str] = Field(None, description="Optional pre-uploaded start image or video media ID")
    end_media_id: Optional[str] = Field(None, description="Optional pre-uploaded end image media ID (requires start_media_id or image_base64)")
    is_video: Optional[bool] = Field(False, description="True if the pre-uploaded reference is a video")
    seed: Optional[int] = Field(None, ge=0, le=4294967295, description="Optional explicit generation seed")
    video_model: Optional[str] = Field(None, description="Optional Flow videoModelKey override")


class MusicGenerationRequest(BaseModel):
    prompt: str = Field(..., description="The prompt describing the music, mood, genre, or instruments")
    duration: int = Field(30, description="Duration in seconds (e.g. 10, 30, 50, 70)")
    loop: bool = Field(False, description="Whether to generate a seamless loop")
    n: int = Field(1, ge=1, le=4, description="Number of music tracks to generate (1-4)")
    model: Optional[str] = Field(None, description="Optional music model/tool name (e.g. MUSIC_FX)")
    seed: Optional[int] = Field(None, ge=0, le=4294967295, description="Optional explicit generation seed")
    response_format: str = Field("url", description="Return format: url or b64_json")
    user: Optional[str] = None


class GeneratedMedia(BaseModel):
    url: Optional[str] = None
    media_id: Optional[str] = None
    warning: Optional[str] = None


class VideoGenerationResult(BaseModel):
    """Stable response shape returned by video submission and polling."""

    job_id: str
    status: Literal["processing", "succeeded", "failed"]
    created: int
    data: List[GeneratedMedia] = Field(default_factory=list)
    note: Optional[str] = None
    error: Optional[Dict[str, Any]] = None


# Chat completions spec support for custom IDE models
class ChatMessage(BaseModel):
    role: str
    content: str

class ChatCompletionRequest(BaseModel):
    model: str = "flow-agent"
    messages: List[ChatMessage]
    temperature: Optional[float] = 1.0
    stream: Optional[bool] = False


class UploadRequest(BaseModel):
    image_base64: str = Field(..., description="Base64 encoded image or video data")
