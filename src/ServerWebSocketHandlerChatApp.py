import json
import logging
import ssl
import uuid
import base64
import asyncio

import websockets
from fastapi import WebSocket
import os
from src.utils.ChatgptUtil import ChatGPTHandler

from .WebsocketClient import Client
from .vad.vad_factory import VADFactory
from .asr.asr_factory import ASRFactory
from .utils.audio_stream_util import AudioStreamManager
from .utils.chatgpt_util import ChatGPTClient
from .utils.elevenlabs_util import ElevenLabsClient
import datetime
from typing import Dict, List
from enum import Enum
from .types import AssistantResponse
from dataclasses import asdict


SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

voice_id = "pNInz6obpgDQGcFmaJgB"
model_id = "eleven_turbo_v2_5"


class ChatHistory:
    def __init__(self):
        self.conversations = {}  # userid -> list of messages

    def add_message(self, user_id: str, message: AssistantResponse):
        if user_id not in self.conversations:
            self.conversations[user_id] = []
        self.conversations[user_id].append(message)

    def get_history(self, user_id: str) -> List[AssistantResponse]:
        return self.conversations.get(user_id, [])


class EventType(Enum):
    ASK_QUESTION = "askQuestion"
    AUDIO_PACKET = "audioPacket"
    END_QUESTION = "endQuestion"
    TRANSCRIPTION = "final_transcription"
    ANSWER = "answer"


class WebSocketHandler:
    def __init__(self, sampling_rate=16000, samples_width=2):
        self.vad_pipeline = VADFactory.create_vad_pipeline(
            "silero", auth_token=os.environ["HUGGINGFACE_API_KEY"]
        )
        self.asr_pipeline = ASRFactory.create_asr_pipeline("groq")
        self.sampling_rate = sampling_rate
        self.samples_width = samples_width
        self.connected_clients: Dict[str, Client] = {}
        self.active_sessions: Dict[str, Dict] = {}  # Store session state
        self.elevenlabs_client = ElevenLabsClient(voice_id=voice_id, model_id=model_id)
        self.chatgpt_client = ChatGPTClient()

        self.audio_stream_manager = AudioStreamManager(
            self.elevenlabs_client, self.chatgpt_client
        )
        self.chat_history = ChatHistory()

    async def handle_ask_question(self, client_id: str, websocket: WebSocket):
        """Handle the start of a new question"""
        self.active_sessions[client_id] = {
            "transcriptions": [],
            "processing_tasks": [],
            "current_audio": bytearray(),
        }
        await websocket.send_json(
            {"type": EventType.ASK_QUESTION.value, "status": "ready"}
        )

    async def handle_audio_packet(
        self, client_id: str, websocket: WebSocket, audio_data: bytes
    ):
        """Process incoming audio packet"""
        session = self.active_sessions[client_id]
        client = self.connected_clients[client_id]

        # Append audio data
        client.append_audio_data(audio_data)

        # Create and track processing task
        task = asyncio.create_task(
            client.process_audio(
                websocket,
                self.vad_pipeline,
                asr_pipeline=self.asr_pipeline,
                transcriptions_list=session["transcriptions"],
            )
        )
        session["processing_tasks"].append(task)

        # Clean up completed tasks
        session["processing_tasks"] = [
            t for t in session["processing_tasks"] if not t.done()
        ]

    async def handle_end_question(
        self, client_id: str, websocket: WebSocket, user_id: str
    ):
        """Process the end of a question and generate response"""
        session = self.active_sessions[client_id]

        # Wait for all processing tasks to complete
        if session["processing_tasks"]:
            await asyncio.gather(*session["processing_tasks"])

        response_id = str(uuid.uuid4())

        # Compile final transcription
        full_text = " ".join(t["text"] for t in session["transcriptions"])
        final_transcription = {
            "type": EventType.TRANSCRIPTION.value,
            "text": full_text,
            "segments": session["transcriptions"],
            "response_id": response_id,
        }

        # Store in chat history
        self.chat_history.add_message(
            user_id,
            AssistantResponse(
                role="user", content=full_text, timestamp=datetime.datetime.now()
            ),
        )

        # Send transcription
        await websocket.send_json(final_transcription)

        # Generate and stream audio response
        gptResponse = await self.audio_stream_manager.handle_stream(
            websocket,
            full_text,
            response_id,
            history=self.chat_history.get_history(user_id),
        )

        # Save response history
        self.chat_history.add_message(
            user_id,
            AssistantResponse(
                role="assistant",
                content=gptResponse,
                timestamp=datetime.datetime.now(),
            ),
        )

        # Clear session data
        self.active_sessions[client_id] = {
            "transcriptions": [],
            "processing_tasks": [],
            "current_audio": bytearray(),
        }

    async def handle_websocket(self, websocket: WebSocket, user_id: str):
        client_id = str(uuid.uuid4())
        client = Client(client_id, self.sampling_rate, self.samples_width)
        self.connected_clients[client_id] = client

        logging.info(f"Client {client_id} connected with user_id {user_id}")

        try:
            # Send existing chat history
            history = self.chat_history.get_history(user_id)
            if history:
                await websocket.send_json(
                    {
                        "type": "chat_history",
                        "history": [asdict(response) for response in history],
                    }
                )

            while True:
                message = await websocket.receive()

                if "text" in message:
                    data = json.loads(s=message["text"])
                    event_type = data.get("type")

                    if event_type == EventType.ASK_QUESTION.value:
                        await self.handle_ask_question(client_id, websocket)
                    elif event_type == EventType.END_QUESTION.value:
                        await self.handle_end_question(client_id, websocket, user_id)

                elif "bytes" in message:
                    # Handle audio packet
                    await self.handle_audio_packet(
                        client_id, websocket, audio_data=message["bytes"]
                    )

        except Exception as e:
            logging.error(f"Error in websocket handler: {e}")
        finally:
            if client_id in self.connected_clients:
                del self.connected_clients[client_id]
            if client_id in self.active_sessions:
                del self.active_sessions[client_id]
            logging.info(f"Client {client_id} disconnected")
