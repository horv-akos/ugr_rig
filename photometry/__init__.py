"""Raw frames -> calibrated luminance maps.

linearise    CR2 / simulator frame -> black-subtracted Bayer superpixels
hdr          Debevec-weighted merge on linear data
calibration  k model, target measurement, vignetting
masks        per-pixel direction and solid angle, rotations, Guth/Iwata position index
profile      config/analysis.toml
"""
