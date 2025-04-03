# 绑定ip地址，本机  127.0.0.1  ，局域网填写对应地址
HOST='127.0.0.1'
# 端口
PORT=5080
# 限制上传大小为 1000MB
FILE_MAX_SIZE=1000 * 1024 * 1024  
# 允许的文件类型
ALLOWED_EXTENSIONS = {'mp3', 'mp4', 'mpeg', 'mpga', 'm4a', 'wav', 'webm', 'aac', 'flac','mov','mkv','avi'} 

import os,time,sys,re,threading,json
from pathlib import Path
from datetime import timedelta,datetime
ROOT_DIR=Path(__file__).parent.as_posix()
TMPDIR=(Path(__file__).parent/'tmp').as_posix()
Path(TMPDIR).mkdir(exist_ok=True)
LOGSDIR=(Path(__file__).parent/'logs').as_posix()
Path(LOGSDIR).mkdir(exist_ok=True)

NLTK_DATA=(Path(__file__).parent/'models/nltk').as_posix()
Path(NLTK_DATA).mkdir(exist_ok=True,parents=True)

# ffmpeg
if sys.platform == 'win32':
    os.environ['PATH'] = ROOT_DIR + f';{ROOT_DIR}\\ffmpeg;' + os.environ['PATH']
else:
    os.environ['PATH'] = ROOT_DIR + f':{ROOT_DIR}/ffmpeg:' + os.environ['PATH']
os.environ['NLTK_DATA']=NLTK_DATA

import subprocess
import tempfile
import shutil
from flask import Flask, request, jsonify, Response,render_template
from werkzeug.utils import secure_filename
import math,torch,logging
from logging.handlers import RotatingFileHandler
from waitress import serve
from pydub import AudioSegment
import nltk
nltk.data.path.insert(0, NLTK_DATA)
# --- 配置 ---
logging.basicConfig(level=logging.INFO) # 设置日志记录
file_handler = RotatingFileHandler(LOGSDIR+f'/{datetime.now().strftime("%Y%m%d")}.log', maxBytes=1024 * 1024, backupCount=5)
# 创建日志的格式
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
# 设置文件处理器的级别和格式
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(formatter)
# 将文件处理器添加到日志记录器中

app = Flask(__name__)
app.logger.setLevel(logging.INFO) 
app.logger.addHandler(file_handler)
app.config['UPLOAD_FOLDER'] = TMPDIR
app.config['MAX_CONTENT_LENGTH'] = FILE_MAX_SIZE
app.config['JSON_AS_ASCII'] = False

# 根据语音活动提前切割
def cut_audio(audio_file):
    audio = AudioSegment.from_file(audio_file,format=audio_file[-3:])
    if len(audio)<30:
        length=len(audio)
        return [{
            "start_time":0,
            "end_time":length,
            "file":audio_file,
            "text":"",
            "time":ms_to_time_string(ms=0)+' --> '+ms_to_time_string(ms=length)
        }]
    sampling_rate=16000
    from faster_whisper.audio import decode_audio
    from faster_whisper.vad import (
        VadOptions,
        get_speech_timestamps
    )

    def convert_to_milliseconds(timestamps):
        milliseconds_timestamps = []
        for timestamp in timestamps:
            milliseconds_timestamps.append(
                {
                    "start": int(round(timestamp["start"] / sampling_rate * 1000)),
                    "end": int(round(timestamp["end"] / sampling_rate * 1000)),
                }
            )

        return milliseconds_timestamps
    vad_p={
        "min_speech_duration_ms":2000,
        "max_speech_duration_s":15,
        "min_silence_duration_ms":250,
        "speech_pad_ms":200
    }
    speech_chunks=get_speech_timestamps(decode_audio(audio_file, sampling_rate=sampling_rate),vad_options=VadOptions(**vad_p))
    speech_chunks=convert_to_milliseconds(speech_chunks)


    dir_name = f"{TMPDIR}/{time.time()}"
    Path(dir_name).mkdir(parents=True, exist_ok=True)
    print(f"Saving segments to {dir_name}")
    data=[]
    
    for it in speech_chunks:
        start_ms, end_ms=it['start'],it['end']
        chunk = audio[start_ms:end_ms]
        file_name=f"{dir_name}/{start_ms}_{end_ms}.wav"
        chunk.export(file_name, format="wav")
        data.append({
            "start_time":start_ms,
            "end_time":end_ms,
            "file":file_name,
            "text":"",
            "time":ms_to_time_string(ms=start_ms)+' --> '+ms_to_time_string(ms=end_ms)
        })
    
    return data
    
def recogn(file,language=None,model_name="base"):
    import dolphin

    waveform = dolphin.load_audio(file)
    model = dolphin.load_model(model_name, ROOT_DIR+"/models", "cuda" if torch.cuda.is_available() else "cpu")
    langlist=language.split('-') if language else [None,None]
    print(f'{langlist=}')
    result = model(waveform, lang_sym=langlist[0], region_sym=langlist[1],padding_speech=False)
    return result.text
    
    

def allowed_file(filename):
    """检查文件扩展名是否允许"""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def ms_to_time_string(ms):
    """将毫秒转换为 SRT 时间格式 HH:MM:SS,ms"""
    if not isinstance(ms, (int, float)) or ms < 0:
        ms = 0 

    seconds = math.floor(ms / 1000)
    milliseconds = round(ms % 1000)
    minutes = math.floor(seconds / 60)
    seconds = seconds % 60
    hours = math.floor(minutes / 60)
    minutes = minutes % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"

