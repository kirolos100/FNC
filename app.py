import os
from flask import Flask, render_template, request, jsonify, send_file, url_for, redirect, send_from_directory, after_this_request
from werkzeug.utils import secure_filename
import json
from datetime import timedelta
import video_processor
import requests
import shutil
from azure.storage.blob import BlobServiceClient
import time

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024 * 1024  # 1GB max file size
app.config['PROCESSED_FOLDER'] = 'static/processed'

# Azure Blob Storage settings
BLOB_CONNECTION_STRING = os.getenv('BLOB_CONNECTION_STRING', "DefaultEndpointsProtocol=https;AccountName=generalvideostorage;AccountKey=J2CYyxoiM5v8WnW4HOsPf+MXjvIAnL59ubJb3RtAXAVHvLe3c4gYYlD7j7k3efpCE17fw247ynfy+AStVWlEdA==;EndpointSuffix=core.windows.net")
BLOB_CONTAINER_NAME = os.getenv('BLOB_CONTAINER_NAME', "generalvideostorage")

# Initialize the BlobServiceClient
blob_service_client = BlobServiceClient.from_connection_string(BLOB_CONNECTION_STRING)
container_client = blob_service_client.get_container_client(BLOB_CONTAINER_NAME)

# Ensure required folders exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['PROCESSED_FOLDER'], exist_ok=True)
os.makedirs('templates', exist_ok=True)

# Store processing status in memory
processing_status = {}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in {'mp4', 'avi', 'mov', 'mkv'}

def update_processing_status(video_id, status, message):
    processing_status[video_id] = {
        'status': status,
        'message': message,
        'timestamp': time.time()
    }

def parse_time(time_str):
    """Parse time string to seconds, handling various formats."""
    try:
        parts = time_str.split(':')
        
        if len(parts) == 3:  # HH:MM:SS or HH:MM:SS.ss
            hours, minutes, seconds = parts
        elif len(parts) == 2:  # MM:SS or MM:SS.ss
            hours = '0'
            minutes, seconds = parts
        else:  # SS or SS.ss
            hours = '0'
            minutes = '0'
            seconds = parts[0]
        
        # Handle decimal seconds
        if '.' in seconds:
            seconds, decimal = seconds.split('.')
            decimal = float(f'0.{decimal}')
        else:
            decimal = 0
            
        total = int(hours) * 3600 + int(minutes) * 60 + int(seconds) + decimal
        return total
    except Exception as e:
        print(f"Error parsing time {time_str}: {str(e)}")
        return 0

