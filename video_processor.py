import os
import sys
import requests
import json
import logging
import time
import urllib3
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import urllib.parse
from dateutil import parser as date_parser
from requests_toolbelt import MultipartEncoder, MultipartEncoderMonitor

# -------------------- Setup Logging --------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# -------------------- Configuration --------------------
account_id = "46b7b8fb-85be-4457-836f-99ab7b722245"
location = "trial"
subscription_key = "50b0ef69dd38466ea287e3efc01646ea"
api_version = "2024-02-15-preview"

# Configure requests session with retries and SSL settings
session = requests.Session()
retry_strategy = Retry(
    total=5,  # Total number of retries
    backoff_factor=1,  # Factor to apply between attempts (1s, 2s, 4s, 8s, 16s)
    status_forcelist=[408, 429, 500, 502, 503, 504],  # Added 408 for timeout errors
    allowed_methods=["HEAD", "GET", "PUT", "DELETE", "OPTIONS", "TRACE", "POST"]  # Added POST
)
adapter = HTTPAdapter(
    max_retries=retry_strategy,
    pool_maxsize=20,  # Increased from 10
    pool_connections=10
)
session.mount("https://", adapter)
session.verify = False  # Disable SSL verification by default for trial account

# Suppress SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def get_access_token(max_retries=3, delay=2):
    """Fetch access token with retry logic."""
    token_url = f"https://api.videoindexer.ai/auth/{location}/Accounts/{account_id}/AccessToken"
    headers = {
        "Ocp-Apim-Subscription-Key": subscription_key
    }
    params = {
        "allowEdit": "true"
    }
    
    for attempt in range(max_retries):
        try:
            response = session.get(
                token_url,
                headers=headers,
                params=params,
                timeout=30,
                verify=False
            )
            if response.status_code == 200:
                return response.text.strip('"')
            logger.warning(f"Attempt {attempt + 1} failed: {response.status_code} - {response.text}")
            if attempt < max_retries - 1:
                time.sleep(delay)
        except Exception as e:
            logger.error(f"Error getting access token: {e}")
            if attempt < max_retries - 1:
                time.sleep(delay)
    raise Exception("Failed to get access token")

class FileStreamIO:
    """Custom file stream iterator for large file uploads."""
    def __init__(self, filename, chunk_size=1024*1024):
        self.filename = filename
        self.chunk_size = chunk_size
        self.file_size = os.path.getsize(filename)
        self.bytes_read = 0
        self.file = None
        self.last_progress = 0
        
    def __iter__(self):
        self.file = open(self.filename, 'rb')
        return self
        
    def __next__(self):
        chunk = self.file.read(self.chunk_size)
        if not chunk:
            self.file.close()
            raise StopIteration
        
        self.bytes_read += len(chunk)
        progress = int((self.bytes_read / self.file_size) * 100)
        
        # Log progress every 5%
        if progress >= self.last_progress + 5:
            logger.info(f"Upload progress: {progress}% ({self.bytes_read/1024/1024:.1f}MB / {self.file_size/1024/1024:.1f}MB)")
            self.last_progress = progress
            
        return chunk
        
    def __len__(self):
        return self.file_size

def create_callback(filename):
    """Create a callback function for upload progress tracking."""
    file_size = os.path.getsize(filename)
    last_progress = [0]
    
    def callback(monitor):
        progress = int((monitor.bytes_read / file_size) * 100)
        
        # Log progress every 5%
        if progress >= last_progress[0] + 5:
            logger.info(f"Upload progress: {progress}% ({monitor.bytes_read/1024/1024:.1f}MB / {file_size/1024/1024:.1f}MB)")
            last_progress[0] = progress
    
    return callback

