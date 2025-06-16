import os
import sys
import requests
import json
import logging
import time
import argparse
import re
import glob
import shutil
from datetime import timedelta
from openai import AzureOpenAI
from concurrent.futures import ThreadPoolExecutor

# -------------------- Setup Logging --------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# -------------------- Command-Line Argument Parsing --------------------
parser = argparse.ArgumentParser(description="Extract and redact video segments for each speaker using Azure Video Indexer.")
parser.add_argument("--input_video", required=True, help="Path to the input video file")
parser.add_argument("--video_id", default=None, help="Existing Video Indexer video ID (if not provided, video will be uploaded)")
parser.add_argument("--blur_type", default="HighBlur", choices=["MediumBlur", "HighBlur", "LowBlur", "BoundingBox", "Black"],
                    help="Type of face blurring to apply")
parser.add_argument("--exclude_faces", nargs="*", help="Face IDs to exclude from redaction")
parser.add_argument("--parallel", action="store_true", help="Enable parallel processing")
args = parser.parse_args()

# -------------------- Configuration --------------------
account_id = "46b7b8fb-85be-4457-836f-99ab7b722245"
location = "trial"
subscription_key = "50b0ef69dd38466ea287e3efc01646ea"
api_version = "2024-02-15-preview"

# -------------------- Helper Functions --------------------
def get_access_token(max_retries=3, delay=2):
    """Fetch access token with retry logic."""
    token_url = f"https://api.videoindexer.ai/Auth/{location}/Accounts/{account_id}/AccessToken?allowEdit=true"
    headers = {"Ocp-Apim-Subscription-Key": subscription_key}
    
    for attempt in range(max_retries):
        try:
            response = requests.get(token_url, headers=headers, timeout=10)
            if response.status_code == 200:
                return response.text.strip('"')
            logger.warning(f"Attempt {attempt + 1} failed: {response.status_code}")
            if attempt < max_retries - 1:
                time.sleep(delay)
        except Exception as e:
            logger.error(f"Error getting access token: {e}")
            if attempt < max_retries - 1:
                time.sleep(delay)
    raise Exception("Failed to get access token")

def upload_video(input_video, access_token):
    """Upload video to Video Indexer and wait for indexing to complete."""
    logger.info("Uploading video to Video Indexer...")
    
    # Upload video with language and face detection settings
    upload_url = f"https://api.videoindexer.ai/{location}/Accounts/{account_id}/Videos"
    headers = {"Authorization": f"Bearer {access_token}"}
    
    # Add language parameters and face detection settings
    params = {
        "language": "ar-AE",  # Arabic (UAE)
        "streamingPreset": "Default",
        "indexingPreset": "Advanced",  # Changed from Default to Advanced
        "linguisticModelId": "default",
        "priority": "Normal",
        "sendSuccessEmail": "true",
        "detectSourceLanguage": "true",
        "faceDetection": {
            "mode": "PerFace",  # Enable per-face detection
            "model": "Latest"   # Use the latest face detection model
        }
    }
    
    with open(input_video, "rb") as f:
        files = {"video": f}
        response = requests.post(upload_url, headers=headers, files=files, params=params)
        
        if response.status_code != 200:
            raise Exception(f"Failed to upload video: {response.status_code}")
        
        video_id = response.json()["id"]
        logger.info(f"Video uploaded successfully. Video ID: {video_id}")
    
    # Wait for indexing to complete
    logger.info("Waiting for video indexing to complete...")
    while True:
        status_url = f"https://api.videoindexer.ai/{location}/Accounts/{account_id}/Videos/{video_id}/Index"
        status_response = requests.get(status_url, headers=headers)
        
        if status_response.status_code == 200:
            state = status_response.json()["state"]
            if state == "Processed":
                logger.info("Video indexing completed successfully")
                return video_id
            elif state == "Failed":
                raise Exception("Video indexing failed")
        
        logger.info("Indexing in progress... waiting 10 seconds")
        time.sleep(10)

def create_redaction_job(video_id, access_token):
    """Create a face redaction job."""
    redact_url = f"https://api.videoindexer.ai/{location}/Accounts/{account_id}/Videos/{video_id}/redact"
    headers = {"Authorization": f"Bearer {access_token}"}
    
    redaction_body = {
        "faces": {
            "blurringKind": args.blur_type
        }
    }
    
    if args.exclude_faces:
        redaction_body["faces"]["filter"] = {
            "ids": [int(face_id) for face_id in args.exclude_faces],
            "scope": "Exclude"
        }
    
    response = requests.post(redact_url, headers=headers, json=redaction_body)
    if response.status_code != 202:
        raise Exception(f"Failed to create redaction job: {response.status_code}")
    
    return response.headers.get("Location")

