pub mod composer;
pub mod header;
pub mod modal;
pub mod transcript;

/// Where Lane F's face widget mounts (header, left edge). Empty on purpose until `rabble-face`
/// lands; the header reserves `FACE_SLOT_WIDTH` columns for it so nothing shifts when it does.
pub const FACE_SLOT_WIDTH: u16 = 0;