def upload_video(input_video, access_token):
    """Upload video to Video Indexer using MultipartEncoder for reliable file upload."""
    logger.info("Uploading video to Video Indexer...")
    
    # Base URL for trial account
    upload_url = f"https://api.videoindexer.ai/{location}/Accounts/{account_id}/Videos"
    
    # Headers for upload
    headers = {
        "Authorization": f"Bearer {access_token}"
    }
    
    # Basic parameters for trial account
    params = {
        "name": os.path.basename(input_video),
        "language": "ar-AE",
        "sourceLanguage": "Arabic",
        "streamingPreset": "SingleBitrate",
        "indexingPreset": "Advanced",
        "privacy": "Private",
        "linguisticModelId": "default",
        "sendSuccessEmail": "false",
        "detectSourceLanguage": "true"
    }
    
    # Features configuration
    features = {
        "audioEffects": True,
        "closedCaptions": True,
        "keyframes": True,
        "audioTranscription": True,
        "slateDetection": True,
        "objectDetection": True,
        "textBasedEmotions": True,
        "namedEntities": True,
        "featuredClothing": True,
        "keywords": True,
        "visualLabels": True,
        "textualLogos": True,
        "observedPeople": True,
        "ocrDetection": True,
        "peopleClothing": True,
        "rollingCredits": True,
        "speakers": True,
        "topics": True,
        "speakerIdentification": True,
        "speakerDiarization": True
    }
    
    # Excluded features
    excluded = {
        "faceDetection": False,
        "celebrities": False,
        "customFaces": False,
        "matchedPerson": False,
        "editorialShotType": False
    }
    
    # Add features configuration
    params.update({
        "features": json.dumps({
            **features,
            **excluded
        })
    })
    
    try:
        # Check if file exists and is readable
        if not os.path.isfile(input_video):
            raise Exception(f"Video file not found: {input_video}")
        
        # Check file size and log it
        file_size = os.path.getsize(input_video)
        logger.info(f"File size: {file_size/1024/1024:.2f} MB")
        if file_size > 1024 * 1024 * 1024:  # 1GB
            raise Exception("File size exceeds 1GB limit")
        
        # Add query parameters to URL
        query_string = urllib.parse.urlencode(params)
        url_with_params = f"{upload_url}?{query_string}"
        
        # Create MultipartEncoder
        encoder = MultipartEncoder({
            'video': (os.path.basename(input_video), open(input_video, 'rb'), 'video/mp4')
        })
        
        # Create progress callback
        callback = create_callback(input_video)
        
        # Create monitor
        monitor = MultipartEncoderMonitor(encoder, callback)
        
        # Update headers with content type
        headers['Content-Type'] = monitor.content_type
        
        # Log request details
        logger.info(f"Uploading to URL: {url_with_params}")
        logger.info(f"Headers: {headers}")
        
        # Make the request with streaming upload
        response = session.post(
            url_with_params,
            data=monitor,
            headers=headers,
            timeout=(30, None),  # No read timeout, only connect timeout
            verify=False
        )
        
        # Log response for debugging
        logger.info(f"Upload response status: {response.status_code}")
        logger.info(f"Upload response text: {response.text}")
        
        if response.status_code != 200:
            raise Exception(f"Failed to upload video: {response.status_code} - {response.text}")
        
        video_id = response.json().get("id")
        if not video_id:
            raise Exception("No video ID in response")
            
        logger.info(f"Video uploaded successfully. Video ID: {video_id}")
        
        # Wait for indexing to complete with timeout
        logger.info("Waiting for video indexing to complete...")
        start_time = time.time()
        max_wait_time = 3600  # 1 hour max wait time
        
        while True:
            if time.time() - start_time > max_wait_time:
                raise Exception("Indexing timeout: Process took longer than 1 hour")
            
            status_url = f"https://api.videoindexer.ai/{location}/Accounts/{account_id}/Videos/{video_id}/Index"
            status_response = session.get(
                status_url,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=30,
                verify=False
            )
            
            if status_response.status_code == 200:
                state = status_response.json().get("state", "")
                if state == "Processed":
                    logger.info("Video indexing completed successfully")
                    
                    # Save insights to file
                    insights_file = f'insights_{video_id}_full.json'
                    with open(insights_file, 'w', encoding='utf-8') as f:
                        json.dump(status_response.json(), f, ensure_ascii=False, indent=2)
                    
                    return video_id
                elif state == "Failed":
                    raise Exception("Video indexing failed")
                else:
                    logger.info(f"Current state: {state}")
                    
                    # Add progress information if available
                    progress = status_response.json().get("processingProgress")
                    if progress:
                        logger.info(f"Processing progress: {progress}")
            
            logger.info("Indexing in progress... waiting 10 seconds")
            time.sleep(10)
            
    except requests.exceptions.Timeout:
        logger.error("Request timed out. This could be due to slow internet connection or server issues.")
        raise Exception("Upload timeout: The request took too long to complete. Please try again or check your internet connection.")
    except requests.exceptions.ConnectionError as e:
        logger.error(f"Connection error: {str(e)}")
        raise Exception("Connection error: Unable to connect to the server. Please check your internet connection.")
    except requests.exceptions.RequestException as e:
        logger.error(f"Request error: {str(e)}")
        raise Exception(f"Upload failed: {str(e)}")
    except Exception as e:
        logger.error(f"Error during upload: {e}")
        raise
    finally:
        # Ensure the file is closed if it was opened
        try:
            encoder.fields['video'][1].close()
        except:
            pass

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
            "-y",
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

