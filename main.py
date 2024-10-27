import os
import json
import base64
import asyncio
import websockets
import urllib.parse
from fastapi import FastAPI, WebSocket, Request, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from typing import Optional

from twilio.twiml.voice_response import VoiceResponse, Connect, Redirect, Dial
from dotenv import load_dotenv
from customgpt_client import CustomGPT
import uuid
import logging
import time
import redis

load_dotenv()

redis_url = os.getenv("REDIS_URL")

redis_client = redis.from_url(redis_url)

current_dir = os.path.dirname(__file__)

mp3_file_path = os.path.join(current_dir, "static", "typing.wav")

CUSTOMGPT_API_KEY = os.getenv('CUSTOMGPT_API_KEY')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
PORT = int(os.getenv('PORT', 5050))
SYSTEM_MESSAGE_2 = (
    "You are a helpful AI assistant designed to answer questions using only the additional context provided by the get_additional_context function. Only respond to greetings without a function call. Anything else, you NEED to ask the information database by calling the get_additional_context function."
    "For every user query, take the user query and generate a detailed, context-rich request to the get_additional_context function. "
    "Start the generated request with the words 'A user asked: ' and include the exact transcription of the user's request. "
    "Expand on the intent and purpose behind the question, adding depth, specificity, and clarity."
    "Tailor the information as if the user were asking an expert in the relevant field, and include any relevant contextual details that would help make the request more comprehensive."
    "The goal is to enhance the user query, making it clearer and more informative while maintaining the original intent."
    "Do not elaborate the user_query in audio stream just pass it to get_additional_context."
    "Now, using this approach, elaborate the user query and then pass detailed user_query immediately to get_additional_context function to obtain information. "
    "Do not use your own knowledge base to answer questions. "
    "Always base your responses solely on the information returned by get_additional_context. "
    "If get_additional_context returns information indicating it's unable to answer or provide details, "
    "Do not elaborate or use any other information beyond what get_additional_context provides. "
    "If get_additional_context provides relevant information, incorporate it into your response. "
    "Be concise and directly address the user's query based only on the additional context. "
    "Do not mention the process of using get_additional_context in your responses to the user."
    "Respond with concise, natural-sounding answers using varied intonation; incorporate brief pauses and occasional filler words; use context-aware language and reference previous statements; include subtle verbal cues like 'hmm' or 'I see' to simulate thoughtfulness; maintain a consistent personality; and adapt your conversation flow to the caller's tone and pace, all while keeping responses under 50 words unless absolutely necessary."
    "Track consecutive misinterpretations or inabilty to answer user_query and initiate a handoff if misunderstandings reach three. Tell the user to press 0 or ask us to transfer to live agent by triggering call_support function."
    "Verbal Indicators: Recognize phrases like “Operator,” “Help,” or “Live Agent.”to and trigger call_support function. "
    "YOU SHOULD NEVER START UNLESS ASKED."
)


VOICE = 'alloy'
LOG_EVENT_TYPES = [
    'response.content.done', 'response.done',
    'input_audio_buffer.committed', 'input_audio_buffer.speech_stopped',
    'input_audio_buffer.speech_started', 'session.created', 'response.audio.done',
    'conversation.item.input_audio_transcription.completed'
]

PERSONAL_PHONE_NUMBER = os.getenv("PERSONAL_PHONE_NUMBER")

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

if not OPENAI_API_KEY:
    raise ValueError('Missing the OpenAI API key. Please set it in the .env file.')

@app.get("/", response_class=HTMLResponse)
async def index_page():
    return "<h1>Twilio Media Stream Server is running!</h1>"

