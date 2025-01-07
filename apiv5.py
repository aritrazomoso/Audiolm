from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Request
import whisper
import torch
import io
from fastapi.responses import HTMLResponse, StreamingResponse
from pydub import AudioSegment
from io import BytesIO
import base64
from fastapi.templating import Jinja2Templates
import tempfile
import os
import asyncio
from openai import OpenAI
from dotenv import load_dotenv
from typing import AsyncGenerator, List, Tuple
import websockets
import json
from scipy import signal
import base64
import numpy as np
import logging
import soundfile as sf
from pyannote.audio import Model
from pyannote.audio.pipelines import VoiceActivityDetection
import datetime
from pathlib import Path
import wave
from time import time
import noisereduce as nr
from faster_whisper import WhisperModel

import requests

API_URL = "https://api-inference.huggingface.co/models/openai/whisper-large-v3-turbo"
# API_URL = "https://api-inference.huggingface.co/models/openai/whisper-medium.en"
headers = {"Authorization": "Bearer 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"}


def convert_audio_text(filename):
    with open(filename, "rb") as f:
        data = f.read()
        response = requests.post(API_URL, headers=headers, data=data)
        return response.json()


load_dotenv()

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

voice_id = "pNInz6obpgDQGcFmaJgB"
model_id = "eleven_turbo_v2_5"

client = OpenAI(
    api_key=os.environ["GROQ_API_KEY"],  # This is the default and can be omitted
    base_url="https://api.groq.com/openai/v1",
)


async def listen(websocket) -> AsyncGenerator[bytes, None]:
    """Listen to the websocket for audio data and stream it."""
    while True:
        try:
            message = await websocket.recv()
            data = json.loads(message)
            if data.get("audio"):
                yield base64.b64decode(data["audio"])
            elif data.get("isFinal"):
                break
        except websockets.exceptions.ConnectionClosed as e:
            print("Connection closed")
            print(e)
            break


async def chatgpt_send_to_websocket(websocket, user_query: str):
    """Send text chunks to websocket from ChatGPT response."""
    chat_completion = client.chat.completions.create(
        messages=[
            {
                "role": "system",
                "content": "You are a helpful assistant. Please assist the user with their query.",
            },
            {
                "role": "user",
                "content": user_query,
            },
        ],
        model="llama3-8b-8192",
        stream=True,
    )

    for chunk in chat_completion:
        if chunk.choices[0].delta.content is not None:
            if chunk.choices[0].delta.content != "":
                await websocket.send(
                    json.dumps({"text": chunk.choices[0].delta.content})
                )
                print("Sending chunk ", {"text": chunk.choices[0].delta.content})
        else:
            await websocket.send(json.dumps({"text": ""}))
            print("End of audio stream")
    return "Complete"


async def generate_audio_stream(user_query: str) -> AsyncGenerator[bytes, None]:
    """Generate audio stream from text."""
    uri = f"wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream-input?model_id={model_id}"

    async with websockets.connect(uri) as websocket:
        # Send initial configuration
        await websocket.send(
            json.dumps(
                {
                    "text": " ",
                    "voice_settings": {
                        "stability": 0.5,
                        "similarity_boost": 0.8,
                        "use_speaker_boost": False,
                    },
                    "generation_config": {
                        "chunk_length_schedule": [90, 120, 160, 250, 290]
                    },
                    "xi_api_key": ELEVENLABS_API_KEY,
                    # "flush": True,
                }
            )
        )

        # Create task for sending text
        send_task = asyncio.create_task(
            chatgpt_send_to_websocket(websocket, user_query)
        )

        # Stream audio chunks
        async for chunk in listen(websocket):
            yield chunk

        await send_task


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# Set up the Jinja2 template renderer
templates = Jinja2Templates(directory="templates")


def load_whisper_model(model_type: str = "base"):
    """
    Load and return the Whisper model.
    Args:
        model_type (str): The type of Whisper model to load ("tiny", "base", "small", "medium", "large")
    """
    return whisper.load_model(model_type)