def process_speaker_segment(segment_info):
    """Process a single speaker segment with face redaction."""
    try:
        speaker_name = segment_info["speaker_name"]
        start_time = segment_info["start"]
        end_time = segment_info["end"]
        output_file = segment_info["output_file"]
        
        logger.info(f"Processing segment for {speaker_name} from {start_time} to {end_time}")
        
        # Extract segment
        extract_cmd = f'ffmpeg -i "{args.input_video}" -ss {start_time} -to {end_time} -c:v libx264 -c:a aac "{output_file}"'
        os.system(extract_cmd)
        
        if not os.path.exists(output_file):
            raise Exception(f"Failed to create segment file: {output_file}")
        
        # Create redaction job for the segment
        with open(output_file, "rb") as f:
            files = {"video": f}
            response = requests.post(f"https://api.videoindexer.ai/{location}/Accounts/{account_id}/Videos",
                                  headers={"Authorization": f"Bearer {access_token}"},
                                  files=files)
            if response.status_code != 200:
                raise Exception(f"Failed to upload segment: {response.status_code}")
            
            segment_video_id = response.json()["id"]
            redaction_job_url = create_redaction_job(segment_video_id, access_token)
            
            # Wait for redaction to complete
            while True:
                status_response = requests.get(redaction_job_url, headers={"Authorization": f"Bearer {access_token}"})
                if status_response.json()["state"] == "Processed":
                    break
                logger.info(f"Waiting for redaction to complete for {speaker_name} segment...")
                time.sleep(5)
            
            # Download redacted segment
            download_url = f"https://api.videoindexer.ai/{location}/Accounts/{account_id}/Videos/{segment_video_id}/SourceFile/DownloadUrl"
            download_response = requests.get(download_url, headers={"Authorization": f"Bearer {access_token}"})
            if download_response.status_code == 200:
                redacted_url = download_response.json()
                with open(output_file, "wb") as out_file:
                    out_file.write(requests.get(redacted_url, allow_redirects=True).content)
                
        logger.info(f"Successfully processed segment for {speaker_name}")
        return True
    except Exception as e:
        logger.error(f"Error processing segment for {speaker_name}: {e}")
        return False

def save_insights(video_index, video_id):
    """Save both full insights and faces-specific JSON files."""
    # Save full insights
    full_insights_path = f"insights_{video_id}_full.json"
    try:
        with open(full_insights_path, "w", encoding="utf-8") as f:
            json.dump(video_index, f, indent=2, ensure_ascii=False)
        logger.info(f"Full insights saved to '{full_insights_path}'")
    except Exception as e:
        logger.error(f"Failed to save full insights: {e}")

    # Extract and save faces data
    faces_path = f"faces_{video_id}.json"
    try:
        # Try to get faces from summarized insights first
        faces = video_index.get("summarizedInsights", {}).get("faces", [])
        if not faces:
            # If not in summarized insights, try to get from videos array
            faces = video_index.get("videos", [])[0].get("insights", {}).get("faces", [])
        
        faces_data = {"faces": faces}
        with open(faces_path, "w", encoding="utf-8") as f:
            json.dump(faces_data, f, indent=2, ensure_ascii=False)
        logger.info(f"Faces data saved to '{faces_path}'")

        # Get face artifacts for complete face data
        artifact_url = f"https://api.videoindexer.ai/{location}/Accounts/{account_id}/Videos/{video_id}/ArtifactUrl?type=Faces"
        headers = {"Authorization": f"Bearer {access_token}"}
        response = requests.get(artifact_url, headers=headers)
        
        if response.status_code == 200:
            artifact_download_url = response.json()
            artifact_response = requests.get(artifact_download_url)
            if artifact_response.status_code == 200:
                artifacts_path = f"faces_artifacts_{video_id}.json"
                with open(artifacts_path, "w", encoding="utf-8") as f:
                    json.dump(artifact_response.json(), f, indent=2, ensure_ascii=False)
                logger.info(f"Face artifacts saved to '{artifacts_path}'")
    except Exception as e:
        logger.error(f"Failed to save faces data: {e}")

