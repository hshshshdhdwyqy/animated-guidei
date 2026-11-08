"""Gemini service: chat (with Google Search grounding), media understanding, image generation/editing."""
import asyncio
import base64
import logging
import io
import wave
import re

import config

log = logging.getLogger("roxet.ai")


def _sdk():
    from google import genai
    from google.genai import types

    return genai, types


class AIError(Exception):
    """Error with a user-friendly Persian message and technical detail for admins."""

    def __init__(self, user_msg: str, detail: str = "", kind: str = "other"):
        super().__init__(user_msg)
        self.user_msg = user_msg
        self.detail = detail
        self.kind = kind


def classify_error(exc: Exception) -> AIError:
    if isinstance(exc, AIError):
        return exc
    code = getattr(exc, "code", None)
    text = f"{getattr(exc, 'status', '')} {getattr(exc, 'message', '')} {exc}".strip()
    low = text.lower()
    detail = text[:600]
    if "api key not valid" in low or "api_key_invalid" in low or "api key expired" in low:
        return AIError("🔑 کلید API معتبر نیست. مدیر باید کلید جدید را در پنل مدیریت ثبت کند.", detail, "key")
    if code == 429 or "resource_exhausted" in low or "quota" in low:
        return AIError("⏳ سهمیه یا محدودیت نرخ Gemini پر شده. چند دقیقه بعد دوباره امتحان کن.", detail, "quota")
    if code == 404 or "not found" in low:
        return AIError("🧠 مدل انتخاب‌شده پیدا نشد. مدیر باید مدل را از پنل مدیریت عوض کند.", detail, "model")
    if code == 403 or "permission" in low or "location is not supported" in low:
        return AIError(
            "🚫 دسترسی به Gemini رد شد (کلید بدون دسترسی یا سرور در منطقه‌ی پشتیبانی‌نشده).",
            detail,
            "perm",
        )
    if code in (500, 502, 503, 504) or "unavailable" in low or "overloaded" in low:
        return AIError("😴 سرورهای Gemini شلوغ‌اند. چند لحظه بعد دوباره امتحان کن.", detail, "busy")
    if "safety" in low or "blocked" in low:
        return AIError("⚠️ این درخواست به‌خاطر محدودیت‌های ایمنی پردازش نشد.", detail, "safety")
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return AIError("⌛ پاسخ دیر رسید. دوباره امتحان کن.", detail, "timeout")
    return AIError("در ارتباط با Gemini خطایی پیش آمد. دوباره امتحان کن.", detail, "other")


# Part descriptions are plain tuples so other modules need not import the SDK:
#   ("text", "hello")  or  ("bytes", b"...", "image/jpeg")
def text_part(t: str):
    return ("text", t)


def bytes_part(data: bytes, mime: str):
    return ("bytes", data, mime)


def _to_sdk_parts(parts):
    _, types = _sdk()
    out = []
    for p in parts:
        if p[0] == "text":
            out.append(types.Part.from_text(text=p[1]))
        else:
            out.append(types.Part.from_bytes(data=p[1], mime_type=p[2]))
    return out


def _version_key(name: str):
    m = re.search(r"(\d+(?:\.\d+)?)", name)
    return float(m.group(1)) if m else 0.0


