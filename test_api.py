#!/usr/bin/env python3
"""Integration Test Script for Flow Agent API.

Tests the following end-to-end flow:
1. GET /health
2. POST /generate/image (T2I)
3. POST /upload/image
4. POST /generate/video (I2V)
5. POST /upload/video
6. POST /edit/video (V2V)
"""

import os
import sys
import time

# Auto-install requests if not present
try:
    import requests
except ImportError:
    print("📦 Installing 'requests' library for testing...")
    os.system(f"{sys.executable} -m pip install requests")
    import requests

API_URL = "http://localhost:8000"

def log_step(name):
    print(f"\n{'=' * 60}")
    print(f"🚀 STEP: {name}")
    print(f"{'=' * 60}")

def check_health():
    log_step("Checking Server Health")
    try:
        r = requests.get(f"{API_URL}/health")
        print(f"Status Code: {r.status_code}")
        print(f"Response: {r.text}")
        if r.status_code != 200:
            print("❌ Server health check failed!")
            sys.exit(1)
        data = r.json()
        if not data.get("extension_connected"):
            print("❌ Chrome extension is NOT connected. Please check Chrome devtools.")
            sys.exit(1)
        if not data.get("has_flow_key"):
            print("⚠️ Chrome extension is connected but NOT logged in / authorized.")
            print("   Please make sure labs.google/fx/tools/flow is open and logged in Chrome.")
            sys.exit(1)
        print("✅ Health Check Passed!")
    except Exception as e:
        print(f"❌ Failed to connect to server: {e}")
        sys.exit(1)

def generate_image():
    log_step("Text-to-Image (T2I) Generation")
    payload = {
        "prompt": "a simple red apple on a clean wooden table, professional studio lighting, 8k resolution",
        "aspect": "square",
        "count": 1
    }
    r = requests.post(f"{API_URL}/generate/image", json=payload)
    print(f"Status Code: {r.status_code}")
    print(f"Response: {r.text}")
    
    if r.status_code != 200:
        print("❌ Image generation failed!")
        sys.exit(1)
        
    data = r.json()
    outputs = data.get("outputs", [])
    if not outputs:
        print("❌ No outputs returned in image generation!")
        sys.exit(1)
        
    img_info = outputs[0]
    remote_url = img_info.get("remote_url")
    filename = img_info.get("filename")
    
    # Download generated image locally for next steps
    local_img_path = os.path.join("output", "test_t2i.png")
    os.makedirs("output", exist_ok=True)
    
    print(f"📥 Downloading image from {remote_url}...")
    img_r = requests.get(remote_url)
    with open(local_img_path, "wb") as f:
        f.write(img_r.content)
        
    print(f"✅ Saved test image to {local_img_path}")
    return local_img_path

def upload_image(image_path):
    log_step("Uploading Image to Flow")
    with open(image_path, "rb") as f:
        files = {"file": (os.path.basename(image_path), f, "image/png")}
        r = requests.post(f"{API_URL}/upload/image", files=files)
        
    print(f"Status Code: {r.status_code}")
    print(f"Response: {r.text}")
    
    if r.status_code != 200:
        print("❌ Image upload failed!")
        sys.exit(1)
        
    data = r.json()
    media_id = data.get("media_id")
    if not media_id:
        print("❌ No media_id returned from upload!")
        sys.exit(1)
        
    print(f"✅ Image uploaded successfully! media_id: {media_id}")
    return media_id

def generate_image_i2i(image_media_id):
    log_step("Image-to-Image (I2I) Generation")
    payload = {
        "prompt": "transform the red apple into a vibrant cartoon style, Pixar 3D aesthetic",
        "aspect": "square",
        "count": 1,
        "ref": [image_media_id]
    }
    r = requests.post(f"{API_URL}/generate/image", json=payload)
    print(f"Status Code: {r.status_code}")
    print(f"Response: {r.text}")
    
    if r.status_code != 200:
        print("❌ Image-to-Image (I2I) generation failed!")
        sys.exit(1)
        
    data = r.json()
    outputs = data.get("outputs", [])
    if not outputs:
        print("❌ No outputs returned in I2I generation!")
        sys.exit(1)
        
    img_info = outputs[0]
    remote_url = img_info.get("remote_url")
    
    local_i2i_path = os.path.join("output", "test_i2i.png")
    print(f"📥 Downloading I2I image from {remote_url}...")
    img_r = requests.get(remote_url)
    with open(local_i2i_path, "wb") as f:
        f.write(img_r.content)
        
    print(f"✅ Saved I2I image to {local_i2i_path}")
    return local_i2i_path

