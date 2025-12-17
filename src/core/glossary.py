import os
import yaml
import logging
from pathlib import Path
from typing import Dict
try:
    from src.utils.paths import get_resource_path
except ImportError:
    # Fallback for direct execution tests if needed, but relative imports usually tricky there
    import sys
    def get_resource_path(relative_path):
        try:
            base_path = sys._MEIPASS
        except Exception:
            base_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return os.path.join(base_path, relative_path)

logger = logging.getLogger(__name__)

class EVEGlossary:
    """
    Handles replacement of common EVE Online terminology 
    from source language (Chinese/Slang) to standard English (or Target) terms.
    Loads terms from YAML configuration files.
    """
    
    def __init__(self, source_lang='zh', target_lang='en'):
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.terms: Dict[str, str] = self._load_glossary()
        
        # Sort terms by length (descending) to ensure longest matches are replaced first.
        self.sorted_terms = sorted(self.terms.items(), key=lambda x: len(x[0]), reverse=True)
        
    def _load_glossary(self) -> Dict[str, str]:
        """Load glossary from YAML files with fallback."""
        terms = {}
        
        # 1. Bundled Glossary
        filename = f"{self.source_lang}_{self.target_lang}.yml"
        # Adjusted path: src/core/glossary.py -> up 2 levels -> data/glossaries
        # Using get_resource_path to handle PyInstaller
        bundled_path = get_resource_path(os.path.join("data", "glossaries", filename))
        
        if os.path.exists(bundled_path):
            terms.update(self._load_yaml_glossary(bundled_path))
            logger.info(f"Loaded bundled glossary: {filename} ({len(terms)} terms)")
        else:
            logger.warning(f"Bundled glossary not found: {bundled_path}")
            
        # 2. User Custom Glossary (~/.eve_translator/glossaries/)
        user_dir = Path.home() / ".eve_translator" / "glossaries"
        user_file = user_dir / f"custom_{filename}"
        
        if user_file.exists():
             custom_terms = self._load_yaml_glossary(str(user_file))
             terms.update(custom_terms)
             logger.info(f"Loaded custom glossary: {user_file.name} ({len(custom_terms)} terms)")

        # 3. Hardcoded Fallback (only if empty and is default zh->en)
        if not terms and self.source_lang == 'zh' and self.target_lang == 'en':
            logger.warning("No glossary files found. Using hardcoded fallback.")
            return self._get_hardcoded_fallback()
            
        return terms

    def _load_yaml_glossary(self, filepath: str) -> Dict[str, str]:
        """Load and flatten YAML glossary file."""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
            
            if not data:
                return {}
                
            # Flatten everything except 'meta' key
            flattened = {}
            for key, value in data.items():
                if key == 'meta': continue
                if isinstance(value, dict):
                    flattened.update(self._flatten_dict(value))
            
            return flattened
        except Exception as e:
            logger.error(f"Error loading glossary {filepath}: {e}")
            return {}

    def _flatten_dict(self, nested_dict: dict) -> Dict[str, str]:
        """Recursively flatten nested dictionary."""
        flattened = {}
        for key, value in nested_dict.items():
            if isinstance(value, dict):
                flattened.update(self._flatten_dict(value))
            elif isinstance(value, str):
                flattened[key] = value
        return flattened

    def replace_terms(self, text: str) -> str:
        """
        Replaces known terms in the text with their English equivalents.
        """
        if not text:
            return text
            
        processed = text
        # Use simple iteration but with regex for safety on alphanumeric terms
        import re

        for term, replacement in self.sorted_terms:
            if not term.strip():
                continue
            
            # Use regex for alphanumeric terms (prevents '00' matching inside '1600')
            # Check if term is basically "word characters"
            if re.match(r'^[a-zA-Z0-9]+$', term):
                # \b matches word boundary. 
                # Note: We replaced `term` with `replacement` surrounded by spaces.
                # However, if we blindly replace, we might re-replace things we just replaced?
                # sorted_terms is by length descending, so 1600 replaced before 00.
                # But '00' finds parts of '1600mm'.
                # Boundary check solves this: \b00\b won't match inside 1600mm.
                pattern = r'\b' + re.escape(term) + r'\b'
                # Check if it matches before compiling replacement? regex sub handles it.
                processed = re.sub(pattern, f" {replacement} ", processed)
            else:
                # Fallback to literal replacement for CJK or symbols (like +1) where \b isn't applicable
                if term in processed:
                    processed = processed.replace(term, f" {replacement} ")
                
        # Clean up double spaces introduced
        return " ".join(processed.split())

    def _get_hardcoded_fallback(self) -> Dict[str, str]:
        """Original hardcoded dictionary for safety."""
        return {
             # Ships
            "穿梭机": "Shuttle",
            "蛋": "Pod",
            "截击": "Interceptor",
            "隐轰": "Bomber",
            "战术驱逐": "T3 Des",
            "佩刀": "Sabre",
            "巡洋": "Cruiser",
            "毒蜥": "Gila",
            "伊什塔": "Ishtar",
            "YST": "Ishtar",
            "海狂怒": "VNI",
            "奥萨斯": "Orthrus",
            "斯特拉修斯": "Stratios",
            "阿斯特罗": "Astero",
            "幼龙": "Drake",
            "金鹏": "Tengu",
            "洛基": "Loki",
            "军团": "Legion",
            "圣卒": "Proteus",
            "战列": "Battleship",
            "灾难": "Apocalypse",
            "地狱天使": "Abaddon",
            "末日沙场": "Armageddon",
            "万王宝座": "Megathron",
            "万王": "Megathron",
            "马克瑞": "Machariel",
            "小马": "Machariel",
            "噩梦": "Nightmare",
            "巴格斯": "Barghest",
            "响尾蛇": "Rattlesnake",
            "复仇者": "Vindicator",
            "乌鸦": "Raven",
            "鹏鲲": "Rokh",
            "台风": "Typhoon",
            "多米": "Dominix",
            "无畏": "Dreadnought",
            "小航": "Carrier",
            "大航": "Supercarrier",
            "泰坦": "Titan",
            "神使": "Avatar",
            "夜魔": "Nyx",
            "归魂": "Hel",
            "拉格": "Ragnarok",
            "勒维": "Leviathan",
            "俄洛巴": "Erebus",
            "大鲸鱼": "Rorqual",
            "小希": "Hic",
            "重拦": "HIC",
            "轻拦": "Dictor",
            # Modules & Actions
            "诱导": "Cyno",
            "隐秘诱导": "Covert Cyno",
            "泡泡": "Bubble",
            "隐身": "Cloak",
            "推子": "MWD",
            "加力": "AB",
            "反跳": "Scram",
            "长反": "Point",
            "网子": "Web",
            "毁电": "Neut",
            "吸电": "Nos",
            "炸弹": "Smartbomb",
            "末日": "Doomsday",
            "吉他": "Jita",
            "底特": "Delve",
            "高安": "Highsec",
            "低安": "Lowsec",
            "00": "Nullsec",
            "虫洞": "Wormhole",
            "死亡": "DED/Deadspace",
            "收": "WTB",
            "出": "WTS",
            "抓": "Tackled",
            "抓到了": "Tackled",
            "蹲": "Camping",
            "跳": "Jump",
            "过门": "Gate Jump",
            "刷怪": "Ratting",
            "挖矿": "Mining",
            "扫描": "Scanning",
            "收割": "Roaming",
            "舰队": "Fleet",
            "指挥": "FC",
            "本地": "Local",
            "蓝加": "+1",
            "救援": "Help/Cyno",
            "9命": "Help!",
            "打得不错": "Good Fight",
            "打得好": "Good Fight",
            "辛苦了": "GF / Good Job",
            "88": "Bye",
            "886": "Bye",
            "666": "Awesome",
            "牛逼": "Awesome/Badass",
            "nb": "Awesome",
            "GG": "Good Game",
            "武运昌隆": "Good Luck",
            "对齐": "Align to",
            "跃迁": "Warp to",
            "集火": "Primary",
            "转火": "Switch Primary",
            "锁定": "Lock",
            "停船": "Stop Ship",
            "接近": "Approach",
            "环绕": "Orbit",
            "进站": "Dock",
            "出站": "Undock",
            "起跳": "Warping",
            "朝向": "Aligning",
            "不要过门": "Do not jump",
            "红": "Red/Stop",
            "绿": "Green/Go",
            "自由土": "Free burn",
            "土": "Free burn",
            "舰队长": "FC",
            "终点": "Destination",
            "亮诱导": "Lighting Cyno",
            "四散": "Starburst",
            "不要广播炸弹伤害": "No Bomb Broadcast",
            "跟走位": "Anchor up",
            "大鱼": "Rorqual",
            "火力": "DPS",
            "抓人": "Tackle",
            "保持距离": "Keep at Range",
            "跟": "Keep at Range/Follow",
            "跟上": "Keep at Range/Follow",
            "锚定": "Anchor up",
            "集合": "Anchor up/Regroup",
            "安塞克斯": "Ansiblex",
            "跳桥": "Jump Bridge",
            "走跳桥": "Take Jump Bridge",
            "走星门": "Take Gate",
            "超载": "Overheat",
            "开起推子": "Prop On",
            "关推子": "Prop Off",
            "开推": "Prop On",
            "关推": "Prop Off",
            "打泡泡": "Shoot Bubble",
            "拉泡泡": "Bubble Up",
            "下泡泡": "Bubble Up",
            "收泡泡": "Bubble Down",
            "修": "Repair/Logi",
            "后勤": "Logistics",
            "摇修": "Rep me",
            "给电": "Cap me",
            "注油": "Cap me",
            "开火": "Fire",
            "停火": "Cease Fire",
            "撒": "Scatter",
            "解散": "Scatter/Disband"
        }