def load_insights(video_id):
    """Load insights from JSON file with Arabic transcript."""
    try:
        insights_file = f'insights_{video_id}_full.json'
        if os.path.exists(insights_file):
            with open(insights_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        else:
            # If file doesn't exist, fetch from API
            return video_processor.get_video_info(video_id)
    except Exception as e:
        raise Exception(f"Failed to load insights: {str(e)}")

def get_transcript_data(video_id):
    """Get transcript data in chronological order with Arabic text."""
    insights = load_insights(video_id)
    transcript = []
    
    try:
        videos = insights.get('videos', [])
        if videos and len(videos) > 0:
            transcript_segments = videos[0].get('insights', {}).get('transcript', [])
            
            for segment in transcript_segments:
                instances = segment.get('instances', [])
                if instances:
                    instance = instances[0]
                    speaker_id = str(segment.get('speakerId', 'unknown'))
                    
                    # Get Arabic text
                    text = segment.get('textInOriginalLanguage', '')  # Try original Arabic text first
                    if not text:
                        text = segment.get('text', '')  # Fallback to regular text
                    
                    if text:
                        transcript.append({
                            'speaker_name': f'المتحدث #{speaker_id}',
                            'text': text,
                            'start': instance.get('start', ''),
                            'end': instance.get('end', '')
                        })
    except Exception as e:
        print(f"Error processing transcript: {str(e)}")
        return []
    
    # Sort by start time
    transcript.sort(key=lambda x: parse_time(x.get('start', '0')))
    return transcript

def upload_to_blob_storage(file_path, video_id, speaker_id, max_retries=3):
    """Upload a file to Azure Blob Storage and return the URL."""
    from azure.storage.blob import BlobBlock
    import uuid
    import time
    import math

    def upload_with_retry(blob_client, file_data, retries=0):
        try:
            # Use chunked upload for large files
            chunk_size = 4 * 1024 * 1024  # 4MB chunks
            total_size = len(file_data)
            block_list = []
            
            # Upload chunks
            for i in range(0, total_size, chunk_size):
                chunk = file_data[i:i + chunk_size]
                block_id = str(uuid.uuid4())
                block_list.append(BlobBlock(block_id=block_id))
                
                # Retry logic for each chunk
                retry_count = 0
                while retry_count < 3:
                    try:
                        print(f"Uploading chunk {i//chunk_size + 1} of {math.ceil(total_size/chunk_size)}")
                        blob_client.stage_block(block_id=block_id, data=chunk)
                        break
                    except Exception as e:
                        retry_count += 1
                        if retry_count == 3:
                            raise
                        time.sleep(1)  # Wait before retrying
            
            # Commit the blocks
            blob_client.commit_block_list(block_list)
            return True
            
        except Exception as e:
            if retries < max_retries:
                print(f"Attempt {retries + 1} failed, retrying... Error: {str(e)}")
                time.sleep(2 ** retries)  # Exponential backoff
                return upload_with_retry(blob_client, file_data, retries + 1)
            else:
                raise
    
    try:
        # Create a unique blob name
        blob_name = f"{video_id}/speaker_{speaker_id}_full.mp4"
        
        # Get blob client
        blob_client = container_client.get_blob_client(blob_name)
        
        # Read file data
        with open(file_path, "rb") as file:
            file_data = file.read()
        
        # Upload with retry logic
        if upload_with_retry(blob_client, file_data):
            return blob_client.url
            
    except Exception as e:
        print(f"Error uploading to blob storage: {str(e)}")
        raise

@app.route('/')
def index():
    error = request.args.get('error')
    return render_template('index.html', error=error)

@app.route('/process_video_id', methods=['POST'])
def process_video_id():
    try:
        video_id = request.form.get('video_id')
        if not video_id:
            return redirect(url_for('index', error='لم يتم تحديد معرف الفيديو'))
        
        # Process video using video_processor
        video_id, speakers = video_processor.process_video(None, video_id=video_id)
        return redirect(url_for('view_video', video_id=video_id))
    except Exception as e:
        return redirect(url_for('index', error=str(e)))

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'video' not in request.files:
        return jsonify({'error': 'لم يتم تحديد ملف فيديو'}), 400
    
    file = request.files['video']
    if file.filename == '':
        return jsonify({'error': 'لم يتم اختيار ملف'}), 400
    
    if file and allowed_file(file.filename):
        try:
            # Create uploads directory if it doesn't exist
            if not os.path.exists(app.config['UPLOAD_FOLDER']):
                os.makedirs(app.config['UPLOAD_FOLDER'])
            
            # Save the file temporarily first
            temp_filepath = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(file.filename))
            file.save(temp_filepath)
            
            # Get access token
            access_token = video_processor.get_access_token()
            
            # Upload video using the file path
            video_processor_response = video_processor.upload_video(temp_filepath, access_token)
            video_id = video_processor_response
            
            # Rename the saved file with video_id
            filename = f'video_{video_id}.mp4'
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            os.rename(temp_filepath, filepath)
            
            try:
                # Process video using existing video_id
                _, speakers = video_processor.process_video(filepath, video_id=video_id)
                
                # Start tracking processing status
                update_processing_status(video_id, 'uploading_speakers', 'جاري رفع مقاطع المتحدثين...')
                
                # Upload each speaker's video to blob storage
                speaker_urls = {}
                total_speakers = len(speakers)
                
                for idx, speaker_id in enumerate(speakers, 1):
                    video_path = f'المتحدث #{speaker_id}_full.mp4'
                    if os.path.exists(video_path):
                        update_processing_status(
                            video_id, 
                            'uploading_speakers', 
                            f'جاري رفع فيديو المتحدث {idx} من {total_speakers}'
                        )
                        try:
                            # Upload to blob storage with progress tracking
                            blob_url = upload_to_blob_storage(video_path, video_id, speaker_id)
                            speaker_urls[speaker_id] = blob_url
                            
                            # Save URLs after each successful upload
                            with open(f'speaker_urls_{video_id}.json', 'w', encoding='utf-8') as f:
                                json.dump(speaker_urls, f, ensure_ascii=False, indent=2)
                                
                        except Exception as e:
                            print(f"Failed to upload speaker {speaker_id} video: {str(e)}")
                
                # Update final status
                update_processing_status(video_id, 'completed', 'اكتملت المعالجة بنجاح')
                
                return jsonify({
                    'success': True,
                    'video_id': video_id,
                    'message': 'تم رفع ومعالجة الفيديو بنجاح'
                })
                
            except Exception as e:
                # Update error status
                if 'video_id' in locals():
                    update_processing_status(video_id, 'failed', str(e))
                
                # If processing fails, clean up the uploaded file
                if os.path.exists(filepath):
                    os.remove(filepath)
                return jsonify({'error': str(e)}), 500
            
        except Exception as e:
            return jsonify({'error': f'خطأ في رفع الملف: {str(e)}'}), 500
    
    return jsonify({'error': 'نوع الملف غير مدعوم'}), 400