class GeminiService:
    def __init__(self, db):
        self.db = db
        self._client = None
        self._key = None

    # ---------------------------------------------------------------- settings
    @property
    def api_keys(self):
        raw = self.db.get_setting("gemini_keys") or ""
        keys = [x.strip() for x in raw.splitlines() if x.strip()]
        if not keys:
            one = self.db.get_setting("gemini_key") or config.ENV_GEMINI_KEY
            keys = [one] if one else []
        return keys

    @property
    def api_key(self) -> str:
        keys = self.api_keys
        if not keys:
            return ""
        index = self.db.get_int("gemini_key_index", 0) % len(keys)
        return keys[index]

    def rotate_key(self):
        keys = self.api_keys
        if len(keys) < 2:
            return False
        self.db.set_setting("gemini_key_index", (self.db.get_int("gemini_key_index", 0) + 1) % len(keys))
        self._client = None
        self._key = None
        log.warning("Rotated Gemini API key after quota/rate-limit response.")
        return True

    @property
    def text_model(self) -> str:
        return self.db.get_setting("text_model") or config.DEFAULT_TEXT_MODEL

    @property
    def image_model(self) -> str:
        return self.db.get_setting("image_model") or config.DEFAULT_IMAGE_MODEL

    def has_key(self) -> bool:
        return bool(self.api_key)

    def client(self):
        key = self.api_key
        if not key:
            raise AIError(
                "🔑 هنوز کلید API ثبت نشده. مدیر باید از پنل مدیریت کلید Gemini را وارد کند.", kind="nokey"
            )
        if key != self._key:
            genai, _ = _sdk()
            self._client = genai.Client(api_key=key)
            self._key = key
        return self._client

    # ------------------------------------------------------------------ models
    async def list_models(self, key: str = None):
        """Return [(name, supported_actions)] for the given (or saved) key. Raises on invalid key."""
        genai, _ = _sdk()
        cli = genai.Client(api_key=key) if key else self.client()

        def _run():
            result = []
            for m in cli.models.list():
                name = (getattr(m, "name", "") or "").replace("models/", "")
                actions = list(getattr(m, "supported_actions", None) or [])
                result.append((name, actions))
            return result

        return await asyncio.to_thread(_run)

    @staticmethod
    def filter_text_models(models):
        bad = ("image", "tts", "live", "audio", "embed", "imagen", "veo", "robotics", "computer", "aqa")
        names = [
            n
            for n, a in models
            if n.startswith("gemini") and ("generateContent" in a or not a) and not any(b in n for b in bad)
        ]
        return sorted(set(names), key=lambda n: (-_version_key(n), "preview" in n or "exp" in n, n))

    @staticmethod
    def filter_image_models(models):
        names = [
            n
            for n, a in models
            if ("image" in n and "generateContent" in a)
            or n.startswith("imagen")
            or ("image" in n and not a)
        ]
        return sorted(set(names), key=lambda n: (-_version_key(n), "preview" in n, n))

    # -------------------------------------------------------------------- chat
    async def chat(self, history, parts, system: str, web: bool = False) -> str:
        _, types = _sdk()
        contents = [
            types.Content(role=("user" if r == "user" else "model"), parts=[types.Part.from_text(text=t)])
            for r, t in history
        ]
        contents.append(types.Content(role="user", parts=_to_sdk_parts(parts)))
        cfg_kwargs = {"system_instruction": system}
        if web:
            cfg_kwargs["tools"] = [types.Tool(google_search=types.GoogleSearch())]
        config_obj = types.GenerateContentConfig(**cfg_kwargs)
        resp = await self._call(
            lambda: self.client().aio.models.generate_content(
                model=self.text_model, contents=contents, config=config_obj
            )
        )
        text = (getattr(resp, "text", None) or "").strip()
        if not text:
            reason = ""
            try:
                reason = str(resp.candidates[0].finish_reason)
            except Exception:  # noqa: BLE001
                pass
            raise AIError(
                "پاسخی تولید نشد (شاید به‌خاطر محدودیت‌های ایمنی). سؤال را کمی تغییر بده.",
                f"empty response {reason}",
                "empty",
            )
        if web:
            text += self._sources(resp)
        return text

    async def stream_chat(self, history, parts, system: str, web: bool = False):
        """Yield incremental text chunks from Gemini; callers may fall back to chat()."""
        _, types = _sdk()
        contents = [
            types.Content(role=("user" if r == "user" else "model"), parts=[types.Part.from_text(text=t)])
            for r, t in history
        ]
        contents.append(types.Content(role="user", parts=_to_sdk_parts(parts)))
        kwargs = {"system_instruction": system}
        if web:
            kwargs["tools"] = [types.Tool(google_search=types.GoogleSearch())]
        cfg = types.GenerateContentConfig(**kwargs)
        stream = await self.client().aio.models.generate_content_stream(
            model=self.text_model, contents=contents, config=cfg
        )
        async for chunk in stream:
            piece = getattr(chunk, "text", None)
            if piece:
                yield piece

    @staticmethod
    def _sources(resp) -> str:
        try:
            chunks = resp.candidates[0].grounding_metadata.grounding_chunks or []
        except Exception:  # noqa: BLE001
            return ""
        seen, lines = set(), []
        for ch in chunks:
            web = getattr(ch, "web", None)
            uri = getattr(web, "uri", None)
            if not uri or uri in seen:
                continue
            seen.add(uri)
            title = (getattr(web, "title", None) or "منبع").replace("[", "(").replace("]", ")")
            lines.append(f"[{title}]({uri})")
            if len(lines) >= 4:
                break
        return ("\n\n🔎 منابع:\n" + "\n".join(f"• {l}" for l in lines)) if lines else ""

    # -------------------------------------------------------------- speech/video
    async def generate_speech(self, text: str, voice: str = "Kore") -> bytes:
        _, types = _sdk()
        model = self.db.get_setting("tts_model") or config.DEFAULT_TTS_MODEL
        cfg = types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
                )
            ),
        )
        response = await self._call(
            lambda: self.client().aio.models.generate_content(model=model, contents=text[:5000], config=cfg)
        )
        for part in getattr(response, "parts", []) or []:
            inline = getattr(part, "inline_data", None)
            if inline and getattr(inline, "data", None):
                data = inline.data
                if isinstance(data, str):
                    data = base64.b64decode(data)
                data = bytes(data)
                # Gemini speech responses are commonly raw 24 kHz, 16-bit PCM.
                # Wrap PCM in a WAV container so Telegram/media players can open it.
                if data[:4] == b"RIFF":
                    return data
                wav = io.BytesIO()
                with wave.open(wav, "wb") as audio_file:
                    audio_file.setnchannels(1)
                    audio_file.setsampwidth(2)
                    audio_file.setframerate(24000)
                    audio_file.writeframes(data)
                return wav.getvalue()
        raise AIError("صدایی از مدل دریافت نشد؛ مدل TTS یا دسترسی کلید را بررسی کن.", "empty TTS response", "empty")

    async def generate_video(self, prompt: str, aspect_ratio: str = "16:9") -> bytes:
        import base64
        model = self.db.get_setting("video_model") or config.DEFAULT_VIDEO_MODEL
        interaction = await self._call(lambda: self.client().aio.interactions.create(
            model=model, input=prompt[:3000],
            response_format={"type": "video", "aspect_ratio": aspect_ratio},
        ), attempts=1, timeout=900)
        output = getattr(interaction, "output_video", None)
        data = getattr(output, "data", None) if output else None
        if not data:
            raise AIError("مدل ویدیو خروجی قابل‌دریافت نداد؛ دسترسی مدل را بررسی کن.", "empty video output", "empty")
        try:
            return base64.b64decode(data)
        except Exception as exc:
            raise AIError("دریافت فایل ویدیو ناموفق بود.", str(exc), "other") from exc

    # ------------------------------------------------------------------ images
    async def generate_image(self, prompt: str, source_images=None):
        """Create (or edit, when source_images is given) an image. Returns (bytes, caption_text)."""
        _, types = _sdk()
        model = self.image_model
        if model.startswith("imagen") and not source_images:
            resp = await self._call(
                lambda: self.client().aio.models.generate_images(
                    model=model,
                    prompt=prompt,
                    config=types.GenerateImagesConfig(number_of_images=1),
                )
            )
            imgs = getattr(resp, "generated_images", None) or []
            if not imgs:
                raise AIError("تصویری ساخته نشد (احتمالاً به‌خاطر محدودیت ایمنی). توضیح را تغییر بده.", "no imagen output", "empty")
            return imgs[0].image.image_bytes, ""

        if model.startswith("imagen"):
            raise AIError(
                "مدل تصویر فعلی (Imagen) از ویرایش عکس پشتیبانی نمی‌کند. مدیر باید یک مدل gemini-…-image انتخاب کند.",
                "imagen cannot edit",
                "model",
            )

        parts = [("text", prompt)] + [("bytes", b, "image/jpeg") for b in (source_images or [])]
        contents = [types.Content(role="user", parts=_to_sdk_parts(parts))]
        cfg = types.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"])
        resp = await self._call(
            lambda: self.client().aio.models.generate_content(model=model, contents=contents, config=cfg)
        )
        img, note = None, []
        try:
            cand_parts = resp.candidates[0].content.parts or []
        except Exception:  # noqa: BLE001
            cand_parts = []
        for p in cand_parts:
            inline = getattr(p, "inline_data", None)
            if inline is not None and getattr(inline, "data", None):
                img = _decode_image(inline.data)
            elif getattr(p, "text", None):
                note.append(p.text)
        if not img:
            hint = " ".join(note)[:300]
            raise AIError(
                "تصویری ساخته نشد. " + (f"پاسخ مدل: {hint}" if hint else "توضیح را دقیق‌تر یا متفاوت بنویس."),
                "no image part in response",
                "empty",
            )
        return img, " ".join(note).strip()

    # ------------------------------------------------------------------- utils
    async def _call(self, factory, attempts: int = 3, timeout: int = 180):
        last = None
        for i in range(attempts):
            try:
                return await asyncio.wait_for(factory(), timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                err = classify_error(exc)
                last = err
                log.warning("Gemini call failed (%s): %s", err.kind, err.detail)
                if err.kind in ("quota", "key") and i < attempts - 1 and self.rotate_key():
                    continue
                if err.kind in ("busy", "timeout") and i < attempts - 1:
                    await asyncio.sleep(2 * (i + 1))
                    continue
                raise err from exc
        raise last  # pragma: no cover


def _decode_image(data) -> bytes:
    """The SDK returns raw bytes; some versions return base64 text. Handle both."""
    if isinstance(data, str):
        return base64.b64decode(data)
    data = bytes(data)
    if data[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1", b"\xff\xd8\xff\xdb") or data[:4] == b"RIFF":
        return data
    try:
        decoded = base64.b64decode(data, validate=True)
        if decoded[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1", b"\xff\xd8\xff\xdb"):
            return decoded
    except Exception:  # noqa: BLE001
        pass
    return data
