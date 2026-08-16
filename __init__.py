from .nodes.aspect_ratio import AspectRatio
from .nodes.save_audio_mp3 import SaveAudioMP3
from .nodes.save_images import SaveImages
from .nodes.frame_save import FrameSave
from .nodes.save_video import SaveVideo
from .nodes.load_video import LoadVideo

# NOTE: these keys are what saved workflows store on disk. Renaming one breaks
# every workflow that already uses it, so a rename must be paired with a
# migration pass over the workflow JSON. Renamed from the upstream
# ComfyUI-Dehypnotic ids (SaveVideoDehypnotic etc.) on 2026-08-15.
# Display names below are cosmetic and safe to change freely.
NODE_CLASS_MAPPINGS = {
    "pixelscience_AspectRatio": AspectRatio,
    "pixelscience_SaveAudioMP3": SaveAudioMP3,
    "pixelscience_SaveImages": SaveImages,
    "pixelscience_FrameSave": FrameSave,
    "pixelscience_SaveVideo": SaveVideo,
    "pixelscience_LoadVideo": LoadVideo,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "pixelscience_AspectRatio": "AspectRatio (pixelscience)",
    "pixelscience_SaveAudioMP3": "Save Audio (pixelscience)",
    "pixelscience_SaveImages": "Save Images (pixelscience)",
    "pixelscience_FrameSave": "FrameSave (pixelscience)",
    "pixelscience_SaveVideo": "Save Video (pixelscience)",
    "pixelscience_LoadVideo": "Load Video (pixelscience)",
}

WEB_DIRECTORY = "./js"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
