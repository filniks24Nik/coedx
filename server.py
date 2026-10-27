"""Local development server. Photos are never written to disk."""
import base64
import io
import json
import os
import secrets
import urllib.request
import urllib.error
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from PIL import Image, UnidentifiedImageError

app = FastAPI()
ROOT = Path(__file__).parent
Image.MAX_IMAGE_PIXELS = 30_000_000


@app.get('/')
def home():
    return FileResponse(ROOT / 'index.html')


@app.get('/api/status')
def status():
    return {'ai_available': bool(os.environ.get('OPENAI_API_KEY'))}


@app.post('/api/retouch')
async def retouch(request: Request):
    # Do not expose this development endpoint publicly without auth/rate limits.
    if request.headers.get('sec-fetch-site') == 'cross-site':
        raise HTTPException(403, 'Недопустимый источник запроса')
    key = os.environ.get('OPENAI_API_KEY')
    if not key:
        raise HTTPException(503, 'ИИ-обработка не подключена. Кадрирование и печать работают без неё.')
    if int(request.headers.get('content-length', '0')) > 12_000_000:
        raise HTTPException(413, 'Максимальный размер запроса — 12 МБ')
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 12_000_000:
            raise HTTPException(413, 'Максимальный размер запроса — 12 МБ')
    try:
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError()
        mode = body.get('mode', 'document')
        style = body.get('style', 'original')
        if mode not in ('document', 'portrait') or style not in ('original', 'business', 'casual', 'minimal'):
            raise ValueError()
        data = base64.b64decode(body['image'], validate=True)
        image = Image.open(io.BytesIO(data))
        image.load()
        image = image.convert('RGB')
        image.thumbnail((2048, 2048))
        output = io.BytesIO()
        image.save(output, format='PNG')
    except (ValueError, KeyError, TypeError, UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise HTTPException(400, 'Не удалось прочитать изображение')
    prompt = (
        'Edit this portrait photograph. Preserve the exact identity, facial geometry, '
        'age, skin tone, eyes, nose, mouth, hairline and permanent distinguishing marks. '
        'Keep realistic skin texture. Use a clean pure white background and natural even lighting. '
        'Keep the original head orientation. Do not invent missing facial features. '
    )
    if mode == 'document':
        prompt += ('This is preparation for an identity document: do not beautify, smooth skin, '
                   'remove under-eye shadows, moles, scars or other facial features. '
                   'Keep the original clothing. Only clean up background and lighting.')
    else:
        prompt += ('Gently reduce temporary blemishes and under-eye shadows, without changing identity. ')
        prompt += {
            'original': 'Keep the original clothing.',
            'business': 'Use elegant professional business clothing appropriate to the subject.',
            'casual': 'Use tasteful smart casual clothing appropriate to the subject.',
            'minimal': 'Use a plain neutral crew-neck top.',
        }[style]
    boundary = secrets.token_hex(16)
    parts = []
    def field(name, value):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    field('model', 'gpt-image-1')
    field('prompt', prompt)
    field('size', '1024x1536')
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="portrait.png"\r\nContent-Type: image/png\r\n\r\n'.encode() + output.getvalue() + b'\r\n')
    parts.append(f'--{boundary}--\r\n'.encode())
    req = urllib.request.Request('https://api.openai.com/v1/images/edits', data=b''.join(parts), headers={
        'Authorization': f'Bearer {key}', 'Content-Type': f'multipart/form-data; boundary={boundary}'})
    try:
        # Blocking provider operation runs outside the event loop.
        import asyncio
        def call():
            with urllib.request.urlopen(req, timeout=180) as response:
                return json.load(response)
        result = await asyncio.to_thread(call)
        edited = base64.b64decode(result['data'][0]['b64_json'])
    except Exception:
        raise HTTPException(502, 'Сервис обработки не ответил. Попробуйте позже; исходное фото сохранено в браузере.')
    return Response(edited, media_type='image/png', headers={'Cache-Control': 'no-store'})