def generate_video_i2v(image_media_id):
    log_step("Image-to-Video (I2V) Generation")
    payload = {
        "prompt": "the apple slowly rolls forward, dynamic cinematic panning, highly realistic",
        "aspect": "portrait",
        "duration": 4, # Short duration for faster testing
        "count": 1,
        "start": image_media_id # Animate using the uploaded image
    }
    r = requests.post(f"{API_URL}/generate/video", json=payload)
    print(f"Status Code: {r.status_code}")
    print(f"Response: {r.text}")
    
    if r.status_code != 200:
        print("❌ Video generation failed!")
        sys.exit(1)
        
    data = r.json()
    outputs = data.get("outputs", [])
    if not outputs:
        print("❌ No outputs returned in video generation!")
        sys.exit(1)
        
    video_info = outputs[0]
    download_url = video_info.get("download_url")
    filename = video_info.get("filename")
    
    local_video_path = os.path.join("output", "test_i2v.mp4")
    
    print(f"📥 Downloading video from {API_URL}{download_url}...")
    video_r = requests.get(f"{API_URL}{download_url}")
    with open(local_video_path, "wb") as f:
        f.write(video_r.content)
        
    print(f"✅ Saved test video to {local_video_path}")
    return local_video_path

def upload_video(video_path):
    log_step("Uploading Video to Flow")
    with open(video_path, "rb") as f:
        files = {"file": (os.path.basename(video_path), f, "video/mp4")}
        r = requests.post(f"{API_URL}/upload/video", files=files)
        
    print(f"Status Code: {r.status_code}")
    print(f"Response: {r.text}")
    
    if r.status_code != 200:
        print("❌ Video upload failed!")
        sys.exit(1)
        
    data = r.json()
    media_id = data.get("media_id")
    if not media_id:
        print("❌ No media_id returned from upload!")
        sys.exit(1)
        
    print(f"✅ Video uploaded successfully! media_id: {media_id}")
    return media_id

def edit_video_v2v(video_media_id):
    log_step("Video-to-Video (V2V) Editing")
    payload = {
        "prompt": "make it look like an anime sketch style, drawing pencil style",
        "video_media_id": video_media_id,
        "aspect": "portrait",
        "fps": 24,
        "duration": 4
    }
    r = requests.post(f"{API_URL}/edit/video", json=payload)
    print(f"Status Code: {r.status_code}")
    print(f"Response: {r.text}")
    
    if r.status_code != 200:
        print("❌ V2V video editing failed!")
        sys.exit(1)
        
    data = r.json()
    outputs = data.get("outputs", [])
    if not outputs:
        print("❌ No outputs returned in video edit!")
        sys.exit(1)
        
    video_info = outputs[0]
    download_url = video_info.get("download_url")
    
    local_edit_path = os.path.join("output", "test_v2v.mp4")
    
    print(f"📥 Downloading edited video from {API_URL}{download_url}...")
    video_r = requests.get(f"{API_URL}{download_url}")
    with open(local_edit_path, "wb") as f:
        f.write(video_r.content)
        
    print(f"✅ Saved edited video to {local_edit_path}")

def main():
    print("🎬 Starting Flow Agent API Integration Test...")
    
    # 1. Health check
    check_health()
    
    # 2. Text to Image (T2I)
    local_image = generate_image()
    
    # 3. Upload Image to GCS/Flow
    image_media_id = upload_image(local_image)
    
    # 4. Image to Image (I2I)
    local_i2i_image = generate_image_i2i(image_media_id)
    
    # 5. Image to Video (I2V)
    local_video = generate_video_i2v(image_media_id)
    
    # 6. Upload Video to GCS/Flow
    video_media_id = upload_video(local_video)
    
    # 7. Video to Video (V2V)
    edit_video_v2v(video_media_id)
    
    print("\n" + "=" * 60)
    print("🎉 ALL TESTS PASSED SUCCESSFULLY! Flow Agent API is 100% verified.")
    print("=" * 60)

if __name__ == "__main__":
    main()
