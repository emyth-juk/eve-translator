import os
import sys

def get_resource_path(relative_path):
    """
    Get absolute path to resource, works for dev and for PyInstaller.
    
    In PyInstaller/Frozen mode:
        sys._MEIPASS is the temp folder where app is unpacked.
        resources are located relative to this root.
        
    In Development mode:
        Root is 2 levels up from src/utils/paths.py -> src/utils -> src -> Root
    """
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
    except Exception:
        # src/utils/paths.py -> src/utils -> src -> Root
        base_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    return os.path.join(base_path, relative_path)
