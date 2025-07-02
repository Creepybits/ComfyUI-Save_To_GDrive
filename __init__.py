import os
import sys
from .SaveImageToGoogleDrive import NODE_CLASS_MAPPINGS as nodes_NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS as nodes_NODE_DISPLAY_NAME_MAPPINGS

__version__ = "1.1.1"

# Define the web directory for ComfyUI to find our JavaScript files
WEB_DIRECTORY = "./web"

NODE_CLASS_MAPPINGS = {
    **nodes_NODE_CLASS_MAPPINGS,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    **nodes_NODE_DISPLAY_NAME_MAPPINGS,
}

__all__ = ['NODE_CLASS_MAPPINGS', 'NODE_DISPLAY_NAME_MAPPINGS', 'WEB_DIRECTORY']