@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request, project_id: int, api_key: Optional[str] = CUSTOMGPT_API_KEY, phone_number: Optional[str] = PERSONAL_PHONE_NUMBER):
    form_data = await request.form() if request.method == "POST" else request.query_params
    caller_number = form_data.get('From', 'Unknown')
    logger.info(f"Caller: {caller_number}")
    session_id = create_session(api_key, project_id, caller_number)
    logger.info(f"Project::{project_id}")
    logger.info(f"Incoming call handled. Session ID: {session_id}")
    response = VoiceResponse()
    response.pause(length=1)
    host = request.url.hostname
    connect = Connect()
    api_key = urllib.parse.quote_plus(api_key)
    connect.stream(url=f'wss://{host}/media-stream/project/{project_id}/session/{session_id}/{api_key}')
    response.append(connect)
    phone_number = urllib.parse.quote_plus(phone_number)
    response.redirect(url=f"https://{host}/end-stream/{session_id}?phone_number={phone_number}")
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.api_route("/end-stream/{session_id}", methods=["GET", "POST"])
async def handle_end_call(request: Request, session_id: Optional[str] = None, phone_number: Optional[str] = PERSONAL_PHONE_NUMBER):
    state = redis_client.get(session_id)
    if state is None:
        state = 'transfer'
    else:
        state = state.decode('utf-8') 
    logger.info(f"Ending Stream with state: {state}")
    response = VoiceResponse()
    if state == "hangup":
        response.hangup()
    else:
        dial = Dial()
        dial.number(phone_number)
        response.append(dial)

    return HTMLResponse(content=str(response), media_type="application/xml")

