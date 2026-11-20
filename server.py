import os
import json
import uuid
import subprocess
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from google import genai
from google.genai import types

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
WORK_DIR = "/tmp/easyclip"
os.makedirs(WORK_DIR, exist_ok=True)

class LinkRequest(BaseModel):
    url: str
    niche: str = "general"

class RenderRequest(BaseModel):
    video_id: str
    start_time: str
    end_time: str
    srt_text: str = ""
    burn_subtitles: bool = True

@app.post("/process-link")
async def process_link(req: LinkRequest):
    video_id = str(uuid.uuid4())[:8]
    video_path = f"{WORK_DIR}/{video_id}.mp4"
    audio_path = f"{WORK_DIR}/{video_id}.mp3"

    dl_cmd = [
        "yt-dlp",
        "-f", "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best",
        "--merge-output-format", "mp4",
        "-o", video_path,
        req.url
    ]
    res = subprocess.run(dl_cmd, capture_output=True, text=True)
    if res.returncode != 0 or not os.path.exists(video_path):
        raise HTTPException(status_code=400, detail=f"Could not download video: {res.stderr[:250]}")

    subprocess.run([
        "ffmpeg", "-y", "-i", video_path, "-vn", "-ac", "1", "-b:a", "64k", audio_path
    ], check=True)

    uploaded = client.files.upload(file=audio_path)

    prompt = f"""
    Analyze this {req.niche} video and extract the top 3 most viral, high-retention moments.
    STRICT RULE: Every clip MUST be between 60 and 90 seconds long (duration 60s to 90s).
    Return strictly valid JSON as a list of objects with keys:
    "title" (catchy viral hook title),
    "start_time" (HH:MM:SS),
    "end_time" (HH:MM:SS),
    "srt" (accurate SRT subtitles for that exact clip, starting at 00:00:00,000).
    """

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[uploaded, prompt],
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )

    client.files.delete(name=uploaded.name)
    os.remove(audio_path)

    clips_data = json.loads(response.text)
    ready_clips = []

    for idx, clip in enumerate(clips_data):
        clip_filename = f"{video_id}_clip_{idx}.mp4"
        clip_path = f"{WORK_DIR}/{clip_filename}"

        cut_cmd = [
            "ffmpeg", "-y",
            "-ss", clip["start_time"],
            "-to", clip["end_time"],
            "-i", video_path,
            "-vf", "crop=ih*9/16:ih,scale=720:1280",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            clip_path
        ]
        subprocess.run(cut_cmd, check=True)

        clip["clip_url"] = f"/clips/{clip_filename}"
        ready_clips.append(clip)

    return {"video_id": video_id, "clips": ready_clips}

@app.get("/clips/{filename}")
async def get_clip(filename: str):
    file_path = f"{WORK_DIR}/{filename}"
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Clip not found")
    return FileResponse(file_path, media_type="video/mp4", filename=filename)

@app.post("/render-clip")
async def render_clip(req: RenderRequest):
    video_path = f"{WORK_DIR}/{req.video_id}.mp4"
    if not os.path.exists(video_path):
        raise HTTPException(status_code=404, detail="Source video expired. Re-paste link.")

    out_id = str(uuid.uuid4())[:8]
    out_path = f"{WORK_DIR}/edited_{out_id}.mp4"
    srt_path = f"{WORK_DIR}/edited_{out_id}.srt"

    vf = "crop=ih*9/16:ih,scale=720:1280"
    if req.burn_subtitles and req.srt_text.strip():
        with open(srt_path, "w", encoding="utf-8") as f:
            f.write(req.srt_text)
        vf += f",subtitles={srt_path}:force_style='FontSize=18,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=2,Alignment=2,MarginV=45'"

    subprocess.run([
        "ffmpeg", "-y",
        "-ss", req.start_time,
        "-to", req.end_time,
        "-i", video_path,
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        out_path
    ], check=True)

    return FileResponse(out_path, media_type="video/mp4", filename=f"easyclip_{out_id}.mp4")
  
