import os
import sys
import asyncio
import logging
import re
import time
import mimetypes
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Callable, Tuple
from dotenv import load_dotenv

import sqlite3

from telethon import TelegramClient
from telethon.tl.types import (
    Chat, Channel, DocumentAttributeFilename, DocumentAttributeVideo,
    MessageMediaPhoto, MessageMediaDocument,
    ForumTopic, ForumTopicDeleted, MessageReplyHeader
)
from telethon.tl.functions.messages import GetForumTopicsRequest
from telethon.errors import (
    SessionPasswordNeededError, FloodWaitError,
    PhoneNumberInvalidError, ApiIdInvalidError,
    PhoneCodeInvalidError, PhoneCodeExpiredError,
    PhoneNumberBannedError, RPCError
)
from telethon.utils import get_extension

from db_tracker import DownloadTrackerDB, normalize_channel_id, sanitize_folder_name
from config_manager import ConfigManager, StorageError
from crypto_accelerator import init_crypto_acceleration, is_accelerated, get_engine_status

# Strict Single-File Concurrency Constraints (Benchmark-Validated)
MAX_CONCURRENT_DOWNLOADS = 1
MAX_ACTIVE_FILE_TRANSFERS = 1
PARALLEL_CHUNK_REQUESTS = 0
CHUNK_SIZE_BYTES = 512 * 1024  # 512 KB optimal MTProto chunk size

logger = logging.getLogger(__name__)

COPYRIGHT_TEXT = "This message couldn't be displayed on your device due to copyright infringement."

def sanitize_filename(name: str) -> str:
    """Sanitizes directory names to avoid invalid Windows characters or path traversal."""
    s = re.sub(r'[\\/*?:"<>|]', "_", str(name)).strip()
    # Remove consecutive dots to eliminate any path traversal vectors
    s = re.sub(r'\.{2,}', '_', s)
    s = re.sub(r'^\.+', '', s)
    s = re.sub(r'\.+$', '', s)
    s = re.sub(r'_+', '_', s).strip(' _')
    return s[:200] or "Unnamed_Chat"

def sanitize_media_filename(name: str) -> str:
    name = str(name or "")
    name = re.sub(r'[\\/*?:"<>|]', "_", name).strip()
    name = re.sub(r'\s+', ' ', name)
    name = re.sub(r'^\.+', '', name)
    return name[:220] or "unnamed_file"

def format_size(n) -> str:
    try:
        n = float(n)
    except Exception:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024.0 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} PB"

class CancelledByUser(Exception):
    pass

