#!/usr/bin/env python3
"""MCP tool schema definitions for Flow Agent.

Single source of truth for the MCP tool surface: both the SSE transport
(flow_server/mcp/sse.py) and the stdio server read their schemas from here.
"""


def get_mcp_tools_list():
    """Tool surface for MCP clients.

    Kept byte-for-byte in step with flow_server.mcp_server.handle_tools_list so
    that stdio and SSE clients see exactly the same tools and schemas.
    """
    return [
        {
            "name": "get_flow_status",
            "description": "Check Flow backend, Chrome extension connection, and Flow session-key health.",
            "inputSchema": {"type": "object", "properties": {}}
        },
        {
            "name": "get_flow_credits",
            "description": "Check the remaining credits / generations on the logged-in Google Flow account.",
            "inputSchema": {
                "type": "object",
                "properties": {}
            }
        },
        {
            "name": "list_flow_models",
            "description": "List image models available through the Flow backend.",
            "inputSchema": {"type": "object", "properties": {}}
        },
        {
            "name": "get_flow_history",
            "description": "List recently generated or uploaded Flow media.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}
                }
            }
        },
        {
            "name": "generate_flow_image",
            "description": "Generate 1-20 images using Google Flow, with model selection and local/media-ID references.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Text description of the image to generate"
                    },
                    "size": {
                        "type": "string",
                        "description": "Dimensions of the output image (default: '1280x720')",
                        "default": "1280x720"
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of image variations (1-20)",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 1
                    },
                    "ref_image_path": {
                        "type": "string",
                        "description": "Optional local file path to a reference image on the host for Image-to-Image"
                    },
                    "ref_image_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 10,
                        "description": "Optional local reference-image paths (up to 10)"
                    },
                    "ref_media_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 10,
                        "description": "Optional already-uploaded Flow media IDs"
                    },
                    "model": {
                        "type": "string",
                        "description": "Image model to use (harbor_seal/lite, narwhal/standard, gem_pix_2/pro)",
                        "default": "gem_pix_2"
                    }
                },
                "required": ["prompt"]
            }
        },
        {
            "name": "generate_flow_video",
            "description": "Generate 1-20 Flow videos with duration, aspect, start asset, reference-media, and delivery-resolution control (720p native, 1080p or 4K via Flow's upsampler).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Description of the motion to generate in the video"
                    },
                    "aspect": {
                        "type": "string",
                        "description": "Video aspect ratio: 'landscape' or 'portrait' (default: 'landscape')",
                        "enum": ["landscape", "portrait"],
                        "default": "landscape"
                    },
                    "start_image_path": {
                        "type": "string",
                        "description": "Optional local file path to a starting reference image on the host for Image-to-Video"
                    },
                    "duration": {"type": "integer", "enum": [4, 6, 8, 10], "default": 8},
                    "count": {"type": "integer", "minimum": 1, "maximum": 20, "default": 1},
                    "start_media_id": {"type": "string", "description": "Optional pre-uploaded start image media ID"},
                    "ref_media_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 10,
                        "description": "Optional Flow reference-media IDs for reference-to-video"
                    },
                    "resolution": {
                        "type": "string",
                        "enum": ["720p", "1080p", "4k"],
                        "default": "720p",
                        "description": "Delivery resolution. Flow generates at 720p; '1080p' (free) or '4k' (paid, higher tier) add Flow's upsampler pass and the high-resolution file is returned first."
                    }
                },
                "required": ["prompt"]
            }
        },
        {
            "name": "upsample_flow_video",
            "description": "Upsample an existing Flow video to 1080p or 4K — the same high-resolution pass behind the Flow UI's HD download. Accepts a media ID or a local video path already in Flow history.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "media_id": {"type": "string", "description": "Media ID of a finished Flow video, or a local video path/filename tracked in history"},
                    "resolution": {
                        "type": "string",
                        "enum": ["1080p", "4k"],
                        "default": "1080p",
                        "description": "Target resolution: '1080p' is free, '4k' costs credits and needs a higher Flow tier"
                    },
                    "aspect": {"type": "string", "enum": ["landscape", "portrait"], "default": "landscape", "description": "Aspect ratio of the source video"},
                    "seed": {"type": "integer", "minimum": 0, "maximum": 4294967295, "description": "Optional explicit upsampler seed"}
                },
                "required": ["media_id"]
            }
        },
        {
            "name": "upload_flow_media",
            "description": "Upload an image or video to Google Flow from a local path, a public URL, or base64 data, and return its media ID.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Absolute local image/video path"},
                    "image_url": {"type": "string", "description": "Optional public HTTP(S) URL of the image/video to fetch and upload"},
                    "image_base64": {"type": "string", "description": "Optional base64-encoded image/video payload"}
                }
            }
        },
        {
            "name": "download_media_from_url",
            "description": "Download an image/video from an HTTP(S) URL, including redirects and signed links; optionally upload it to Google Flow.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Direct or signed HTTP(S) image/video URL"},
                    "output_dir": {"type": "string", "description": "Optional local destination directory; defaults to FLOW_OUTPUT_DIR"},
                    "filename": {"type": "string", "description": "Optional output filename"},
                    "upload_to_flow": {"type": "boolean", "default": False, "description": "Upload the downloaded file to Google Flow and return its media ID"},
                    "max_size_mb": {"type": "integer", "minimum": 1, "maximum": 4096, "default": 2048}
                },
                "required": ["url"]
            }
        },
        {
            "name": "edit_flow_video",
            "description": "Edit an existing Flow video (video-to-video) using its media ID or a local video file.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Requested video transformation"},
                    "media_id": {"type": "string", "description": "Existing Flow video media ID"},
                    "video_path": {"type": "string", "description": "Optional local video; uploaded automatically when media_id is omitted"},
                    "aspect": {"type": "string", "enum": ["landscape", "portrait"], "default": "landscape"},
                    "duration": {"type": "integer", "enum": [4, 6, 8, 10], "default": 8},
                    "ref_media_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 10}
                },
                "required": ["prompt"]
            }
        }
    ]