def extract_speaker_segments(video_index):
    """Extract speaker segments from video index and format as custom insights."""
    speakers = {}
    
    # Try to get speakers from different parts of the index
    try:
        # First try summarizedInsights
        all_speakers = video_index.get("summarizedInsights", {}).get("speakers", [])
        source = "summarizedInsights"
        
        # If no speakers found, try videos array
        if not all_speakers:
            all_speakers = video_index.get("videos", [])[0].get("insights", {}).get("speakers", [])
            source = "videos insights"
            
        # If still no speakers, try transcript
        if not all_speakers:
            transcript = video_index.get("videos", [])[0].get("insights", {}).get("transcript", [])
            speaker_ids = set()
            for segment in transcript:
                if "speakerId" in segment:
                    speaker_ids.add(str(segment["speakerId"]))
            all_speakers = [{"id": speaker_id, "name": f"Speaker #{speaker_id}"} for speaker_id in speaker_ids]
            source = "transcript"
        
        logger.info("\n" + "="*50)
        logger.info(f"Speaker Detection Results (source: {source})")
        logger.info("="*50)
        logger.info(f"Total speakers detected: {len(all_speakers)}")
        
        # Initialize speakers
        for speaker in all_speakers:
            speaker_id = str(speaker["id"])
            speaker_name = speaker.get("name", f"Speaker #{speaker_id}")
            speakers[speaker_id] = {
                "name": speaker_name,
                "segments": []
            }
            logger.info(f"\nDetected Speaker: {speaker_name} (ID: {speaker_id})")
        
        # Get transcript segments
        transcript = video_index.get("videos", [])[0].get("insights", {}).get("transcript", [])
        
        # Collect all speaking segments
        for segment in transcript:
            speaker_id = str(segment.get("speakerId", "unknown"))
            if speaker_id not in speakers:
                speakers[speaker_id] = {
                    "name": f"Speaker #{speaker_id}",
                    "segments": []
                }
            
            instances = segment.get("instances", [])
            if instances:
                speakers[speaker_id]["segments"].append({
                    "start": instances[0]["start"],
                    "end": instances[0]["end"],
                    "text": segment.get("text", "")
                })
        
        # Sort segments by start time and merge consecutive segments
        logger.info("\n" + "="*50)
        logger.info("Speaker Segments Analysis")
        logger.info("="*50)
        
        for speaker_id, speaker_data in speakers.items():
            speaker_data["segments"].sort(key=lambda x: x["start"])
            merged_segments = []
            total_duration = 0
            
            if speaker_data["segments"]:
                current = speaker_data["segments"][0]
                
                for segment in speaker_data["segments"][1:]:
                    # If segments are close (within 2 seconds), merge them
                    if time_to_seconds(segment["start"]) - time_to_seconds(current["end"]) <= 2:
                        current["end"] = segment["end"]
                        current["text"] += " " + segment["text"]
                    else:
                        duration = time_to_seconds(current["end"]) - time_to_seconds(current["start"])
                        total_duration += duration
                        merged_segments.append(current)
                        current = segment
                
                # Add the last segment
                duration = time_to_seconds(current["end"]) - time_to_seconds(current["start"])
                total_duration += duration
                merged_segments.append(current)
                speaker_data["segments"] = merged_segments
                
                # Log speaker statistics
                logger.info(f"\nSpeaker: {speaker_data['name']}")
                logger.info(f"  - Number of speech segments: {len(merged_segments)}")
                logger.info(f"  - Total speaking time: {str(timedelta(seconds=int(total_duration)))}")
                logger.info(f"  - First appearance: {merged_segments[0]['start']}")
                logger.info(f"  - Last appearance: {merged_segments[-1]['end']}")
        
        # Remove speakers with no segments
        empty_speakers = [k for k, v in speakers.items() if not v["segments"]]
        if empty_speakers:
            logger.info("\n" + "="*50)
            logger.info("Removing speakers with no speech segments:")
            for k in empty_speakers:
                logger.warning(f"- {speakers[k]['name']} (no speech segments found)")
                del speakers[k]
        
        if not speakers:
            logger.warning("\nNo speaker segments found in the video index")
        else:
            logger.info("\n" + "="*50)
            logger.info(f"Final speaker count: {len(speakers)}")
            logger.info("="*50 + "\n")
            
        return speakers
        
    except Exception as e:
        logger.error(f"Error extracting speaker segments: {e}")
        return {}

def time_to_seconds(time_str):
    """Convert time string (HH:MM:SS.SS) to seconds."""
    h, m, s = time_str.split(':')
    return float(h) * 3600 + float(m) * 60 + float(s)