def run_ffmpeg(input_file, output_file):
    """使用 ffmpeg 将输入文件转换为 pcm_s16le WAV 格式"""
    command = [
        'ffmpeg',
        '-y',
        "-hide_banner", 
        "-ignore_unknown",
        '-i', input_file,
        '-vn',              # 禁用视频录制
        '-acodec', 'pcm_s16le', # 设置音频编解码器为 pcm_s16le
        '-ar', '16000',      # 设置音频采样率为 16kHz (常见的 ASR 采样率)
        '-ac', '1',          # 设置音频通道为单声道
        output_file
    ]
    try:
        app.logger.info(f"Running FFmpeg command: {' '.join(command)}")
        result = subprocess.run(command, check=True, capture_output=True, text=True,encoding='utf-8',errors='ignore')
        app.logger.info(f"FFmpeg conversion successful for {input_file}")
        return True, None
    except subprocess.CalledProcessError as e:
        error_message = f"FFmpeg failed for {input_file}: {e.stderr}"
        app.logger.error(error_message)
        return False, error_message
    except FileNotFoundError:
        error_message = "FFmpeg command not found. Please ensure ffmpeg is installed and in your PATH."
        app.logger.error(error_message)
        return False, error_message

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/v1/audio/transcriptions', methods=['POST'])
def audio_transcriptions():
    """处理音频转录请求"""
    temp_dir = None 
    try:
        if 'file' not in request.files:
            app.logger.warning("No file part in the request")
            return jsonify({"error": "No file part"}), 400

        file = request.files['file']

        if file.filename == '':
            app.logger.warning("No selected file")
            return jsonify({"error": "No selected file"}), 400

        if not file or not allowed_file(file.filename):
            app.logger.warning(f"File type not allowed: {file.filename}")
            return jsonify({"error": "File type not allowed"}), 400

        language = request.form.get('language', None) 
        model = request.form.get('model', "base") 
        response_format = request.form.get('response_format', "json")
        app.logger.info(f"Received request: language='{language}', filename='{file.filename}'")

        filename = f'{time.time()}_'+secure_filename(file.filename)
        original_filepath = f'{TMPDIR}/raw_{filename}'
        file.save(original_filepath)
        app.logger.info(f"Saved original file to: {original_filepath}")


        wav_filename = os.path.splitext(filename)[0] + ".wav"
        wav_filepath = f'{TMPDIR}/pcms16le_{wav_filename}'
        success, ffmpeg_error = run_ffmpeg(original_filepath, wav_filepath)
        if not success:
            return jsonify({"error": f"Audio conversion failed. {ffmpeg_error}"}), 500


        print(f'{wav_filepath=}')
        segments = cut_audio(wav_filepath)
        if not segments:
             app.logger.warning(f"cut_audio returned no segments for {wav_filepath}")
             return jsonify({"error": "Audio segmentation failed or produced no segments"}), 500


        processed_segments = []
        for i, segment in enumerate(segments):
            segment_file = segment.get("file")
            if not segment_file or not os.path.exists(segment_file):
                 app.logger.error(f"Segment file path missing or file not found in segment data: {segment}")
                 continue

            try:
                print(f'{language=},{segment_file=}')
                recognized_text = recogn(segment_file, language,model)
                print(f'{recognized_text=}')
                segment["text"] = recognized_text.strip() # 去除首尾空格
                processed_segments.append(segment)
            except Exception as e:
                app.logger.error(f"Recognition failed for segment {i+1} ({segment_file}): {e}", exc_info=True)
                return jsonify({"error": f"Recognition failed for segment {i+1}. Error: {e}"}), 500

        srt_output = []
        for i, segment in enumerate(processed_segments):
            if not segment.get("text"):
                continue
            text=(re.sub(r'<[a-zA-Z0-9\.]+>','',segment["text"])).strip()
            if response_format=="srt":
                srt_output.append(str(i + 1)) 
                srt_output.append(segment["time"])
                srt_output.append(text)
                srt_output.append("")
            else:
                srt_output.append(text) # 识别文本

        srt_string = "\n".join(srt_output)

        # 返回
        app.logger.info(f"Successfully processed '{filename}'. Returning SRT response.")
        print({"text":srt_string})
        if response_format=='json':
            return Response(
                response=json.dumps({"text":srt_string}, ensure_ascii=False),
                status=200,
                mimetype='application/json; charset=utf-8'
            )
        return Response(srt_string, mimetype='text/plain', status=200)

    except Exception as e:
        app.logger.error(f"An unexpected error occurred: {e}", exc_info=True)
        return jsonify({"error": f"An internal server error occurred: {str(e)}"}), 500
    finally:
        try:
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except:
            pass

def openwebpage():
    import webbrowser
    try:
        time.sleep(2)
        webbrowser.open_new_tab(f'http://127.0.0.1:{PORT}')
    except Exception:
        pass


if __name__ == '__main__':
    threading.Thread(target=openwebpage).start()
    try:
        serve(app, host=HOST, port=PORT,threads=4,channel_timeout=7300)
    except Exception as e:
        logger.error(f"An error occurred: {str(e)}")
        logger.error(traceback.format_exc())