@app.route('/api/processing-status/<video_id>')
def get_processing_status(video_id):
    """Get the current processing status for a video"""
    if video_id not in processing_status:
        return jsonify({
            'status': 'unknown',
            'message': 'لم يتم العثور على معرف الفيديو'
        })
    
    status = processing_status[video_id]
    return jsonify({
        'status': status['status'],
        'message': status['message']
    })

@app.route('/video')
def video_redirect():
    video_id = request.args.get('video_id')
    if not video_id:
        return redirect(url_for('index', error='لم يتم تحديد معرف الفيديو'))
    return redirect(url_for('view_video', video_id=video_id))

@app.route('/video/<video_id>')
def view_video(video_id):
    try:
        insights = load_insights(video_id)
        speakers = {}
        
        # Load blob URLs if available
        speaker_urls = {}
        try:
            with open(f'speaker_urls_{video_id}.json', 'r', encoding='utf-8') as f:
                speaker_urls = json.load(f)
        except:
            pass
        
        # Get transcript segments grouped by speaker
        videos = insights.get('videos', [])
        if videos:
            transcript = videos[0].get('insights', {}).get('transcript', [])
            
            for segment in transcript:
                speaker_id = str(segment.get('speakerId', 'unknown'))
                if speaker_id not in speakers:
                    speakers[speaker_id] = {
                        'name': f'المتحدث #{speaker_id}',
                        'segments': [],
                        'total_duration': 0
                    }
                
                instances = segment.get('instances', [{}])[0]
                start = instances.get('start', '')
                end = instances.get('end', '')
                
                # Get Arabic text
                text = segment.get('textInOriginalLanguage', '')
                if not text:
                    text = segment.get('text', '')
                
                speakers[speaker_id]['segments'].append({
                    'text': text,
                    'start': start,
                    'end': end
                })
                
                # Calculate duration
                if start and end:
                    start_time = parse_time(start)
                    end_time = parse_time(end)
                    speakers[speaker_id]['total_duration'] += end_time - start_time
            
            # Convert total duration to HH:MM:SS format
            for speaker in speakers.values():
                total_seconds = speaker['total_duration']
                hours = int(total_seconds // 3600)
                minutes = int((total_seconds % 3600) // 60)
                seconds = int(total_seconds % 60)
                speaker['total_duration'] = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        
        return render_template('speakers.html', video_id=video_id, speakers=speakers)
    except FileNotFoundError as e:
        return redirect(url_for('index', error=str(e)))
    except Exception as e:
        return redirect(url_for('index', error=f"خطأ في معالجة الفيديو: {str(e)}"))

@app.route('/transcript/<video_id>')
def view_transcript(video_id):
    try:
        transcript = get_transcript_data(video_id)
        if not transcript:
            return redirect(url_for('index', error='لم يتم العثور على نص للفيديو'))
            
        # Get video source for the template
        video_source = get_video_source(video_id)
        if video_source and video_source.startswith('http'):
            video_url = video_source
        else:
            video_url = url_for('get_full_video', video_id=video_id)
            
        return render_template('transcript.html', 
                             video_id=video_id, 
                             transcript=transcript,
                             video_url=video_url)
    except Exception as e:
        print(f"Error in view_transcript: {str(e)}")
        return redirect(url_for('index', error=str(e)))

@app.route('/api/speaker_video/<video_id>/<speaker_id>')
def get_speaker_video(video_id, speaker_id):
    try:
        # Check local file first for faster access
        video_path = f'المتحدث #{speaker_id}_full.mp4'
        if os.path.exists(video_path):
            return send_file(video_path, mimetype='video/mp4')
        
        # If not in local storage, try to get from blob storage
        try:
            blob_name = f"{video_id}/speaker_{speaker_id}_full.mp4"
            blob_client = container_client.get_blob_client(blob_name)
            
            # Download to a temporary file
            temp_path = os.path.join(app.config['PROCESSED_FOLDER'], f'temp_speaker_{speaker_id}_{video_id}.mp4')
            with open(temp_path, "wb") as video_file:
                stream = blob_client.download_blob()
                video_file.write(stream.readall())
            
            # Send the file and clean up
            @after_this_request
            def cleanup(response):
                try:
                    os.remove(temp_path)
                except Exception as e:
                    print(f"Error cleaning up temp file: {e}")
                return response
                
            return send_file(temp_path, mimetype='video/mp4')
            
        except Exception as e:
            print(f"Error retrieving from blob storage: {e}")
            return jsonify({'error': 'لم يتم العثور على ملف الفيديو'}), 404
            
    except Exception as e:
        print(f"Error in get_speaker_video: {e}")
        return jsonify({'error': str(e)}), 404

@app.route('/api/transcript/<video_id>/download')
def download_transcript(video_id):
    try:
        transcript = get_transcript_data(video_id)
        if not transcript:
            return jsonify({'error': 'لم يتم العثور على نص للفيديو'}), 404
        
        # Create transcript file
        output_file = f'transcript_{video_id}.txt'
        with open(output_file, 'w', encoding='utf-8') as f:
            for entry in transcript:
                f.write(f"{entry['speaker_name']} ({entry['start']} - {entry['end']}):\n")
                f.write(f"{entry['text']}\n\n")
        
        return send_file(
            output_file,
            as_attachment=True,
            download_name=f'transcript_{video_id}.txt',
            mimetype='text/plain'
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def get_video_source(video_id):
    """Get the video source in order of preference:
    1. Original uploaded video in uploads folder with video_id
    2. Azure Video Indexer stream
    3. First speaker's video
    """
    try:
        # Check uploads folder for video_id specific file first
        uploads_dir = app.config['UPLOAD_FOLDER']
        video_file = os.path.join(uploads_dir, f'video_{video_id}.mp4')
        if os.path.exists(video_file):
            return video_file
        
        # Try to get Azure Video Indexer stream URL
        try:
            access_token = video_processor.get_access_token()
            player_url = f"https://api.videoindexer.ai/{video_processor.location}/Accounts/{video_processor.account_id}/Videos/{video_id}/SourceFile/DownloadUrl"
            headers = {"Authorization": f"Bearer {access_token}"}
            response = requests.get(player_url, headers=headers, verify=False)
            
            if response.status_code == 200:
                return response.json()
        except Exception as e:
            print(f"Error getting Azure stream URL: {e}")
        
        # Fall back to first speaker's video
        speaker_video = f'المتحدث #1_full.mp4'
        if os.path.exists(speaker_video):
            return speaker_video
            
        # Try other speaker videos
        for i in range(2, 10):
            speaker_video = f'المتحدث #{i}_full.mp4'
            if os.path.exists(speaker_video):
                return speaker_video
                
        return None
    except Exception as e:
        print(f"Error getting video source: {e}")
        return None

@app.route('/api/full_video/<video_id>')
def get_full_video(video_id):
    try:
        video_source = get_video_source(video_id)
        
        if not video_source:
            return jsonify({'error': 'لم يتم العثور على ملف الفيديو'}), 404
            
        # If it's a URL from Azure, redirect to it
        if video_source.startswith('http'):
            return redirect(video_source)
            
        # Otherwise serve the local file
        return send_file(video_source, mimetype='video/mp4')
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)