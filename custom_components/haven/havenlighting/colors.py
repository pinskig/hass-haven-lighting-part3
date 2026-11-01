"""Haven's discrete color presets, shared by commands and state.

These mappings are not a device capability discovery result. The integration
uses SetColor with a preset ID; no continuous-Kelvin endpoint is established.
The white IDs match the colorDefinitions response captured during the Homey
work on 2025-05-26; preset 6 is 4400 K, not the inherited 4100 K label.
"""

HAVEN_KELVIN_MAP = {
    2700: 1, 3000: 2, 3500: 3, 3700: 4,
    4000: 5, 4400: 6, 4700: 7, 5000: 8,
}

HAVEN_RGB_MAP = {
    (255, 0, 0): 11,
    (255, 100, 0): 13,
    (255, 191, 0): 14,
    (255, 128, 0): 15,
    (255, 215, 0): 16,
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

HAVEN_EFFECT_MAP = {"Fire": 12, "Sunset": 17, "Ocean": 24}

KELVIN_BY_ID = {color_id: kelvin for kelvin, color_id in HAVEN_KELVIN_MAP.items()}
RGB_BY_ID = {color_id: rgb for rgb, color_id in HAVEN_RGB_MAP.items()}
EFFECT_BY_ID = {color_id: effect for effect, color_id in HAVEN_EFFECT_MAP.items()}