def download_video_from_azure(video_id, access_token):
    """Download video from Azure Video Indexer."""
    try:
        # Get video download URL
        download_url = f"https://api.videoindexer.ai/{location}/Accounts/{account_id}/Videos/{video_id}/SourceFile/DownloadUrl"
        headers = {"Authorization": f"Bearer {access_token}"}
        response = requests.get(download_url, headers=headers, verify=False)
        
        if response.status_code != 200:
            raise Exception(f"Failed to get video download URL: {response.status_code}")
            
        video_url = response.json()
        
        # Download the video
        video_path = os.path.join('uploads', f'video_{video_id}.mp4')
        os.makedirs('uploads', exist_ok=True)
        
        logger.info(f"Downloading video from Azure to {video_path}")
        response = requests.get(video_url, stream=True, verify=False)
        response.raise_for_status()
        
        with open(video_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    
        logger.info("Video download completed")
        return video_path
    except Exception as e:
        logger.error(f"Error downloading video: {e}")
        raise

def process_video(input_video=None, video_id=None, parallel=False):
    """Process video and return video_id.
    
    Args:
        input_video: Path to video file to upload, or None if using existing video_id
        video_id: Existing video ID to process, or None if uploading new video
        parallel: Whether to process speaker videos in parallel
    """
    access_token = get_access_token()
    downloaded_video = None
    
    try:
        # If input_video is provided, verify it exists and check size
        if input_video:
            if not os.path.exists(input_video):
                raise Exception(f"Video file not found: {input_video}")
                
            file_size = os.path.getsize(input_video)
            if file_size > 1024 * 1024 * 1024:  # 1GB
                raise Exception("File size exceeds 1GB limit")
            
            # Upload new video
            video_id = upload_video(input_video, access_token)
            logger.info(f"Uploaded new video with ID: {video_id}")
            video_path = input_video
        elif not video_id:
            raise Exception("Either input_video or video_id must be provided")
        else:
            logger.info(f"Using existing video ID: {video_id}")
            
            # Verify the video exists in Azure
            verify_url = f"https://api.videoindexer.ai/{location}/Accounts/{account_id}/Videos/{video_id}/Index"
            headers = {"Authorization": f"Bearer {access_token}"}
            response = requests.get(verify_url, headers=headers, verify=False)
            
            if response.status_code != 200:
                raise Exception(f"Video ID not found in Azure: {video_id}")
                
            # Download the video from Azure
            video_path = download_video_from_azure(video_id, access_token)
            downloaded_video = video_path
        
        # Get video index with summarized insights
        index_url = f"https://api.videoindexer.ai/{location}/Accounts/{account_id}/Videos/{video_id}/Index?language=ar-EG"
        headers = {"Authorization": f"Bearer {access_token}"}
        
        logger.info("Fetching video index...")
        response = requests.get(index_url, headers=headers, verify=False)
        if response.status_code != 200:
            raise Exception(f"Failed to get video index: {response.status_code}")
        
        video_index = response.json()
        logger.info("Successfully retrieved video index")
        
        # Save insights to file
        insights_file = f'insights_{video_id}_full.json'
        with open(insights_file, 'w', encoding='utf-8') as f:
            json.dump(video_index, f, ensure_ascii=False, indent=2)
        
        # Extract speaker segments
        speakers = extract_speaker_segments(video_index)
        logger.info(f"Found {len(speakers)} speakers")
        
        # Process each speaker's video
        for speaker_id, speaker_data in speakers.items():
            if parallel:
                with ThreadPoolExecutor() as executor:
                    future = executor.submit(create_speaker_video, speaker_data, video_path)
                    future.result()
            else:
                create_speaker_video(speaker_data, video_path)
        
        logger.info("All speaker videos processed successfully")
        return video_id, speakers
        
    except Exception as e:
        logger.error(f"Error in processing: {e}")
        raise
    finally:
        # Clean up downloaded video if it exists
        if downloaded_video and os.path.exists(downloaded_video):
            try:
                os.remove(downloaded_video)
                logger.info(f"Cleaned up downloaded video: {downloaded_video}")
            except Exception as e:
                logger.error(f"Error cleaning up downloaded video: {e}")

def extract_speaker_segments(video_index):
    """Extract speaker segments from video index."""
    speakers = {}
    
    try:
        # Get transcript segments first
        videos = video_index.get('videos', [])
        if not videos:
            logger.warning("No videos found in index")
            return speakers
            
        transcript = videos[0].get('insights', {}).get('transcript', [])
        if not transcript:
            logger.warning("No transcript found in video insights")
            return speakers
            
        # Process each transcript segment
        for segment in transcript:
            speaker_id = str(segment.get('speakerId', 'unknown'))
            if speaker_id not in speakers:
                speakers[speaker_id] = {
                    'name': f'المتحدث #{speaker_id}',
                    'segments': [],
                    'total_duration': 0
                }
            
            instances = segment.get('instances', [])
            if instances:
                instance = instances[0]
                start = instance.get('start', '')
                end = instance.get('end', '')
                
                # Get Arabic text
                text = segment.get('textInOriginalLanguage', '')  # Try original Arabic text first
                if not text:
                    text = segment.get('text', '')  # Fallback to regular text
                
                if text and start and end:
                    speakers[speaker_id]['segments'].append({
                        'text': text,
                        'start': start,
                        'end': end
                    })
                    
                    # Calculate duration
                    start_time = parse_time(start)
                    end_time = parse_time(end)
                    speakers[speaker_id]['total_duration'] += (end_time - start_time)
        
        # Convert total duration to HH:MM:SS format for each speaker
        for speaker in speakers.values():
            total_seconds = speaker['total_duration']
            hours = int(total_seconds // 3600)
            minutes = int((total_seconds % 3600) // 60)
            seconds = int(total_seconds % 60)
            speaker['total_duration'] = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        
        # Log speaker detection results
        logger.info(f"\nSpeaker Detection Results")
        logger.info(f"Total speakers detected: {len(speakers)}")
        for speaker_id, speaker in speakers.items():
            logger.info(f"Speaker {speaker_id}: {len(speaker['segments'])} segments, duration: {speaker['total_duration']}")
        
        return speakers
        
    except Exception as e:
        logger.error(f"Error extracting speaker segments: {e}")
        raise

def get_video_info(video_id):
    """Get video information from Video Indexer."""
    try:
        access_token = get_access_token()
        index_url = f"https://api.videoindexer.ai/{location}/Accounts/{account_id}/Videos/{video_id}/Index?language=ar-AE"
        headers = {"Authorization": f"Bearer {access_token}"}
        
        response = requests.get(index_url, headers=headers)
        if response.status_code == 200:
            return response.json()
        else:
            raise Exception(f"Failed to get video info: {response.status_code}")
    except Exception as e:
        logger.error(f"Error getting video info: {e}")
        raise

def parse_time(time_str):
    """Parse time string to seconds."""
    return date_parser.parse(time_str).timestamp()