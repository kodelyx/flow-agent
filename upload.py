#!/usr/bin/env python3
"""Upload video to Google Flow and get media_id.

Usage:
    python upload.py chunks/chunk_00.mp4
    python upload.py chunks/chunk_00.mp4 --project-id YOUR_PROJECT_ID
"""
import asyncio
import json
import os
import subprocess
import sys
import uuid
import logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from omni import ExtensionBridge

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger('upload')

DEFAULT_PROJECT_ID = 'ff92d5cc-8a03-41d2-b59e-e0774d17bcf6'
MEDIA_ID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'media-id.js')


async def upload_video(video_path: str, project_id: str = DEFAULT_PROJECT_ID) -> dict:
    """Upload a video file to Google Flow.
    
    Returns dict with mediaId, media, workflow on success.
    """
    video_path = os.path.abspath(video_path)
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video not found: {video_path}")

    video_size = os.path.getsize(video_path)
    log.info('Video: %s (%d bytes, %.1fMB)', os.path.basename(video_path), video_size, video_size / 1024 / 1024)

    # Step 1: Get session URL via extension's trpc_request (no ext reload needed)
    bridge = ExtensionBridge()
    await bridge.start()
    if not await bridge.wait_for_extension(timeout=30):
        raise ConnectionError("Extension did not connect")
    await asyncio.sleep(2)

    token = bridge._flow_key
    rid = str(uuid.uuid4())[:8]
    fut = asyncio.get_event_loop().create_future()
    bridge._pending[rid] = fut

    await bridge._ws.send(json.dumps({
        'id': rid,
        'method': 'trpc_request',
        'params': {
            'url': 'https://labs.google/fx/api/upload-video?action=start',
            'method': 'POST',
            'headers': {
                'X-Upload-Project-Id': project_id,
                'X-Upload-Content-Type': 'video/mp4',
                'X-Upload-Content-Length': str(video_size),
            },
        },
    }))

    try:
        r = await asyncio.wait_for(fut, timeout=20)
    except asyncio.TimeoutError:
        await bridge.close()
        raise TimeoutError("Step 1 timeout")

    await bridge.close()

    session_url = r.get('data', {}).get('sessionUrl', '')
    auth_token = token or ''

    if not session_url:
        raise RuntimeError(f"No session URL: {json.dumps(r)[:500]}")

    log.info('Got session URL')

    # Step 2: PUT via curl (works, service worker fetch doesn't)
    log.info('Uploading %d bytes via curl...', video_size)
    proc = subprocess.run([
        'curl', '-s', '-X', 'PUT', session_url,
        '-H', 'Content-Type: video/mp4',
        '-H', f'Authorization: Bearer {auth_token}',
        '-H', 'X-Goog-Upload-Command: upload, finalize',
        '-H', 'X-Goog-Upload-Offset: 0',
        '--data-binary', f'@{video_path}',
        '--max-time', '120',
    ], capture_output=True, text=True)

    if proc.returncode != 0:
        raise RuntimeError(f"curl failed: {proc.stderr[:500]}")

    if not proc.stdout.strip():
        raise RuntimeError("Empty response from upload")

    data = json.loads(proc.stdout)
    media_id = data.get('mediaId') or data.get('name') or data.get('id')
    if not media_id and isinstance(data.get('media'), dict):
        media_id = data['media'].get('name') or data['media'].get('mediaId')

    if media_id:
        log.info('✅ SUCCESS! media_id = %s', media_id)
        update_media_id_js(os.path.basename(video_path), media_id)
    else:
        log.warning('Upload succeeded but no media_id found')
        log.info('Response: %s', json.dumps(data, indent=2)[:1000])

    return data


def update_media_id_js(filename: str, media_id: str):
    """Auto-update media-id.js with new filename → media_id entry."""
    entries = {}
    if os.path.exists(MEDIA_ID_FILE):
        with open(MEDIA_ID_FILE, 'r') as f:
            for line in f:
                line = line.strip()
                if ' : ' in line:
                    k, v = line.split(' : ', 1)
                    entries[k.strip()] = v.strip()

    entries[filename] = media_id

    with open(MEDIA_ID_FILE, 'w') as f:
        for k, v in sorted(entries.items()):
            f.write(f'{k} : {v}\n')

    log.info('📝 Updated media-id.js: %s → %s', filename, media_id)


async def main():
    import argparse
    parser = argparse.ArgumentParser(description='Upload video to Google Flow')
    parser.add_argument('video', help='Path to video file')
    parser.add_argument('--project-id', default=DEFAULT_PROJECT_ID, help='Project ID')
    args = parser.parse_args()

    result = await upload_video(args.video, args.project_id)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
