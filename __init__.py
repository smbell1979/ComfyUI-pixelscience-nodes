from .nodes.aspect_ratio import AspectRatio
from .nodes.save_audio_mp3 import SaveAudioMP3
from .nodes.save_images import SaveImages
from .nodes.frame_save import FrameSave
from .nodes.save_video import SaveVideo
from .nodes.load_video import LoadVideo

# NOTE: these keys are what saved workflows store on disk. They deliberately
# match the upstream ComfyUI-Dehypnotic ids so existing graphs keep loading.
# Renaming one breaks every workflow that already uses it. Display names below
# are cosmetic and safe to change.
NODE_CLASS_MAPPINGS = {
    "dehypnotic_AspectRatio": AspectRatio,
    "SaveAudioMP3Dehypnotic": SaveAudioMP3,
    "SaveImagesDehypnotic": SaveImages,
    "FrameSaveDehypnotic": FrameSave,
    "SaveVideoDehypnotic": SaveVideo,
    "LoadVideoDehypnotic": LoadVideo,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "dehypnotic_AspectRatio": "AspectRatio (pixelscience)",
    "SaveAudioMP3Dehypnotic": "Save Audio (pixelscience)",
    "SaveImagesDehypnotic": "Save Images (pixelscience)",
    "FrameSaveDehypnotic": "FrameSave (pixelscience)",
    "SaveVideoDehypnotic": "Save Video (pixelscience)",
    "LoadVideoDehypnotic": "Load Video (pixelscience)",
}

WEB_DIRECTORY = "./js"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
