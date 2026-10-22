import os
import numpy as np
from PIL import Image
from PIL.PngImagePlugin import PngInfo
import threading
import json
import uuid
import folder_paths

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ['https://www.googleapis.com/auth/drive.file']
DEFAULT_FOLDER = "ComfyUI_Saves"

# Files under this size are sent in a single request; bigger ones use a resumable upload.
RESUMABLE_THRESHOLD_BYTES = 5 * 1024 * 1024

NODE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_PATH = os.path.join(NODE_DIR, "json", "credentials.json")
TOKEN_PATH = os.path.join(NODE_DIR, "json", "token.json")


def _read_json(path):
    """Returns the parsed file, or None if it's missing or still the placeholder text shipped with the node."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _escape_query_value(value):
    # Drive query strings are quoted with '; a folder name like "Bob's renders" would otherwise break the query.
    return value.replace("\\", "\\\\").replace("'", "\\'")


class SaveImageToGoogleDrive:
    # Shared across runs: the Drive client and the folder IDs already looked up.
    _service = None
    _creds = None
    _folder_cache = {}
    _lock = threading.Lock()

    def __init__(self):
        self.output_dir = folder_paths.get_temp_directory()
        self.type = "temp"

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "image": ("IMAGE",),
                "google_drive_folder_path": ("STRING", {"default": DEFAULT_FOLDER}),
                "filename_prefix": ("STRING", {"default": "ComfyUI_Image_"}),
                "file_format": (["PNG", "JPEG", "WEBP"], {"default": "PNG"}),
                "quality": ("INT", {"default": 90, "min": 1, "max": 100, "step": 1}),
            },
            "hidden": {
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO"
            },
        }

    RETURN_TYPES = ("IMAGE", "STRING",)
    RETURN_NAMES = ("image", "status",)

    FUNCTION = "save_image"

    OUTPUT_NODE = True

    CATEGORY = "Creepybits/Google Drive"

    def authenticate_google_drive(self):
        os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)

        creds = self.__class__._creds
        if creds is None:
            token_info = _read_json(TOKEN_PATH)
            if token_info:
                try:
                    creds = Credentials.from_authorized_user_info(token_info, SCOPES)
                except Exception as e:
                    print(f"Google Drive API Warning: Could not load token file {TOKEN_PATH}. Will attempt re-authentication. Error: {e}")
                    creds = None

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as e:
                    print(f"Google Drive API Error refreshing token. Will initiate full re-authentication flow. Error: {e}")
                    creds = None
            if not creds or not creds.valid:
                client_config = _read_json(CREDENTIALS_PATH)
                if not client_config:
                    print(f"Google Drive API Error: {CREDENTIALS_PATH} is missing or still the placeholder. "
                          "Download your OAuth client file from Google Cloud Console and save it there.")
                    return None
                try:
                    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
                    creds = flow.run_local_server(port=0)
                except Exception as e:
                    print(f"Google Drive API Error during authentication flow: {e}")
                    return None

            try:
                with open(TOKEN_PATH, 'w') as token:
                    token.write(creds.to_json())
            except Exception as e:
                print(f"Google Drive API Warning: Could not save token file to {TOKEN_PATH}. You may need to re-authenticate next time. Error: {e}")

            # New credentials need a new client.
            self.__class__._service = None

        self.__class__._creds = creds
        return creds

    def get_service(self, creds):
        if self.__class__._service is None:
            self.__class__._service = build('drive', 'v3', credentials=creds, cache_discovery=False)
        return self.__class__._service

    def get_folder_id(self, service, path_parts):
        """Finds (or creates) each folder in the path. Results are cached so repeated saves to
        the same folder skip the lookups."""
        cache_key = "/".join(path_parts)
        cached = self._folder_cache.get(cache_key)
        if cached:
            return cached

        current_parent_id = 'root'
        for folder_name in path_parts:
            folder_exists_query = (f"name='{_escape_query_value(folder_name)}' and mimeType='application/vnd.google-apps.folder' "
                                   f"and '{current_parent_id}' in parents and trashed=false")
            response = service.files().list(q=folder_exists_query,
                                            spaces='drive',
                                            fields='files(id, name)',
                                            pageSize=1).execute()
            found_folders = response.get('files', [])

            if found_folders:
                current_parent_id = found_folders[0]['id']
            else:
                folder_metadata = {
                    'name': folder_name,
                    'mimeType': 'application/vnd.google-apps.folder',
                    'parents': [current_parent_id]
                }
                created_folder = service.files().create(body=folder_metadata, fields='id').execute()
                current_parent_id = created_folder.get('id')

        self._folder_cache[cache_key] = current_parent_id
        return current_parent_id

    def save_image(self, image, google_drive_folder_path, filename_prefix, file_format, quality, prompt=None, extra_pnginfo=None):
        print("Google Drive Save: Starting...")

        chosen_format_upper = file_format.upper()
        file_extension = {"JPEG": "jpeg", "WEBP": "webp"}.get(chosen_format_upper, "png")
        upload_mimetype = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[chosen_format_upper]

        metadata = None
        if chosen_format_upper == "PNG":
            metadata = PngInfo()
            if prompt is not None:
                metadata.add_text("prompt", json.dumps(prompt))
            if extra_pnginfo is not None:
                for k, v in extra_pnginfo.items():
                    metadata.add_text(k, json.dumps(v))

        # Save every image in the batch locally (also used for the preview). This used to upload only the first one.
        os.makedirs(self.output_dir, exist_ok=True)
        local_files = []
        for i in range(image.shape[0]):
            image_pil = Image.fromarray(np.clip(image[i].cpu().numpy() * 255.0, 0, 255).astype(np.uint8))
            if chosen_format_upper == "JPEG" and image_pil.mode != "RGB":
                image_pil = image_pil.convert("RGB")

            temp_filename_unique = f"comfy_gdrive_temp_{uuid.uuid4().hex}.{file_extension}"
            temp_filepath_local = os.path.join(self.output_dir, temp_filename_unique)
            try:
                if chosen_format_upper == "PNG":
                    image_pil.save(temp_filepath_local, pnginfo=metadata, compress_level=4)
                else:
                    image_pil.save(temp_filepath_local, quality=quality)
            except Exception as e:
                status_message = f"Google Drive Save Error: Could not save temporary image file to {temp_filepath_local}. {e}"
                print(status_message)
                return {"ui": {"images": []}, "result": (image, status_message,)}
            local_files.append(temp_filename_unique)

        previews = [{"filename": f, "subfolder": "", "type": self.type} for f in local_files]

        path_parts = [part for part in google_drive_folder_path.strip().strip('/').split('/') if part.strip()] or [DEFAULT_FOLDER]
        display_path = "/".join(path_parts)

        with self._lock:
            creds = self.authenticate_google_drive()
            if not creds:
                status_message = "Google Drive Save Error: Authentication failed. Check console for details."
                print(status_message)
                return {"ui": {"images": previews}, "result": (image, status_message,)}

            try:
                service = self.get_service(creds)
                target_folder_id = self.get_folder_id(service, path_parts)
            except Exception as e:
                # A cached folder may have been deleted on Drive; look it up fresh next time.
                self._folder_cache.pop(display_path, None)
                status_message = f"Google Drive Save Error processing folder path '{google_drive_folder_path}'. {e}"
                print(status_message)
                return {"ui": {"images": previews}, "result": (image, status_message,)}

            statuses = []
            for i, temp_filename in enumerate(local_files):
                # A single image keeps the plain "<prefix>.<ext>" name; batches are numbered.
                final_filename = f"{filename_prefix}.{file_extension}" if len(local_files) == 1 else f"{filename_prefix}_{i + 1:03d}.{file_extension}"
                temp_filepath_local = os.path.join(self.output_dir, temp_filename)
                try:
                    media = MediaFileUpload(temp_filepath_local,
                                            mimetype=upload_mimetype,
                                            resumable=os.path.getsize(temp_filepath_local) > RESUMABLE_THRESHOLD_BYTES,
                                            chunksize=1024*1024)
                    file = service.files().create(body={'name': final_filename, 'parents': [target_folder_id]},
                                                  media_body=media,
                                                  fields='id').execute()
                    statuses.append(f"Successfully uploaded '{final_filename}' to folder '{display_path}'. File ID: {file.get('id')}")
                except Exception as e:
                    if "notFound" in str(e) or "File not found" in str(e):
                        self._folder_cache.pop(display_path, None)
                    statuses.append(f"Error: Could not upload file '{final_filename}'. {e}")
                print(f"Google Drive Save: {statuses[-1]}")

        status_message = "Google Drive Save: " + " | ".join(statuses)
        return {"ui": {"images": previews}, "result": (image, status_message,)}


NODE_CLASS_MAPPINGS = {
    "SaveImageToGoogleDrive": SaveImageToGoogleDrive
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "SaveImageToGoogleDrive": "Save Image to Google Drive (Creepybits)"
}
