"""Haven's discrete color presets, shared by commands and state.

These mappings are not a device capability discovery result. The integration
uses SetColor with a preset ID; no continuous-Kelvin endpoint is established.
The white IDs, named colors, and pattern IDs were checked against the settings
and colorDefinitions response captured during the Homey work on 2025-05-26.
Pattern IDs establish reported names, not a verified pattern/rate write API.
"""

HAVEN_KELVIN_MAP = {
    2700: 1, 3000: 2, 3500: 3, 3700: 4,
    4000: 5, 4400: 6, 4700: 7, 5000: 8,
}

HAVEN_RGB_MAP = {
    (255, 0, 0): 11,
    (255, 21, 0): 12,
    (255, 100, 0): 13,
    (255, 191, 0): 14,
    (255, 128, 0): 15,
    (255, 215, 0): 16,
    (255, 120, 0): 17,
    (255, 255, 0): 18,
    (191, 255, 0): 19,
    (128, 255, 0): 20,
    (0, 255, 0): 21,
    (0, 255, 128): 22,
    (64, 224, 208): 23,
    (0, 0, 255): 25,
    (127, 0, 255): 26,
    (128, 0, 128): 27,
    (230, 230, 250): 28,
    (255, 192, 203): 29,
    (255, 105, 180): 30,
}

# These are static colors, including the three formerly advertised as effects.
# Fire and Sunset's RGB values above come from the captured colorDefinitions.
# Ocean has a verified name/ID but no recovered swatch; do not invent one.
HAVEN_COLOR_MAP = {
    "Red": 11, "Fire": 12, "Pumpkin": 13, "Amber": 14, "Tangerine": 15,
    "Merigold": 16, "Sunset": 17, "Yellow": 18, "Lime": 19,
    "Light Green": 20, "Green": 21, "Sea Foam": 22, "Turquoise": 23,
    "Ocean": 24, "Deep Blue": 25, "Violet": 26, "Purple": 27,
    "Lavender": 28, "Pink": 29, "Hot Pink": 30,
}

# Read-only names for inventory and diagnostics. Pattern selection and rate
# commands require a verified contract; these IDs alone are not sufficient.
HAVEN_PATTERN_MAP = {"All Colors": 35, "Warm Colors": 33, "Cool Colors": 34}

# Preserve the old effect: off compatibility behavior without treating these
# static colors as native patterns or changing the namespace being reset.
LEGACY_EFFECT_COLOR_IDS = frozenset({12, 17, 24})

KELVIN_BY_ID = {color_id: kelvin for kelvin, color_id in HAVEN_KELVIN_MAP.items()}
RGB_BY_ID = {color_id: rgb for rgb, color_id in HAVEN_RGB_MAP.items()}
COLOR_BY_ID = {color_id: name for name, color_id in HAVEN_COLOR_MAP.items()}
PATTERN_BY_ID = {color_id: name for name, color_id in HAVEN_PATTERN_MAP.items()}