async def listen_numpy(websocket: WebSocket) -> AsyncGenerator[np.ndarray, None]:
    """
    Listen to the websocket for audio data and stream it.
    Yields processed numpy arrays from the audio data.
    """
    while True:
        try:
            message = await websocket.receive_text()
            data = json.loads(message)

            if data.get("audio"):
                # Decode base64 audio data
                audio_bytes = base64.b64decode(data["audio"])
                # Convert bytes to numpy array
                audio_data = np.frombuffer(audio_bytes, dtype=np.int16)
                # Convert to float32 and normalize
                audio_float = audio_data.astype(np.float32) / 32768.0
                yield audio_float
            elif data.get("isFinal"):
                logger.info("Received final message")
                break
        except Exception as e:
            logger.error(f"Error in listen: {str(e)}")
            break


class AudioDebugger:
    def __init__(self, debug_folder="debug_audio"):
        """
        Initialize the audio debugger with a specific folder for saving files.

        Args:
            debug_folder (str): The folder where debug audio files will be saved
        """
        self.debug_folder = Path(debug_folder)
        self.create_debug_folder()
        self.session_folder = None
        self.chunk_counter = 0

    def create_debug_folder(self):
        """Create the main debug folder if it doesn't exist."""
        try:
            self.debug_folder.mkdir(parents=True, exist_ok=True)
            logger.info(f"Debug folder created/verified at: {self.debug_folder}")
        except Exception as e:
            logger.error(f"Error creating debug folder: {str(e)}")

    def start_new_session(self):
        """
        Start a new debugging session with timestamp-based folder.
        Returns the session folder path.
        """
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_folder = self.debug_folder / timestamp
        self.chunk_counter = 0

        try:
            self.session_folder.mkdir(parents=True, exist_ok=True)
            logger.info(f"New debug session started at: {self.session_folder}")
            return self.session_folder
        except Exception as e:
            logger.error(f"Error creating session folder: {str(e)}")
            return None

    def save_audio_chunk(
        self,
        audio_data: np.ndarray,
        sample_rate: int = 44100,
        prefix: str = "chunk",
        format: str = "flac",
    ):
        """
        Save an audio chunk with debugging information.

        Args:
            audio_data (np.ndarray): The audio data to save
            sample_rate (int): The sample rate of the audio
            prefix (str): Prefix for the filename
            format (str): Audio format to save ('flac' or 'wav')

        Returns:
            Path: Path to the saved file
        """
        if self.session_folder is None:
            self.start_new_session()

        self.chunk_counter += 1

        # Create filename with metadata
        timestamp = datetime.datetime.now().strftime("%H%M%S")
        filename = f"{prefix}_{timestamp}_{self.chunk_counter:04d}.{format}"
        filepath = self.session_folder / filename

        try:
            # Save audio file
            if format == "flac":
                sf.write(
                    filepath, audio_data, sample_rate, format="FLAC", subtype="PCM_24"
                )
            else:  # wav
                sf.write(
                    filepath, audio_data, sample_rate, format="WAV", subtype="PCM_24"
                )

            # Save metadata
            meta_filepath = filepath.with_suffix(".meta.txt")
            with open(meta_filepath, "w") as f:
                f.write(f"Timestamp: {timestamp}\n")
                f.write(f"Sample Rate: {sample_rate}\n")
                f.write(f"Shape: {audio_data.shape}\n")
                f.write(f"Max Amplitude: {np.max(np.abs(audio_data))}\n")
                f.write(f"Mean Amplitude: {np.mean(np.abs(audio_data))}\n")

            logger.info(f"Saved debug audio chunk to: {filepath}")
            return filepath

        except Exception as e:
            logger.error(f"Error saving debug audio chunk: {str(e)}")
            return None

    def save_raw_bytes(
        self,
        audio_bytes: bytes,
        sample_rate: int = 44100,
        channels: int = 1,
        sampwidth: int = 4,
    ):
        """
        Save raw audio bytes for debugging.

        Args:
            audio_bytes (bytes): Raw audio bytes
            sample_rate (int): Sample rate of the audio
            channels (int): Number of audio channels
            sampwidth (int): Sample width in bytes

        Returns:
            Path: Path to the saved file
        """
        if self.session_folder is None:
            self.start_new_session()

        self.chunk_counter += 1

        # Create filename
        timestamp = datetime.datetime.now().strftime("%H%M%S")
        filename = f"raw_{timestamp}_{self.chunk_counter:04d}.wav"
        filepath = self.session_folder / filename

        try:
            with wave.open(str(filepath), "wb") as wav_file:
                wav_file.setnchannels(channels)
                wav_file.setsampwidth(sampwidth)
                wav_file.setframerate(sample_rate)
                wav_file.writeframes(audio_bytes)

            logger.info(f"Saved raw audio bytes to: {filepath}")
            return filepath

        except Exception as e:
            logger.error(f"Error saving raw audio bytes: {str(e)}")
            return None