@app.websocket("/media-stream/project/{project_id}/session/{session_id}/{api_key}")
async def handle_media_stream(websocket: WebSocket, project_id: int, session_id: str, api_key: str):
    logger.info(f"WebSocket connection attempt. Session ID: {session_id}:: {api_key}")
    await websocket.accept()
    logger.info(f"WebSocket connection accepted. Session ID: {session_id}")

    # Create task termination event
    termination_event = asyncio.Event()

    async with websockets.connect(
        'wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01',
        extra_headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "OpenAI-Beta": "realtime=v1"
        }
    ) as openai_ws:
        try:
            start_time = time.time()
            stream_sid = None

            async def check_timeout():
                logger.info(f"Checking inactivity. Session ID: {session_id}")
                try:
                    while not termination_event.is_set():
                        current_time = time.time()
                        diff = current_time - start_time
                        if diff > 40:
                            logger.info(f"Session timeout after 30 seconds of inactivity. Session ID: {session_id}")
                            redis_client.set(session_id, "hangup")
                            termination_event.set()
                            await clear_buffer(websocket, openai_ws, stream_sid)
                            await websocket.close()
                            break
                        await asyncio.sleep(5)
                except Exception as e:
                    raise e

            await send_session_update(openai_ws)
            asyncio.create_task(check_timeout())
            async def receive_from_twilio():
                nonlocal stream_sid, start_time
                try:
                    while not termination_event.is_set():
                        try:
                            message = await websocket.receive_text()
                            data = json.loads(message)
                            if data['event'] == 'media' and openai_ws.open:
                                audio_append = {
                                    "type": "input_audio_buffer.append",
                                    "audio": data['media']['payload']
                                }
                                await openai_ws.send(json.dumps(audio_append))
                            elif data['event'] == 'start':
                                stream_sid = data['start']['streamSid']
                                start_time = time.time()
                                logger.info(f"Incoming stream has started {stream_sid}")
                            elif data['event'] == 'dtmf':
                                digit = data['dtmf']['digit']
                                logger.info(f"DTMF received: {digit}")
                                if digit == "0":
                                    logger.info("DTMF '0' detected, redirecting call...")
                                    termination_event.set()
                                    await websocket.close()
                                    break
                        except WebSocketDisconnect:
                            logger.info(f"Twilio WebSocket disconnected. Session ID: {session_id}")
                            break
                        except RuntimeError as e:
                            if "WebSocket is not connected" in str(e):
                                logger.info(f"WebSocket connection lost. Session ID: {session_id}")
                                break
                            logger.error(f"Runtime error in receive_from_twilio: {e}")
                            break
                        except Exception as e:
                            logger.error(f"Error in receive_from_twilio: {e}")
                            break
                finally:
                    termination_event.set()
                    await clear_buffer(websocket, openai_ws, stream_sid)
                    raise Exception("Close Stream")

            async def send_to_twilio():
                nonlocal stream_sid, api_key, start_time
                try:
                    async for openai_message in openai_ws:
                        try:
                            response = json.loads(openai_message)
                            start_time = time.time()
                            if response['type'] in LOG_EVENT_TYPES:
                                logger.info(f"Received event: {response['type']}::{response}")
                            if response['type'] == 'session.updated':
                                logger.info(f"Session updated successfully: {response}")
                            if response['type'] == "input_audio_buffer.speech_started":
                                logger.info(f"Input Audio Detected::{response}")
                                await clear_buffer(websocket, openai_ws, stream_sid)

                            if response['type'] == 'response.audio.delta' and response.get('delta'):
                                try:
                                    audio_payload = base64.b64encode(base64.b64decode(response['delta'])).decode('utf-8')
                                    audio_delta = {
                                        "event": "media",
                                        "streamSid": stream_sid,
                                        "media": {
                                            "payload": audio_payload
                                        }
                                    }
                                    await websocket.send_json(audio_delta)
                                    start_time = time.time()
                                except asyncio.TimeoutError:
                                    logger.error("Timeout while sending audio data to Twilio")
                                except Exception as e:
                                    logger.error(f"Error processing audio data: {e}")                            
                            if response['type'] == 'response.function_call_arguments.done':
                                try:
                                    function_name = response['name']
                                    call_id = response['call_id']
                                    arguments = json.loads(response['arguments'])
                                    if function_name == 'get_additional_context':
                                        await play_typing(websocket, stream_sid)
                                        logger.info("CustomGPT Started")
                                        start_time = time.time()
                                        result = get_additional_context(arguments['query'], api_key, project_id, session_id)
                                        logger.info(f"Clear Audio::Additional Context gained")
                                        await clear_buffer(websocket, openai_ws, stream_sid)
                                        end_time = time.time()
                                        elapsed_time = end_time - start_time
                                        logger.info(f"CustomGPT response: {result}")
                                        logger.info(f"get_additional_context execution time: {elapsed_time:.4f} seconds")
                                        function_response = {
                                            "type": "conversation.item.create",
                                            "item": {
                                                "type": "function_call_output",
                                                "call_id": call_id,
                                                "output": result
                                            }
                                        }
                                        await openai_ws.send(json.dumps(function_response))
                                        await openai_ws.send(json.dumps({"type": "response.create"}))
                                    elif function_name == 'call_support':
                                        logger.info("Detected Term for calling support...")
                                        termination_event.set()
                                        raise Exception("Close Stream")

                                except json.JSONDecodeError as e:
                                    logger.error(f"Error in json decode in function_call: {e}::{response}")

                        except json.JSONDecodeError as e:
                            logger.error(f"Error in json decode of response: {e}::{openai_message}")

                except WebSocketDisconnect:
                    logger.info(f"OpenAI WebSocket disconnected. Session ID: {session_id}")
                except Exception as e:
                    logger.error(f"Error in send_to_twilio: {e}")
                    raise Exception("Close Stream")

            await asyncio.gather(receive_from_twilio(), send_to_twilio())
        except websockets.exceptions.ConnectionClosed:
            logger.error(f"WebSocket connection closed unexpectedly. Session ID: {session_id}")
        except Exception as e:
            logger.error(f"Unexpected error in handle_media_stream: {e}")

        finally:
            try:
                await clear_buffer(websocket, openai_ws, stream_sid)
                await openai_ws.close()
                await websocket.close()
            except Exception:
                logger.info(f"WebSocket connection closed. Session ID: {session_id}")