class TelegramDownloaderEngine:
    def __init__(
        self,
        config_mgr: ConfigManager,
        db_tracker: DownloadTrackerDB,
        event_callback: Optional[Callable[[str, Any], None]] = None,
        session_name: str = "anon"
    ):
        self.config_mgr = config_mgr
        self.db = db_tracker
        self.event_callback = event_callback or (lambda event, data: None)
        self.cancel_flag = False
        self.client: Optional[TelegramClient] = None
        self.session_name = session_name
        self.is_downloading = False
        self.phone: Optional[str] = None
        self.phone_code_hash: Optional[str] = None
        self.account_id: Optional[int] = None
        self.dialogs_cache: Dict[Tuple[str, int], Dict[str, Any]] = {}
        self._forum_topics_cache: Dict[int, Dict[int, Dict[str, Any]]] = {}

        # Initialize native OpenSSL hardware AES acceleration
        accel_ok, accel_msg = init_crypto_acceleration()
        if accel_ok:
            logger.info(f"Crypto acceleration active: {accel_msg}")
        else:
            logger.warning(f"Crypto acceleration fallback: {accel_msg}")

        # Global single-transfer concurrency guard
        self._download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

        env_path = Path(__file__).parent / ".env"
        if env_path.exists():
            load_dotenv(dotenv_path=env_path, override=True)
        else:
            load_dotenv()

        self.api_id = os.getenv("API_ID")
        self.api_hash = os.getenv("API_HASH")
        if not self.session_name or self.session_name == "anon":
            self.session_name = os.getenv("SESSION_NAME", "anon")

    def emit(self, event_type: str, data: Any = None):
        try:
            self.event_callback(event_type, data)
        except Exception as e:
            logger.debug(f"Event callback error: {e}")

    async def init_client(self) -> bool:
        env_path = Path(__file__).parent / ".env"
        if env_path.exists():
            load_dotenv(dotenv_path=env_path, override=True)
        else:
            load_dotenv()

        self.api_id = os.getenv("API_ID")
        self.api_hash = os.getenv("API_HASH")

        if not self.api_id or not self.api_hash:
            err_msg = (
                "API_ID or API_HASH is missing in C:\\TelegramData\\.env.\n"
                "Please create or edit .env and specify your API credentials from https://my.telegram.org."
            )
            self.emit("auth_error", err_msg)
            self.emit("log", err_msg)
            return False

        try:
            api_id_int = int(str(self.api_id).strip())
        except ValueError:
            err_msg = f"API_ID must be a numeric integer. Got: {self.api_id!r}"
            self.emit("auth_error", err_msg)
            self.emit("log", err_msg)
            return False

        api_hash_str = str(self.api_hash).strip()
        if len(api_hash_str) < 10:
            err_msg = "API_HASH appears invalid (too short). Please check your .env file."
            self.emit("auth_error", err_msg)
            self.emit("log", err_msg)
            return False

        try:
            if self.client is None:
                self.client = TelegramClient(self.session_name, api_id_int, api_hash_str)
            if not self.client.is_connected():
                self.emit("auth_status", "Connecting to Telegram servers...")
                await self.client.connect()
            self.emit("log", f"Crypto Engine: {get_engine_status()}")
            return True
        except ApiIdInvalidError:
            err_msg = "Telegram rejected API_ID / API_HASH. Please verify your credentials at https://my.telegram.org."
            self.emit("auth_error", err_msg)
            self.emit("log", err_msg)
            return False
        except Exception as e:
            err_msg = f"Failed to connect to Telegram: {type(e).__name__} - {e}"
            self.emit("auth_error", err_msg)
            self.emit("log", err_msg)
            return False

    async def _resolve_account_id(self):
        try:
            if self.client and hasattr(self.client, "get_me"):
                res = self.client.get_me()
                if asyncio.iscoroutine(res):
                    me = await res
                else:
                    me = res
                if me and hasattr(me, "id"):
                    try:
                        self.account_id = int(me.id)
                    except (ValueError, TypeError):
                        pass
        except Exception as e:
            logger.debug(f"Could not resolve account_id: {e}")

    async def check_auth(self) -> bool:
        if not await self.init_client():
            return False
        try:
            if await self.client.is_user_authorized():
                await self._resolve_account_id()
                self.emit("auth_success", None)
                return True
        except Exception as e:
            logger.warning(f"Error checking user authorization: {e}")
        return False

    async def send_login_code(self, phone: str):
        if not await self.init_client():
            return

        phone_clean = re.sub(r"[^\d]", "", str(phone or ""))
        if not phone_clean or len(phone_clean) < 5:
            self.emit("auth_error", "Invalid phone number format. Please enter your full phone number with country code (e.g. 1234567890).")
            return

        try:
            self.emit("auth_status", f"Contacting Telegram servers to request OTP for {phone_clean}...")
            self.emit("log", f"Sending OTP code request to {phone_clean}...")
            sent_code = await self.client.send_code_request(phone_clean)
            self.phone = phone_clean
            self.phone_code_hash = sent_code.phone_code_hash

            delivery_hint = "Telegram app"
            sent_type_name = type(getattr(sent_code, "type", None)).__name__
            if "Sms" in sent_type_name:
                delivery_hint = "SMS"
            elif "Call" in sent_type_name:
                delivery_hint = "phone call"

            self.emit("auth_code_sent", f"Login code sent via {delivery_hint}. Please check your {delivery_hint} and enter the OTP below.")
            self.emit("log", f"OTP code sent via {delivery_hint} (hash: {self.phone_code_hash[:6]}...).")
        except PhoneNumberInvalidError:
            self.emit("auth_error", f"Phone number '{phone_clean}' is not recognized by Telegram. Ensure you included your country code without '+' (e.g. 1234567890).")
        except ApiIdInvalidError:
            self.emit("auth_error", "Telegram rejected API_ID / API_HASH. The API credentials in .env are invalid.")
        except PhoneNumberBannedError:
            self.emit("auth_error", f"This phone number ({phone_clean}) has been banned by Telegram.")
        except FloodWaitError as fwe:
            self.emit("auth_error", f"Telegram rate limit (FloodWait): Please wait {fwe.seconds} seconds before requesting another code.")
        except RPCError as rpce:
            self.emit("auth_error", f"Telegram RPC Error: {rpce.message or rpce}")
        except (ConnectionError, OSError, asyncio.TimeoutError) as ne:
            self.emit("auth_error", f"Network connection failed: {ne}. Please check your internet connection.")
        except Exception as e:
            self.emit("auth_error", f"Failed to request code: {type(e).__name__} - {str(e)}")

    async def sign_in_code(self, phone: str, code: str, password: Optional[str] = None):
        if not await self.init_client():
            return

        phone_clean = re.sub(r"[^\d]", "", str(phone or getattr(self, "phone", "") or ""))
        code_clean = str(code).strip()
        phone_code_hash = getattr(self, "phone_code_hash", None)

        try:
            self.emit("auth_status", "Verifying credentials with Telegram...")
            if password:
                await self.client.sign_in(password=password)
            else:
                if phone_code_hash:
                    await self.client.sign_in(phone_clean, code_clean, phone_code_hash=phone_code_hash)
                else:
                    await self.client.sign_in(phone_clean, code_clean)
            await self._resolve_account_id()
            self.emit("auth_success", None)
            self.emit("log", "Telegram authentication successful.")
        except SessionPasswordNeededError:
            self.emit("password_needed", None)
        except PhoneCodeInvalidError:
            self.emit("auth_error", "The OTP code you entered is invalid. Please check the code in your Telegram app.")
        except PhoneCodeExpiredError:
            self.emit("auth_error", "The OTP code has expired. Please click 'Send Code' to request a new code.")
        except FloodWaitError as fwe:
            self.emit("auth_error", f"Telegram rate limit: Please wait {fwe.seconds} seconds before attempting to sign in.")
        except RPCError as rpce:
            self.emit("auth_error", f"Telegram error: {rpce.message or str(rpce)}")
        except Exception as e:
            self.emit("auth_error", f"Login failed: {type(e).__name__} - {e}")

    async def fetch_dialogs(self, search_term: str = "") -> List[Dict[str, Any]]:
        if not await self.init_client():
            return []
        try:
            async for dialog in self.client.iter_dialogs():
                if isinstance(dialog.entity, (Chat, Channel)):
                    norm_id = normalize_channel_id(dialog.entity.id)
                    peer_type = "channel" if isinstance(dialog.entity, Channel) else "chat"
                    access_hash = getattr(dialog.entity, "access_hash", None)
                    username = getattr(dialog.entity, "username", "") or ""

                    dialog_dict = {
                        "id": norm_id,
                        "name": dialog.name,
                        "peer_type": peer_type,
                        "access_hash": access_hash,
                        "username": username,
                        "entity": dialog.entity
                    }
                    self.dialogs_cache[(peer_type, norm_id)] = dialog_dict
                    self.db.update_channel(norm_id, dialog.name)

            filtered = self.filter_cached_dialogs(search_term)
            self.emit("dialogs", filtered)
            return filtered
        except Exception as e:
            self.emit("log", f"Failed to fetch dialogs: {e}")
            return []

    def filter_cached_dialogs(self, search_term: str = "") -> List[Dict[str, Any]]:
        """Non-destructive in-memory filtering against master dialogs_cache."""
        term = (search_term or "").strip().lower()
        if not term:
            return list(self.dialogs_cache.values())
        return [
            d for d in self.dialogs_cache.values()
            if term in d["name"].lower() or (d.get("username") and term in d["username"].lower())
        ]

    async def resolve_selection_entity(self, selection: Dict[str, Any]):
        """
        Resolves a Telegram entity from selection dictionary using access hash cascade.
        """
        cid = normalize_channel_id(selection["channel_id"])
        peer_type = selection.get("peer_type", "channel")
        access_hash = selection.get("access_hash")

        # 1. Check in-memory master cache
        cache_key = (peer_type, cid)
        if cache_key in self.dialogs_cache:
            return self.dialogs_cache[cache_key]["entity"]

        # 2. Check Telethon session cache
        try:
            return await self.client.get_entity(cid)
        except Exception as e1:
            logger.debug(f"Direct get_entity({cid}) cache miss: {e1}")

        # 3. Reconstruct TL InputPeer using stored access_hash
        try:
            from telethon.tl import types
            if peer_type in ("channel", "supergroup") and access_hash is not None:
                input_peer = types.InputPeerChannel(channel_id=cid, access_hash=int(access_hash))
                return await self.client.get_entity(input_peer)
            elif peer_type == "chat":
                input_peer = types.InputPeerChat(chat_id=cid)
                return await self.client.get_entity(input_peer)
        except Exception as e2:
            logger.warning(f"Access hash resolution failed for {cid}: {e2}")

        return None

    def match_media(self, message, media_filters: Dict[str, bool], cutoff_date: Optional[datetime]) -> Tuple[bool, Optional[str], Optional[int]]:
        """
        Determines whether a message matches media filters and cutoff date.
        Returns: (allowed, suggested_filename, estimated_size)
        """
        if not getattr(message, "media", None):
            return False, None, None

        if cutoff_date and message.date and message.date < cutoff_date:
            return False, None, None

        # Photos
        if isinstance(message.media, MessageMediaPhoto):
            if not media_filters.get("Photos", False) and not media_filters.get("All Media", False):
                return False, None, None
            size = 0
            photo = getattr(message.media, "photo", None)
            if photo and getattr(photo, "sizes", None):
                sizes = [s.size for s in photo.sizes if hasattr(s, "size")]
                if sizes:
                    size = max(sizes)
            return True, f"photo_{message.id}.jpg", size

        # Documents (PDFs, Videos, Audios, Docs)
        if isinstance(message.media, MessageMediaDocument):
            doc = message.media.document
            mime = (getattr(doc, "mime_type", "") or "").lower()
            file_size = getattr(doc, "size", 0) or 0

            is_pdf = mime.endswith("pdf")
            is_video = mime.startswith("video/") or any(isinstance(a, DocumentAttributeVideo) for a in doc.attributes)
            is_audio = mime.startswith("audio/")
            is_image = mime.startswith("image/")
            is_doc = not is_video and not is_audio and not is_image

            allowed = False
            if media_filters.get("All Media", False):
                allowed = True
            elif is_image and media_filters.get("Photos", False):
                allowed = True
            elif is_pdf and media_filters.get("PDFs", False):
                allowed = True
            elif is_video and media_filters.get("Videos", False):
                allowed = True
            elif is_audio and media_filters.get("Voice/Audio", False):
                allowed = True
            elif is_doc and not is_pdf and media_filters.get("Documents (Other)", False):
                allowed = True

            if not allowed:
                return False, None, None

            file_name = "unknown"
            for attr in doc.attributes:
                if isinstance(attr, DocumentAttributeFilename) and attr.file_name:
                    file_name = sanitize_media_filename(attr.file_name)
                    break

            if file_name == "unknown":
                caption = (message.message or "").strip()
                if caption:
                    file_name = sanitize_media_filename(caption)
                else:
                    ext = ""
                    if is_video: ext = "mp4"
                    elif is_audio: ext = "ogg"
                    elif is_pdf: ext = "pdf"
                    elif is_image:
                        guessed = get_extension(message.media) or mimetypes.guess_extension(mime) or ""
                        ext = guessed.lstrip('.') if guessed else "jpg"
                    else:
                        guessed = get_extension(message.media) or mimetypes.guess_extension(mime) or ""
                        ext = guessed.lstrip('.') if guessed else "bin"
                    file_name = f"media_{message.id}.{ext}"

            return True, file_name, file_size

        return False, None, None

    def resolve_target_path(self, base_folder: Path, message_id: int, original_filename: str) -> Path:
        """
        Determines the safe destination path with collision disambiguation.
        If a file with the same name already exists from a DIFFERENT message ID,
        appends [msg_id] to prevent collision.
        """
        target = base_folder / original_filename
        if not target.suffix:
            target = target.with_suffix(".bin")

        if not target.exists():
            return target

        stem = target.stem
        suffix = target.suffix
        candidate = base_folder / f"{stem} [{message_id}]{suffix}"
        idx = 1
        while candidate.exists():
            candidate = base_folder / f"{stem} [{message_id}_{idx}]{suffix}"
            idx += 1
        return candidate

    async def get_or_fetch_forum_topics(self, entity, channel_id: int, force_refresh: bool = False) -> Dict[int, Dict[str, Any]]:
        """
        Enumerates and caches native forum topics for a forum supergroup using Telethon messages.GetForumTopicsRequest.
        Handles pagination, closed/hidden topics, General topic membership, and deleted topics.
        Caches in-memory and in SQLite forum_topics table to avoid repeated API requests.
        """
        norm_cid = normalize_channel_id(channel_id)
        if not force_refresh and norm_cid in self._forum_topics_cache:
            return self._forum_topics_cache[norm_cid]

        # Check database cache first if not force_refresh
        if not force_refresh:
            db_topics = self.db.get_forum_topics(norm_cid)
            if db_topics:
                cached_dict = {}
                for dt in db_topics:
                    cached_dict[dt["topic_id"]] = {
                        "id": dt["topic_id"],
                        "title": dt["title"],
                        "local_folder_name": dt["local_folder_name"],
                        "is_closed": bool(dt.get("is_closed", 0)),
                        "is_hidden": bool(dt.get("is_hidden", 0))
                    }
                self._forum_topics_cache[norm_cid] = cached_dict
                return cached_dict

        # If not in cache or forced, fetch from Telegram
        is_forum = getattr(entity, "forum", False) and getattr(entity, "megagroup", False)
        if not is_forum:
            self._forum_topics_cache[norm_cid] = {}
            return {}

        topics_dict: Dict[int, Dict[str, Any]] = {}
        offset_topic = 0
        offset_id = 0
        offset_date = None
        has_more = True
        page_limit = 100

        while has_more:
            try:
                res = await self.client(GetForumTopicsRequest(
                    peer=entity,
                    offset_date=offset_date,
                    offset_id=offset_id,
                    offset_topic=offset_topic,
                    limit=page_limit
                ))
            except Exception as e:
                logger.warning(f"Error fetching forum topics for channel {norm_cid}: {e}")
                break

            fetched_topics = getattr(res, "topics", [])
            if not fetched_topics:
                break

            for t in fetched_topics:
                t_id = getattr(t, "id", None)
                if t_id is None:
                    continue

                if isinstance(t, ForumTopicDeleted):
                    title = f"Deleted_Topic_{t_id}"
                    is_closed = True
                    is_hidden = True
                else:
                    raw_title = getattr(t, "title", None) or f"Topic_{t_id}"
                    title = raw_title.strip()
                    is_closed = bool(getattr(t, "closed", False))
                    is_hidden = bool(getattr(t, "hidden", False))

                safe_folder = self.db.resolve_unique_topic_folder_name(norm_cid, t_id, title)
                self.db.save_forum_topic(
                    channel_id=norm_cid,
                    topic_id=t_id,
                    title=title,
                    local_folder_name=safe_folder,
                    is_closed=is_closed,
                    is_hidden=is_hidden
                )
                topics_dict[t_id] = {
                    "id": t_id,
                    "title": title,
                    "local_folder_name": safe_folder,
                    "is_closed": is_closed,
                    "is_hidden": is_hidden
                }

            if len(fetched_topics) < page_limit:
                has_more = False
            else:
                last_t = fetched_topics[-1]
                offset_topic = getattr(last_t, "id", 0)
                offset_id = getattr(last_t, "top_message", 0)
                offset_date = getattr(last_t, "date", None)
                if not offset_topic:
                    has_more = False

        # Ensure General topic (topic_id=1) is registered if not explicitly present
        if 1 not in topics_dict:
            gen_folder = self.db.resolve_unique_topic_folder_name(norm_cid, 1, "General")
            self.db.save_forum_topic(
                channel_id=norm_cid,
                topic_id=1,
                title="General",
                local_folder_name=gen_folder,
                is_closed=False,
                is_hidden=False
            )
            topics_dict[1] = {
                "id": 1,
                "title": "General",
                "local_folder_name": gen_folder,
                "is_closed": False,
                "is_hidden": False
            }

        self._forum_topics_cache[norm_cid] = topics_dict
        logger.info(f"Enumerated {len(topics_dict)} forum topics for channel {norm_cid}.")
        return topics_dict

    def resolve_message_topic(
        self,
        message,
        channel_id: int,
        is_forum: bool,
        topics_dict: Dict[int, Dict[str, Any]]
    ) -> Tuple[Optional[int], Optional[str]]:
        """
        Determines the forum topic ID and verified title for a Telegram message.
        Follows MTProto reply_to header and topic root conventions.
        
        Rules:
          - Broadcast channel: (None, None)
          - Message ID matches known topic ID: (msg.id, topic_title) [Root message]
          - Message has reply_to.reply_to_top_id: (top_id, topic_title) [Nested reply]
          - Message has reply_to.forum_topic and reply_to_msg_id: (msg_id, topic_title) [Topic root reply]
          - No reply metadata in forum:
              - If General (topic 1) exists and not closed: (1, General title)
              - Otherwise: (None, 'Unclassified')
          - Ambiguous or unrecognized reply: (None, 'Unclassified')
        """
        if not is_forum:
            return None, None

        msg_id = getattr(message, "id", None)
        # 1. Topic root creation message
        if msg_id is not None and msg_id in topics_dict:
            return msg_id, topics_dict[msg_id]["title"]

        # 2. Check reply metadata
        reply = getattr(message, "reply_to", None)
        if reply is not None:
            # Check reply_to_top_id (MTProto thread top)
            top_id = getattr(reply, "reply_to_top_id", None)
            if top_id is not None:
                if top_id in topics_dict:
                    return top_id, topics_dict[top_id]["title"]
                else:
                    return top_id, f"Topic_{top_id}"

            # Check forum_topic flag + reply_to_msg_id
            is_forum_reply = getattr(reply, "forum_topic", False)
            reply_msg_id = getattr(reply, "reply_to_msg_id", None)
            if is_forum_reply and reply_msg_id is not None:
                if reply_msg_id in topics_dict:
                    return reply_msg_id, topics_dict[reply_msg_id]["title"]
                else:
                    return reply_msg_id, f"Topic_{reply_msg_id}"

            # Has reply_to but no top_id and forum_topic is False -> cross-reply / ambiguous
            return None, "Unclassified"

        # 3. Message has no reply metadata:
        # Check sufficient evidence for General topic (topic_id=1)
        if 1 in topics_dict and not topics_dict[1].get("is_closed", False):
            return 1, topics_dict[1]["title"]

        # Fallback to Unclassified if General topic does not exist or is closed
        return None, "Unclassified"

    def resolve_destination_path(
        self,
        channel_id: int,
        channel_name: str,
        raw_filename: str,
        message_id: int,
        channel_folder: Optional[Path] = None,
        is_forum: bool = False,
        topic_id: Optional[int] = None,
        topic_title: Optional[str] = None,
        peer_type: str = "channel",
        message_date: Optional[datetime] = None,
        expected_size: int = 0
    ) -> Tuple[Path, str, Optional[int], Optional[str]]:
        """
        Central destination-path resolver for both GUI and headless worker downloads.
        
        Structure:
          - Forum Supergroups:  <DownloadRoot>\<GroupName>\<TopicName>\<Filename>
          - Broadcast Channels: <DownloadRoot>\<GroupName>\<Filename>
          
        Guarantees:
          - Legacy compatibility: Preserves existing in-progress .part files and previously completed paths.
          - Sanitizes Windows-invalid characters, reserved names, and trims dots/spaces.
          - Preserves Unicode characters (Hindi, emojis, etc.).
          - Resolves duplicate topic titles and channel name collisions safely.
          - Traversal-proof: Resolved paths are strictly within download root.
          - Retains persistent directory mappings from SQLite.
        """
        download_root = self.config_mgr.get_download_dir()
        norm_cid = normalize_channel_id(channel_id)

        # 1. Legacy Compatibility Guard:
        # If a download record already exists in downloads table, check for existing physical file or .part file!
        rec = self.db.get_download_record(norm_cid, message_id)
        if rec:
            rec_dict = dict(rec)
            rel = rec_dict.get("relative_path")
            if rel:
                historical_target = download_root / rel
                historical_part = Path(f"{historical_target}.part")
                if historical_part.exists():
                    logger.info(f"Preserving existing .part resume path for message {message_id}: {historical_part}")
                    return historical_target, rel, rec_dict.get("topic_id"), rec_dict.get("topic_title")
                elif historical_target.exists():
                    logger.info(f"Preserving existing historical target path for message {message_id}: {historical_target}")
                    return historical_target, rel, rec_dict.get("topic_id"), rec_dict.get("topic_title")

        # 2. Determine Channel Folder
        if channel_folder is not None:
            channel_dir = channel_folder
        elif self.account_id:
            channel_folder_name = self.db.resolve_unique_channel_folder_name(
                account_id=self.account_id,
                channel_id=norm_cid,
                base_title=channel_name,
                peer_type=peer_type
            )
            # Persist mapping if not present
            if not self.db.get_channel_storage_mapping(self.account_id, norm_cid, peer_type):
                self.db.set_channel_storage_mapping(
                    account_id=self.account_id,
                    channel_id=norm_cid,
                    verified_title=channel_name,
                    local_folder_name=channel_folder_name,
                    is_forum=is_forum,
                    peer_type=peer_type
                )
            channel_dir = download_root / channel_folder_name
        else:
            channel_folder_name = sanitize_folder_name(channel_name)
            channel_dir = download_root / channel_folder_name

        # 3. Determine Topic Folder if Forum
        organize_topics = bool(self.config_mgr.config.get("organize_forum_topics", True))
        if is_forum and organize_topics:
            if topic_id is not None:
                effective_title = topic_title or f"Topic_{topic_id}"
                topic_folder_name = self.db.resolve_unique_topic_folder_name(norm_cid, topic_id, effective_title)
                # Persist topic if not present in DB
                if not self.db.get_forum_topic(norm_cid, topic_id):
                    self.db.save_forum_topic(norm_cid, topic_id, effective_title, topic_folder_name)
                target_dir = channel_dir / topic_folder_name
                eff_topic_id = topic_id
                eff_topic_title = effective_title
            else:
                # Ambiguous or unclassified message in forum supergroup
                target_dir = channel_dir / "Unclassified"
                eff_topic_id = None
                eff_topic_title = "Unclassified"
        else:
            # Broadcast channel or disabled topic organization -> flat layout
            target_dir = channel_dir
            eff_topic_id = None
            eff_topic_title = None

        # 4. Traversal guard
        if not target_dir.resolve().is_relative_to(download_root.resolve()):
            raise ValueError(f"Path traversal detected in destination path: {target_dir}")

        # 5. Filename resolution with collision disambiguation
        safe_fname = sanitize_media_filename(raw_filename)
        base_target = target_dir / safe_fname
        if not base_target.suffix:
            base_target = base_target.with_suffix(".bin")

        if base_target.exists():
            is_match = False
            if expected_size > 0 and base_target.stat().st_size == expected_size:
                if message_date is not None and abs(base_target.stat().st_mtime - message_date.timestamp()) <= 2.0:
                    is_match = True
            if is_match:
                target_path = base_target
            else:
                target_path = self.resolve_target_path(target_dir, message_id, safe_fname)
        else:
            target_path = base_target

        rel_path = str(target_path.relative_to(download_root))
        return target_path, rel_path, eff_topic_id, eff_topic_title

    async def download_single_message(
        self,
        message,
        channel_id: int,
        channel_folder: Path,
        raw_filename: str,
        expected_size: int,
        status_prefix: str = "",
        is_forum: bool = False,
        topic_id: Optional[int] = None,
        topic_title: Optional[str] = None,
        channel_name: str = ""
    ):
        """
        Executes safe download with crash-safe .part staging, topic routing, and SQLite tracking.
        """
        channel_id = normalize_channel_id(channel_id)
        ch_name = channel_name or channel_folder.name
        self.db.ensure_channel(channel_id, ch_name)

        # Central destination-path resolution
        msg_date = getattr(message, "date", None)
        target_path, rel_path, eff_topic_id, eff_topic_title = self.resolve_destination_path(
            channel_id=channel_id,
            channel_name=ch_name,
            raw_filename=raw_filename,
            message_id=message.id,
            channel_folder=channel_folder,
            is_forum=is_forum,
            topic_id=topic_id,
            topic_title=topic_title,
            message_date=msg_date,
            expected_size=expected_size
        )

        # 1. Check duplicate state in DB
        if self.db.is_message_downloaded(channel_id, message.id):
            if target_path.exists() and (expected_size == 0 or target_path.stat().st_size == expected_size):
                self.emit("log", f"Skipped (already in DB & on disk): {target_path.name}")
                return

        # 2. Check if file already exists with verified message timestamp metadata
        if target_path.exists() and expected_size > 0 and target_path.stat().st_size == expected_size:
            is_mtime_match = (
                msg_date is not None and
                abs(target_path.stat().st_mtime - msg_date.timestamp()) <= 2.0
            )
            if is_mtime_match:
                self.db.record_completed(
                    channel_id, message.id, target_path.name, expected_size, rel_path,
                    topic_id=eff_topic_id, topic_title=eff_topic_title
                )
                self.emit("log", f"Registered existing verified file: {target_path.name}")
                return

        # 3. Storage capacity pre-flight check
        try:
            self.config_mgr.check_capacity_for_file(expected_size)
        except StorageError as se:
            self.emit("log", f"STORAGE HALT: {se}")
            self.emit("storage_error", str(se))
            raise se

        target_path.parent.mkdir(parents=True, exist_ok=True)
        part_path = Path(f"{target_path}.part")
        self.db.record_start(
            channel_id, message.id, target_path.name, expected_size, rel_path,
            topic_id=eff_topic_id, topic_title=eff_topic_title
        )
        self.emit("log", f"{status_prefix}Downloading: {target_path.name} ({format_size(expected_size)})")

        last_update = time.monotonic()
        last_current = 0

        async def progress_cb(current, total):
            if self.cancel_flag:
                raise CancelledByUser("Download cancelled by user.")
            nonlocal last_update, last_current
            now = time.monotonic()
            elapsed = now - last_update
            if elapsed >= 0.5:
                speed = (current - last_current) / elapsed
                pct = (current / total * 100) if total else 0
                self.emit("progress", pct)
                status_msg = f"{status_prefix}{target_path.name} | {format_size(current)}/{format_size(total)} ({pct:.1f}%) | {format_size(speed)}/s"
                self.emit("status", status_msg)
                last_update = now
                last_current = current

        # Download into .part staging file with global concurrency = 1 and 512KB chunks
        async with self._download_semaphore:
            try:
                resume_offset = 0
                chunk_size = CHUNK_SIZE_BYTES  # 512 * 1024

                if part_path.exists():
                    existing_bytes = part_path.stat().st_size
                    if expected_size > 0 and existing_bytes >= expected_size:
                        try:
                            part_path.unlink()
                        except OSError:
                            pass
                    else:
                        resume_offset = (existing_bytes // chunk_size) * chunk_size
                        if resume_offset > 0:
                            self.emit("log", f"{status_prefix}Resuming {target_path.name} from {format_size(resume_offset)}...")

                is_document = (
                    isinstance(getattr(message, "media", None), MessageMediaDocument) and
                    hasattr(message.media, "document") and
                    getattr(message.media.document, "size", 0) > 0
                )

                if is_document and expected_size > 0:
                    mode = "r+b" if resume_offset > 0 else "wb"
                    current_bytes = resume_offset
                    with open(part_path, mode) as f:
                        if resume_offset > 0:
                            f.seek(resume_offset)
                            f.truncate(resume_offset)
                        async for chunk in self.client.iter_download(
                            message.media.document,
                            offset=resume_offset,
                            request_size=chunk_size
                        ):
                            if self.cancel_flag:
                                f.flush()
                                os.fsync(f.fileno())
                                raise CancelledByUser("Download cancelled by user.")
                            f.write(chunk)
                            current_bytes += len(chunk)
                            await progress_cb(current_bytes, expected_size)
                        f.flush()
                        os.fsync(f.fileno())
                else:
                    if part_path.exists() and resume_offset == 0:
                        try:
                            part_path.unlink()
                        except OSError:
                            pass
                    await self.client.download_media(message, file=part_path, progress_callback=progress_cb)

                # Verification: size check if expected_size known
                actual_size = part_path.stat().st_size if part_path.exists() else 0
                if expected_size > 0 and actual_size != expected_size:
                    raise IOError(f"Size mismatch: expected {expected_size} bytes, got {actual_size} bytes")

                # Prevent overwriting any existing valid media that may have appeared
                if target_path.exists():
                    is_mtime_match = (
                        message.date is not None and
                        abs(target_path.stat().st_mtime - message.date.timestamp()) <= 2.0
                    )
                    if expected_size > 0 and target_path.stat().st_size == expected_size and is_mtime_match:
                        if part_path.exists():
                            try:
                                part_path.unlink()
                            except OSError:
                                pass
                        self.db.record_completed(
                            channel_id, message.id, target_path.name, expected_size, rel_path,
                            topic_id=eff_topic_id, topic_title=eff_topic_title
                        )
                        self.emit("progress", 100)
                        self.emit("log", f"Existing valid file preserved: {target_path.name}")
                        return
                    else:
                        target_path = self.resolve_target_path(target_path.parent, message.id, target_path.name)
                        rel_path = str(target_path.relative_to(self.config_mgr.get_download_dir()))

                # Atomic promotion to final target path
                os.replace(part_path, target_path)

                if message.date:
                    mtime = message.date.timestamp()
                    os.utime(target_path, (mtime, mtime))

                self.db.record_completed(
                    channel_id, message.id, target_path.name, actual_size, rel_path,
                    topic_id=eff_topic_id, topic_title=eff_topic_title
                )
                self.emit("progress", 100)
                self.emit("log", f"Successfully completed: {target_path.name}")

            except CancelledByUser:
                self.emit("log", f"Cancelled: {target_path.name} (partial state preserved in .part)")
                raise

            except FloodWaitError as fwe:
                self.emit("log", f"Telegram Rate Limit (FloodWait): Sleeping for {fwe.seconds} seconds...")
                self.emit("status", f"Rate limited. Pausing for {fwe.seconds}s...")
                await asyncio.sleep(fwe.seconds)
                raise fwe

            except Exception as e:
                self.emit("log", f"Download failed for {target_path.name}: {e}")
                if part_path.exists():
                    try:
                        part_path.unlink()
                    except OSError:
                        pass
                self.db.record_failed(channel_id, message.id, str(e))
                raise e

    async def scan_and_download_channel(
        self,
        entity,
        channel_id: int,
        channel_name: str,
        media_filters: Dict[str, bool],
        cutoff_date: Optional[datetime],
        channel_idx: int = 1,
        total_channels: int = 1
    ):
        """Processes a single channel: scans messages, retries failed ones, downloads new ones."""
        download_root = self.config_mgr.get_download_dir()
        safe_folder_name = sanitize_folder_name(channel_name)
        channel_folder = download_root / safe_folder_name
        
        # Guard against directory traversal
        if not channel_folder.resolve().is_relative_to(download_root.resolve()):
            raise ValueError(f"Path traversal detected in channel name: {channel_name}")
            
        channel_id = normalize_channel_id(channel_id)
        peer_type = "channel" if isinstance(entity, Channel) else "chat"
        if self.account_id and not self.db.is_selected(self.account_id, channel_id, peer_type):
            self.emit("log", f"Skipping {channel_name}: removed from active Download List.")
            return

        channel_folder.mkdir(parents=True, exist_ok=True)
        self.emit("log", f"\n=== [{channel_idx}/{total_channels}] Channel: {channel_name} ===")

        # Upfront channel registration: Guarantee channel exists in DB before scanning or retrying
        self.db.update_channel(channel_id, channel_name)

        # Stage B: Forum Supergroup Topic Discovery & Enumeration
        is_forum = bool(getattr(entity, "forum", False) and getattr(entity, "megagroup", False))
        topics_dict = {}
        if is_forum:
            self.emit("status", f"Discovering forum topics for {channel_name}...")
            topics_dict = await self.get_or_fetch_forum_topics(entity, channel_id)
            self.emit("log", f"Detected native forum supergroup: {len(topics_dict)} topics available.")

        # First, check previously failed messages for retry
        failed_records = self.db.get_failed_messages(channel_id, max_retries=5)
        if failed_records:
            self.emit("log", f"Retrying {len(failed_records)} previously failed messages in {channel_name}...")
            for fail_rec in failed_records:
                if self.cancel_flag: return
                msg_id = fail_rec["message_id"]
                try:
                    msg = await self.client.get_messages(entity, ids=msg_id)
                    if msg:
                        allowed, fname, fsize = self.match_media(msg, media_filters, cutoff_date)
                        if allowed:
                            t_id = fail_rec.get("topic_id")
                            t_title = fail_rec.get("topic_title")
                            if t_id is None and is_forum:
                                t_id, t_title = self.resolve_message_topic(msg, channel_id, is_forum, topics_dict)
                            await self.download_single_message(
                                msg, channel_id, channel_folder, fname, fsize or 0,
                                status_prefix=f"[Retry {msg_id}] ",
                                is_forum=is_forum, topic_id=t_id, topic_title=t_title,
                                channel_name=channel_name
                            )
                except Exception as e:
                    logger.warning(f"Retry failed for message {msg_id}: {e}")

        # Now scan messages
        self.emit("status", f"Scanning messages in {channel_name}...")
        matching_messages = []
        highest_msg_id = 0

        async for msg in self.client.iter_messages(entity, limit=None):
            if self.cancel_flag: break
            if cutoff_date and msg.date and msg.date < cutoff_date:
                break
            if (msg.message or "") == COPYRIGHT_TEXT:
                continue

            highest_msg_id = max(highest_msg_id, msg.id)
            allowed, fname, fsize = self.match_media(msg, media_filters, cutoff_date)
            if allowed:
                # If already completed in DB, skip collecting
                if not self.db.is_message_downloaded(channel_id, msg.id):
                    t_id, t_title = self.resolve_message_topic(msg, channel_id, is_forum, topics_dict)
                    matching_messages.append((msg, fname, fsize or 0, t_id, t_title))

        if self.cancel_flag: return

        total_to_download = len(matching_messages)
        self.emit("log", f"Found {total_to_download} new matching files to download in {channel_name}.")

        has_database_error = False
        for i, (msg, fname, fsize, t_id, t_title) in enumerate(matching_messages, 1):
            if self.cancel_flag: break
            if self.account_id and not self.db.is_selected(self.account_id, channel_id, peer_type):
                self.emit("log", f"Stopping remaining downloads for {channel_name}: removed from Download List.")
                break
            prefix = f"[{channel_idx}/{total_channels}][{i}/{total_to_download}] "
            try:
                await self.download_single_message(
                    msg, channel_id, channel_folder, fname, fsize,
                    status_prefix=prefix,
                    is_forum=is_forum, topic_id=t_id, topic_title=t_title,
                    channel_name=channel_name
                )
            except FloodWaitError:
                # Retry once after FloodWait sleep
                await self.download_single_message(
                    msg, channel_id, channel_folder, fname, fsize,
                    status_prefix=prefix,
                    is_forum=is_forum, topic_id=t_id, topic_title=t_title,
                    channel_name=channel_name
                )
            except StorageError:
                raise  # Halt whole process on storage missing/full
            except sqlite3.Error as sqle:
                has_database_error = True
                self.emit("log", f"CRITICAL DATABASE ERROR on message {msg.id}: {sqle}")
                logger.critical(f"Database error on message {msg.id} in channel {channel_id}: {sqle}")
                self.emit("status", f"Halted channel {channel_name} due to database error: {sqle}")
                break
            except Exception as e:
                if isinstance(getattr(e, "__cause__", None), sqlite3.Error) or isinstance(getattr(e, "__context__", None), sqlite3.Error):
                    has_database_error = True
                    self.emit("log", f"CRITICAL DATABASE ERROR on message {msg.id}: {e}")
                    logger.critical(f"Database error on message {msg.id}: {e}")
                    self.emit("status", f"Halted channel {channel_name} due to database error: {e}")
                    break
                self.emit("log", f"Skipping message {msg.id} after error: {e}")

        # Checkpoint: Advance scan checkpoint only if no database error occurred and not cancelled
        if not has_database_error and not self.cancel_flag:
            self.db.update_channel(channel_id, channel_name, last_scanned_id=highest_msg_id)
            self.emit("log", f"Finished scanning {channel_name} (checkpoint: {highest_msg_id}).")
        elif has_database_error:
            self.emit("log", f"Scan checkpoint NOT advanced for {channel_name} due to database error.")
        else:
            self.emit("log", f"Scan checkpoint NOT advanced for {channel_name} (cancelled by user).")

    async def run_batch(self, selected_dialogs: List[Dict[str, Any]]):
        """Runs batch download for given list of dialog entities."""
        self.is_downloading = True
        self.cancel_flag = False
        self.emit("download_started", None)

        # Validate storage before beginning
        valid, msg, _ = self.config_mgr.validate_storage()
        if not valid:
            self.emit("log", f"CRITICAL STORAGE ERROR: {msg}")
            self.emit("storage_error", msg)
            self.is_downloading = False
            self.emit("download_ended", None)
            return

        media_filters = self.config_mgr.config.get("media_filters", {})
        date_str = self.config_mgr.config.get("cutoff_date", "").strip()
        cutoff_date = None
        if date_str:
            try:
                cutoff_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                self.emit("log", f"Invalid cutoff date format: {date_str}. Ignoring.")

        total_channels = len(selected_dialogs)
        try:
            for idx, d in enumerate(selected_dialogs, 1):
                if self.cancel_flag: break
                await self.scan_and_download_channel(
                    d["entity"], d["id"], d["name"], media_filters, cutoff_date,
                    channel_idx=idx, total_channels=total_channels
                )
        except StorageError as se:
            self.emit("log", f"DOWNLOAD ABORTED: {se}")
        except Exception as e:
            self.emit("log", f"Unexpected error during batch: {e}")
        finally:
            self.is_downloading = False
            if self.cancel_flag:
                self.emit("status", "Download process stopped.")
                self.emit("log", "\n--- Downloads Stopped by User ---")
            else:
                self.emit("status", "All downloads completed.")
                self.emit("log", "\n--- All Downloads Finished ---")
            self.emit("download_ended", None)