def preprocess_audio(audio_np: np.ndarray, sample_rate: int = 44100) -> np.ndarray:
    """
    Preprocess audio data to improve quality, reduce noise, and enhance clarity.
    """
    try:
        # Normalize audio
        audio_np = audio_np / np.max(np.abs(audio_np))

        # Apply band-pass filter
        low_cutoff = 300 / (sample_rate / 2)
        high_cutoff = 3400 / (sample_rate / 2)
        b, a = signal.butter(4, [low_cutoff, high_cutoff], "bandpass")
        audio_np = signal.filtfilt(b, a, audio_np)

        # Apply noise reduction
        noise_sample = audio_np[:sample_rate]  # Use the first second as noise sample
        audio_np = nr.reduce_noise(y=audio_np, sr=sample_rate, y_noise=noise_sample)

        return audio_np
    except Exception as e:
        logger.error(f"Error preprocessing audio: {str(e)}")
        return audio_np


def save_audio_to_flac(audio_np: np.ndarray, sample_rate: int = 44100) -> str:
    """
    Save audio numpy array to a temporary FLAC file with high quality settings.
    """
    try:
        temp_file = tempfile.NamedTemporaryFile(suffix=".flac", delete=False)
        sf.write(
            temp_file.name,
            audio_np,
            samplerate=sample_rate,
            format="FLAC",
            subtype="PCM_24",  # Use 24-bit FLAC encoding
        )
        temp_file.close()
        return temp_file.name
    except Exception as e:
        logger.error(f"Error saving audio to FLAC: {str(e)}")
        raise


async def listen_raw(websocket: WebSocket) -> AsyncGenerator[bytes, None]:
    """
    Listen to the websocket for raw audio data.
    Yields raw audio bytes directly.
    """
    while True:
        try:
            message = await websocket.receive_text()
            data = json.loads(message)

            if data.get("audio"):
                # Decode base64 audio data and yield raw bytes
                audio_bytes = base64.b64decode(data["audio"])
                yield audio_bytes
            elif data.get("isFinal"):
                logger.info("Received final message")
                break
        except Exception as e:
            logger.error(f"Error in listen_raw: {str(e)}")
            break


def transcribe_audio(model: whisper.Whisper, audio_data: np.ndarray) -> str:
    """
    Transcribe audio data using Whisper model.
    """
    try:
        result = model.transcribe(audio_data)
        # result = convert_audio_text(audio_data)
        return result["text"].strip()
    except Exception as e:
        logger.error(f"Transcription error: {str(e)}")
        return ""


def convert_audio_text_ndarray(filename) -> str:
    try:
        with open(filename, "rb") as f:
            data = f.read()
            response = requests.post(API_URL, headers=headers, data=data)
        return response
    except Exception as e:
        logger.error(f"Transcription error: {str(e)}")
        return ""


def convert_audio_text_groq(filename) -> str:

    try:
        audio_file = open(filename, "rb")
        transcription = client.audio.transcriptions.create(
            model="distil-whisper-large-v3-en", file=audio_file, temperature=0
        )
        return transcription.text
    except Exception as e:
        logger.error(f"Transcription error: {str(e)}")
        return ""


def transcribe_audio_with_groq(audio_data: np.ndarray) -> str:
    """
    Transcribe audio data using Hugging Face Whisper API.
    """
    try:
        start = time()
        # Preprocess the audio for better quality
        audio_data = preprocess_audio(audio_data)

        flac_file_path = save_audio_to_flac(audio_data)

        # Send the audio to the Hugging Face API
        res = convert_audio_text_groq(flac_file_path)
        end = time()
        logger.info(f"Transcribe time: {end-start}")
        return res
    except Exception as e:
        logger.error(f"HF transcription error: {str(e)}")
        return ""


model_size = "tiny"