def get_additional_context(query, api_key, project_id, session_id):
    custom_persona = """
    You are an AI assistant tasked with answering user queries based on a knowledge base. The user query is transcribed from voice audio, so there may be transcription errors.

    When responding to the user query, follow these guidelines:
    1. Match the query to the knowledge base using both phonetic and semantic similarity.
    2. Attempt to answer even if the match isn't perfect, as long as it seems reasonably close.

    Provide a concise answer, limited to three sentences.
    """

    tries = 0
    max_retries = 2
    while tries <= max_retries:
        try:
            CustomGPT.api_key = api_key or CUSTOMGPT_API_KEY
            conversation = CustomGPT.Conversation.send(
                project_id=project_id, 
                session_id=session_id, 
                prompt=query, 
                custom_persona=custom_persona
            )
            return conversation.parsed.data.openai_response  # Correct f-string is unnecessary
        except Exception as e:
            logger.error(f"Get Additional Context failed::Try {tries}::Error: {conversation}")
            time.sleep(2)
        tries += 1

    return "Sorry, I didn't get your query."

def create_session(api_key, project_id, caller_number):
    tries = 0
    max_retries = 2
    while tries <= max_retries:
        try:
            CustomGPT.api_key = api_key or CUSTOMGPT_API_KEY
            session = CustomGPT.Conversation.create(project_id=project_id, name=caller_number)
            logger.info(f"CustomGPT Session Created::{session.parsed.data}");
            return session.parsed.data.session_id
        except Exception as e:
            logger.error(f"Error in create_session::Try {tries}::Error: {e}")
        tries += 1

    session_id = uuid.uuid4()
    return session_id

async def send_session_update(openai_ws):
    session_update = {
        "type": "session.update",
        "session": {
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.6,
                "prefix_padding_ms": 300,
                "silence_duration_ms": 500
            },
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "voice": VOICE,
            "instructions": SYSTEM_MESSAGE_2,
            "modalities": ["text", "audio"],
            "temperature": 0.6,
            "tools": [
                {
                  "type": "function",
                  "name": "get_additional_context",
                  "description": "Elaborate on the user's original query, providing additional context, specificity, and clarity to create a more detailed, expert-level question. The function should transform a simple query into a richer and more informative version that is suitable for an expert to answer.",
                  "parameters": {
                    "type": "object",
                    "properties": {
                      "query": {
                        "type": "string",
                        "description": "The elaborated user query. This should fully describe the user's original question, adding depth, context, and clarity. Tailor the expanded query as if the user were asking an expert in the relevant field, providing necessary background or related subtopics that may help inform the response. Start with 'Please use your knowledge base'"
                      }
                    },
                    "required": ["query"]
                  }
                },
                {
                  "type": "function",
                  "name": "call_support",
                  "description": "The purpose of the call_support function is to help user when the agent is unable to answer query multiple times and they request transfer to a live agent or support but do not provide enough detail for effective assistance."
                }
            ]
        }
    }
    logger.info('Sending session update: %s', json.dumps(session_update))
    await openai_ws.send(json.dumps(session_update))
    time.sleep(1)
    initial_response = {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "assistant",
            "content": [
              {
                "type": "text",
                "text": "Hello, how can I assist you today?"
              }
            ]
        }
    }
    await openai_ws.send(json.dumps(initial_response))
    await openai_ws.send(json.dumps({"type": "response.create"}))

async def play_typing(websocket, stream_sid):
    with open(mp3_file_path, "rb") as mp3_file:
        mp3_data = mp3_file.read()
        base64_audio = base64.b64encode(mp3_data).decode('utf-8')

    audio_delta = {
        "event": "media",
        "streamSid": stream_sid,
        "media": {
            "payload": base64_audio
        }
    }
    await websocket.send_json(audio_delta)

async def clear_buffer(websocket, openai_ws, stream_sid):
    audio_delta = {
      'streamSid': stream_sid,
      'event': 'clear',
    }
    await openai_ws.send(json.dumps({"type": "response.cancel"}))
    await websocket.send_json(audio_delta)
