import os
import json
import tempfile
import openai
from channels.generic.websocket import AsyncWebsocketConsumer
from django.conf import settings

# OpenAI key from settings (ensure set)
openai.api_key = getattr(settings, "OPENAI_API_KEY", None)

class ChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.room_name = self.scope['url_route']['kwargs']['room_name']
        self.room_group_name = f'chat_{self.room_name}'
        # Path to store audio chunks for this connection (temp file)
        self._audio_tempfile = tempfile.NamedTemporaryFile(delete=False, suffix='.webm')
        self._audio_path = self._audio_tempfile.name
        self._audio_tempfile.close()  # we'll append bytes later

        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.accept()
        await self.send(text_data=json.dumps({"message": f"Connected to room {self.room_name}"}))

    async def disconnect(self, close_code):
        # cleanup
        try:
            os.remove(self._audio_path)
        except Exception:
            pass
        await self.channel_layer.group_discard(self.room_group_name, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        """
        Handles:
         - text_data JSON with {"message": "..."}  -> AI grammar correction -> broadcast
         - bytes_data -> append to temp webm file
         - text_data JSON with {"action":"finalize_audio"} -> transcribe & correct -> broadcast
        """
        # Binary audio chunk
        if bytes_data:
            # append bytes to temp file
            with open(self._audio_path, 'ab') as f:
                f.write(bytes_data)
            return

        # Text message / control message
        if text_data:
            try:
                data = json.loads(text_data)
            except Exception:
                return

            # finalize audio -> call speech-to-text and AI correction
            if data.get('action') == 'finalize_audio':
                # If no audio or openai key missing, return error
                if not openai.api_key:
                    await self.send(text_data=json.dumps({"message": "OpenAI key not configured on server."}))
                    return

                # Transcribe using OpenAI (whisper)
                try:
                    with open(self._audio_path, 'rb') as audio_file:
                        # 'whisper-1' or 'gpt-4o-transcribe' depending on availability
                        transcript = openai.Audio.transcribe("whisper-1", audio_file)
                        user_text = transcript.get('text', '').strip()
                except Exception as e:
                    user_text = ""
                    await self.send(text_data=json.dumps({"message": "Transcription error: " + str(e)}))

                if user_text:
                    ai_response = await self.get_ai_correction(user_text)
                    await self.channel_layer.group_send(
                        self.room_group_name,
                        {'type': 'chat_message', 'message': f"User (transcribed): {user_text}\nAI correction: {ai_response}"}
                    )
                return

            # plain chat message handling
            if 'message' in data:
                user_message = data['message']
                ai_response = await self.get_ai_correction(user_message)
                await self.channel_layer.group_send(
                    self.room_group_name,
                    {'type': 'chat_message', 'message': ai_response}
                )
                return

    async def chat_message(self, event):
        await self.send(text_data=json.dumps({'message': event['message']}))

    async def get_ai_correction(self, text):
        """Call OpenAI Chat API to correct grammar and return improved text."""
        try:
            resp = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "You are an English teacher that corrects grammar and gives improved versions of student sentences."},
                    {"role": "user", "content": text}
                ],
                max_tokens=300
            )
            return resp['choices'][0]['message']['content'].strip()
        except Exception as e:
            return "AI error: " + str(e)