def transcribe_audio_local(
    audio_data: np.ndarray,
    model: whisper.Whisper = whisper.load_model(model_size),
) -> str:
    try:
        result = model.transcribe(audio_data)
        return result["text"].strip()
    except Exception as e:
        logger.error(f"Transcription error: {str(e)}")
        return ""


def transcribe_audio_with_hf(audio_data: np.ndarray) -> str:
    """
    Transcribe audio data using Hugging Face Whisper API.
    """
    try:
        start = time()
        # Preprocess the audio for better quality
        audio_data = preprocess_audio(audio_data)

        flac_file_path = save_audio_to_flac(audio_data)

        # Send the audio to the Hugging Face API
        response = convert_audio_text_ndarray(flac_file_path)

        if response.status_code == 200:
            result = response.json()
            end = time()
            logger.info(f"Transcribe time: {end-start}")
            return result.get("text", "").strip()
        else:
            logger.error(f"HF API error: {response.status_code} - {response.text}")
            return ""
    except Exception as e:
        logger.error(f"HF transcription error: {str(e)}")
        return ""


def remove_overlap(prev, new):
    """Removes the common overlap (if any) between the end of the previous transcription
    and the beginning of the new transcription."""
    if prev and new:
        # Find the longest common suffix of prev and the prefix of new
        overlap_length = 0
        for i in range(min(len(prev), len(new))):
            if prev[-(i + 1) :] == new[: (i + 1)]:
                overlap_length = i + 1
            else:
                break
        return new[overlap_length:]
    return new


class AudioChunker:
    def __init__(
        self,
        access_token: str,
        sample_rate: int = 44100,
        min_chunk_duration: float = 1.0,
        max_chunk_duration: float = 5.0,
        min_speech_duration: float = 0.5,
    ):
        """
        Initialize the VAD-based audio chunker.

        Args:
            access_token: HuggingFace access token for pyannote
            sample_rate: Audio sample rate
            min_chunk_duration: Minimum duration of audio chunk to process
            max_chunk_duration: Maximum duration of audio chunk
            min_speech_duration: Minimum duration of speech to consider
        """
        self.sample_rate = sample_rate
        self.min_chunk_duration = min_chunk_duration
        self.max_chunk_duration = max_chunk_duration
        self.min_speech_duration = min_speech_duration

        # Initialize VAD pipeline
        model = Model.from_pretrained(
            "pyannote/voice-activity-detection", use_auth_token=access_token
        )
        self.vad = VoiceActivityDetection(segmentation=model)

        # Move to GPU if available
        if torch.cuda.is_available():
            self.vad = self.vad.to(torch.device("cuda"))

    def find_speech_regions(self, audio_np: np.ndarray) -> List[Tuple[float, float]]:
        """
        Find regions containing speech in the audio.

        Args:
            audio_np: Audio data as numpy array

        Returns:
            List of (start_time, end_time) tuples in seconds
        """
        # Ensure audio is float32 and normalized
        if audio_np.dtype != np.float32:
            audio_np = audio_np.astype(np.float32)
        if np.max(np.abs(audio_np)) > 1.0:
            audio_np = audio_np / np.max(np.abs(audio_np))

        # Run VAD
        vad_results = self.vad(
            {
                "waveform": torch.from_numpy(audio_np).unsqueeze(0),
                "sample_rate": self.sample_rate,
            }
        )

        # Extract speech segments
        speech_regions = []
        for speech_segment in vad_results.get_timeline().support():
            start_time = speech_segment.start
            end_time = speech_segment.end

            # Only include segments longer than minimum duration
            if end_time - start_time >= self.min_speech_duration:
                speech_regions.append((start_time, end_time))

        return speech_regions

    def get_chunk_regions(
        self, speech_regions: List[Tuple[float, float]], audio_duration: float
    ) -> List[Tuple[float, float]]:
        """
        Convert speech regions into chunk regions, ensuring minimum and maximum durations.
        """
        if not speech_regions:
            return []

        chunk_regions = []
        current_start = speech_regions[0][0]
        current_end = speech_regions[0][1]

        for start, end in speech_regions[1:]:
            # If gap between segments is small, merge them
            if start - current_end < 0.5:  # 500ms gap threshold
                current_end = end
            else:
                # If current chunk is long enough, add it
                if current_end - current_start >= self.min_chunk_duration:
                    chunk_regions.append((current_start, current_end))
                current_start = start
                current_end = end

            # If chunk exceeds max duration, split it
            if current_end - current_start > self.max_chunk_duration:
                chunk_regions.append(
                    (current_start, current_start + self.max_chunk_duration)
                )
                current_start = current_start + self.max_chunk_duration

        # Add final chunk if it's long enough
        if current_end - current_start >= self.min_chunk_duration:
            chunk_regions.append((current_start, current_end))

        return chunk_regions