def create_speaker_video(speaker_data, input_video):
    """Create a single video for a speaker containing all their speech segments."""
    try:
        speaker_name = speaker_data["name"]
        segments = speaker_data["segments"]
        
        if not segments:
            logger.warning(f"No segments found for {speaker_name}")
            return False
        
        # Create filter complex for direct concatenation
        filter_complex = []
        input_args = []
        
        for i, segment in enumerate(segments):
            # Add input for each segment
            input_args.extend([
                "-ss", segment["start"],
                "-to", segment["end"],
                "-i", input_video
            ])
            
            # Add to filter complex
            filter_complex.append(f"[{i}:v][{i}:a]")
        
        # Build the concat command
        output_file = f"{speaker_name}_full.mp4"
        concat_filter = f"{''.join(filter_complex)}concat=n={len(segments)}:v=1:a=1[outv][outa]"
        
        # Construct the full ffmpeg command
        cmd_parts = [
            "ffmpeg",
            *input_args,
            "-filter_complex", concat_filter,
            "-map", "[outv]",
            "-map", "[outa]",
            "-c:v", "libx264",
            "-c:a", "aac",
            "-y",  # Overwrite output file if exists
            output_file
        ]
        
        # Execute the command
        cmd = " ".join(f'"{arg}"' if " " in str(arg) else str(arg) for arg in cmd_parts)
        logger.info(f"Creating video for {speaker_name}")
        result = os.system(cmd)
        
        if result == 0:
            logger.info(f"Successfully created video for {speaker_name}: {output_file}")
            return True
        else:
            logger.error(f"Failed to create video for {speaker_name}")
            return False
            
    except Exception as e:
        logger.error(f"Error creating video for {speaker_name}: {e}")
        return False

def process_video(input_video, video_id=None, blur_type="HighBlur", exclude_faces=None, parallel=False):
    """Process video and return video_id."""
    global access_token
    
    # Get access token
    access_token = get_access_token()
    
    try:
        # Use existing video ID or upload new video
        if video_id:
            logger.info(f"Using existing video ID: {video_id}")
        else:
            video_id = upload_video(input_video, access_token)
            logger.info(f"Uploaded new video with ID: {video_id}")
        
        # Get video index with summarized insights
        index_url = f"https://api.videoindexer.ai/{location}/Accounts/{account_id}/Videos/{video_id}/Index?language=en-US&includeSummarizedInsights=true"
        headers = {"Authorization": f"Bearer {access_token}"}
        
        logger.info("Fetching video index...")
        response = requests.get(index_url, headers=headers)
        if response.status_code != 200:
            raise Exception(f"Failed to get video index: {response.status_code}")
        
        video_index = response.json()
        logger.info("Successfully retrieved video index")
        
        # Save insights
        save_insights(video_index, video_id)
        
        # Extract speaker segments
        speakers = extract_speaker_segments(video_index)
        logger.info(f"Found {len(speakers)} speakers")
        
        # Process each speaker's video
        for speaker_id, speaker_data in speakers.items():
            if parallel:
                with ThreadPoolExecutor() as executor:
                    future = executor.submit(create_speaker_video, speaker_data, input_video)
                    future.result()
            else:
                create_speaker_video(speaker_data, input_video)
        
        logger.info("All speaker videos processed successfully")
        return video_id
        
    except Exception as e:
        logger.error(f"Error in main process: {e}")
        raise

if __name__ == '__main__':
    # Parse command line arguments only when run directly
    parser = argparse.ArgumentParser(description="Extract and redact video segments for each speaker using Azure Video Indexer.")
    parser.add_argument("--input_video", required=True, help="Path to the input video file")
    parser.add_argument("--video_id", default=None, help="Existing Video Indexer video ID")
    parser.add_argument("--blur_type", default="HighBlur", 
                        choices=["MediumBlur", "HighBlur", "LowBlur", "BoundingBox", "Black"],
                        help="Type of face blurring to apply")
    parser.add_argument("--exclude_faces", nargs="*", help="Face IDs to exclude from redaction")
    parser.add_argument("--parallel", action="store_true", help="Enable parallel processing")
    
    args = parser.parse_args()
    
    try:
        video_id = process_video(
            args.input_video,
            args.video_id,
            args.blur_type,
            args.exclude_faces,
            args.parallel
        )
    except Exception as e:
        logger.error(f"Error: {e}")
        sys.exit(1) 