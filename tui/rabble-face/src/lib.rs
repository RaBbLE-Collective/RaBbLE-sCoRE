//! RaBbLE's face raster, no terminal deps, parity-tested against Python goldens (`ui/face.py`, `ui/eyes.py`).
//! Palette colours come from `rabble_theme::Palette`; nothing here draws to a terminal.

pub mod canvas;
pub mod degrade;
pub mod driver;
pub mod eyes;
pub mod face;
pub mod prng;

pub use canvas::{ansi, Canvas, Cell, Grid};
pub use degrade::{break_level, degrade, eye_block, noise_level, DegradeState};
pub use driver::{Driver, Life, Mood};
pub use face::{blend_pose, provider_of, Detail, Expr, Face, FacePalette, Layer, Pose, Provider};
pub use prng::SplitMix64;
