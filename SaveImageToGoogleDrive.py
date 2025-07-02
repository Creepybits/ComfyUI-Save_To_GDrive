import os
import comfy.sd
import comfy.utils
import torch
import numpy as np
from PIL import Image
from PIL.PngImagePlugin import PngInfo
import time
import threading
import json
import random
import string
import folder_paths

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ['https://www.googleapis.com/auth/drive.file']


class SaveImageToGoogleDrive:
    def __init__(self):
        self.output_dir = folder_paths.get_temp_directory()
        self.type = "temp"

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "image": ("IMAGE",),
                "google_drive_folder_path": ("STRING", {"default": "ComfyUI_Saves"}),
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
        node_dir = os.path.dirname(os.path.abspath(__file__))
        credentials_path = os.path.join(node_dir, "json", "credentials.json")
        token_path = os.path.join(node_dir, "json", "token.json")

        os.makedirs(os.path.join(node_dir, "json"), exist_ok=True)

        creds = None
        if os.path.exists(token_path):
            try:
                creds = Credentials.from_authorized_user_file(token_path, SCOPES)
            except Exception as e:
                 print(f"Google Drive API Warning: Could not load token file {token_path}. Will attempt re-authentication. Error: {e}")
                 creds = None

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as e:
                    print(f"Google Drive API Error refreshing token. Will initiate full re-authentication flow. Error: {e}")
                    creds = None
            if not creds:
                try:
                    flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
                    creds = flow.run_local_server(port=0)
                except FileNotFoundError:
                     print(f"Google Drive API Error: credentials.json not found at {credentials_path}. Please make sure you have downloaded it from Google Cloud Console and placed it there.")
                     return None
                except Exception as e:
                     print(f"Google Drive API Error during authentication flow: {e}")
                     return None

            try:
                with open(token_path, 'w') as token:
                    token.write(creds.to_json())
            except Exception as e:
                 print(f"Google Drive API Warning: Could not save token file to {token_path}. You may need to re-authenticate next time. Error: {e}")

        return creds

    def save_image(self, image, google_drive_folder_path, filename_prefix, file_format, quality, prompt=None, extra_pnginfo=None):
        status_message = "Google Drive Save: Starting..."
        print(status_message)

        if image.shape[0] > 1:
            image_to_process = image[0].unsqueeze(0)
        else:
            image_to_process = image
        image_np = image_to_process[0].numpy() * 255.0
        image_pil = Image.fromarray(np.clip(image_np, 0, 255).astype(np.uint8))

        chosen_format_upper = file_format.upper()
        file_extension = "png"

        if chosen_format_upper == "JPEG":
            file_extension = "jpeg"
            if image_pil.mode == 'RGBA':
                image_pil = image_pil.convert("RGB")
        elif chosen_format_upper == "WEBP":
            file_extension = "webp"
            if image_pil.mode == 'P':
                image_pil = image_pil.convert("RGBA")
            elif image_pil.mode == 'RGBA' and not image_pil.getchannel('A').getbbox():
                 image_pil = image_pil.convert("RGB")

        temp_filename_unique = f"comfy_gdrive_temp_{os.getpid()}_{threading.current_thread().ident}_{int(time.time())}_{''.join(random.choice(string.ascii_lowercase + string.digits) for i in range(8))}.{file_extension}"
        temp_filepath_local = os.path.join(self.output_dir, temp_filename_unique)

        metadata = None
        if chosen_format_upper == "PNG":
            metadata = PngInfo()
            if prompt is not None:
                metadata.add_text("prompt", json.dumps(prompt))
            if extra_pnginfo is not None:
                for k, v in extra_pnginfo.items():
                    metadata.add_text(k, json.dumps(v))

        try:
            os.makedirs(self.output_dir, exist_ok=True)
            if chosen_format_upper == "PNG":
                image_pil.save(temp_filepath_local, pnginfo=metadata, compress_level=4)
            elif chosen_format_upper in ["JPEG", "WEBP"]:
                image_pil.save(temp_filepath_local, quality=quality)
            else:
                image_pil.save(temp_filepath_local)
            image_pil.close()
        except Exception as e:
            status_message = f"Google Drive Save Error: Could not save temporary image file to {temp_filepath_local}. {e}"
            print(status_message)
            return {"ui": {"images": []}, "result": (image, status_message,)}

        preview_dict = {
            "filename": temp_filename_unique,
            "subfolder": "",
            "type": self.type
        }

        creds = self.authenticate_google_drive()
        if not creds:
            status_message = "Google Drive Save Error: Authentication failed. Check console for details."
            print(status_message)
            return {"ui": {"images": [preview_dict]}, "result": (image, status_message,)}

        target_folder_id = None
        try:
            service = build('drive', 'v3', credentials=creds)

            if not google_drive_folder_path or google_drive_folder_path.strip() == "" or google_drive_folder_path.strip() == "/":
                path_parts = [self.INPUT_TYPES()["required"]["google_drive_folder_path"][1]["default"]]
            else:
                path_parts = [part for part in google_drive_folder_path.strip('/').split('/') if part]
                if not path_parts:
                    path_parts = [self.INPUT_TYPES()["required"]["google_drive_folder_path"][1]["default"]]

            current_parent_id = 'root'

            for i, folder_name in enumerate(path_parts):
                folder_exists_query = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and '{current_parent_id}' in parents and trashed=false"
                response = service.files().list(q=folder_exists_query,
                                                spaces='drive',
                                                fields='files(id, name)',
                                                pageSize=1).execute()
                found_folders = response.get('files', [])

                if found_folders:
                    current_folder_id = found_folders[0]['id']
                else:
                    folder_metadata = {
                        'name': folder_name,
                        'mimeType': 'application/vnd.google-apps.folder',
                        'parents': [current_parent_id]
                    }
                    created_folder = service.files().create(body=folder_metadata, fields='id').execute()
                    current_folder_id = created_folder.get('id')

                current_parent_id = current_folder_id
                if i == len(path_parts) - 1:
                    target_folder_id = current_folder_id

            if not target_folder_id:
                status_message = f"Google Drive Save Error: Could not determine target folder ID for path '{google_drive_folder_path}'."
                print(status_message)
                return {"ui": {"images": [preview_dict]}, "result": (image, status_message,)}

        except Exception as e:
            status_message = f"Google Drive Save Error processing folder path '{google_drive_folder_path}'. {e}"
            print(status_message)
            return {"ui": {"images": [preview_dict]}, "result": (image, status_message,)}


        uploaded_file_id = None
        try:
            final_filename = f"{filename_prefix}.{file_extension}"
            file_metadata = {
                'name': final_filename,
                'parents': [target_folder_id]
            }

            mimetype_map = {
                "PNG": "image/png",
                "JPEG": "image/jpeg",
                "WEBP": "image/webp"
            }
            upload_mimetype = mimetype_map.get(chosen_format_upper, 'application/octet-stream')

            media = MediaFileUpload(temp_filepath_local,
                                    mimetype=upload_mimetype,
                                    resumable=True,
                                    chunksize=1024*1024)
            file = service.files().create(body=file_metadata,
                                            media_body=media,
                                            fields='id,webContentLink,webViewLink').execute()
            uploaded_file_id = file.get('id')

            display_path = "/".join(path_parts)
            status_message = f"Google Drive Save: Successfully uploaded '{final_filename}' to folder '{display_path}'. File ID: {uploaded_file_id}"
            print(status_message)

        except Exception as e:
            status_message = f"Google Drive Save Error: Could not upload file '{os.path.basename(temp_filepath_local)}'. {e}"
            print(status_message)

        return {"ui": {"images": [preview_dict]}, "result": (image, status_message,)}


NODE_CLASS_MAPPINGS = {
    "SaveImageToGoogleDrive": SaveImageToGoogleDrive
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "SaveImageToGoogleDrive": "Save Image to Google Drive (Creepybits)"
}