@app.websocket("/ws/audio")
async def audio_ws1(websocket: WebSocket):
    await websocket.accept()
    logger.info(msg="Client connected")

    # Initialize audio debugger
    debugger = AudioDebugger(debug_folder="debug_audio")
    debugger.start_new_session()

    # Initialize chunker with your HuggingFace token
    chunker = AudioChunker(
        access_token=os.environ["HUGGINGFACE_API_KEY"],
        sample_rate=44100,
        min_chunk_duration=1.0,
        max_chunk_duration=5.0,
        min_speech_duration=0.5,
    )

    # Initialize buffer
    audio_buffer = b""
    BYTES_PER_SAMPLE = 4  # 32-bit = 4 bytes
    MIN_BUFFER_SIZE = int(
        chunker.sample_rate * BYTES_PER_SAMPLE * chunker.min_chunk_duration
    )

    try:
        async for chunk_data in listen_raw(websocket):
            try:
                if chunk_data:
                    audio_buffer += chunk_data

                    # Process when we have enough data
                    if len(audio_buffer) >= MIN_BUFFER_SIZE:
                        # Convert to numpy array
                        audio_np = (
                            np.frombuffer(audio_buffer, dtype=np.int32).astype(
                                np.float32
                            )
                            / 2147483648.0
                        )

                        # Find speech regions
                        speech_regions = chunker.find_speech_regions(audio_np)

                        # Get optimal chunk regions
                        chunk_regions = chunker.get_chunk_regions(
                            speech_regions, len(audio_np) / chunker.sample_rate
                        )

                        # Process each chunk
                        for start_time, end_time in chunk_regions:
                            # Convert time to samples
                            start_sample = int(start_time * chunker.sample_rate)
                            end_sample = int(end_time * chunker.sample_rate)

                            # Extract chunk
                            chunk_np = audio_np[start_sample:end_sample]

                            # Save for debugging
                            debugger.save_audio_chunk(
                                chunk_np,
                                sample_rate=chunker.sample_rate,
                                prefix="transcribed",
                            )

                            # Transcribe
                            transcription = transcribe_audio_local(chunk_np)

                            if transcription:
                                await websocket.send_text(transcription)
                                logger.info(f"Transcribed: {transcription}")

                        # Keep unprocessed audio
                        if chunk_regions:
                            last_end_sample = int(
                                chunk_regions[-1][1] * chunker.sample_rate
                            )
                            audio_buffer = audio_buffer[
                                last_end_sample * BYTES_PER_SAMPLE :
                            ]

                        # Prevent buffer from growing too large
                        max_buffer_samples = int(
                            chunker.max_chunk_duration * chunker.sample_rate
                        )
                        if len(audio_buffer) > max_buffer_samples * BYTES_PER_SAMPLE:
                            audio_buffer = audio_buffer[
                                -max_buffer_samples * BYTES_PER_SAMPLE :
                            ]

            except Exception as e:
                logger.error(f"Error processing chunk: {str(e)}")
                continue

    except WebSocketDisconnect:
        logger.info("Client disconnected")
    except Exception as e:
        logger.error(f"WebSocket error: {str(e)}")
    finally:
        await websocket.close()


@app.get("/stream-audio/")
async def stream_audio(query: str):
    """
    Stream audio endpoint.
    Query parameter: query (str) - The text query to convert to speech
    """
    return StreamingResponse(
        generate_audio_stream(query),
        media_type="audio/mpeg",
        headers={"Content-Disposition": "attachment; filename=audio_stream.mp3"},
    )


@app.get("/appv5")
async def get(request: Request):
    return templates.TemplateResponse("appv5.html", {"request": request})


# Serve HTML UI for recording and transmitting audio
@app.get("/")
async def get(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)