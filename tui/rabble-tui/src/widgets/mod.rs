pub mod composer;
pub mod face;
pub mod header;
pub mod modal;
pub mod transcript;

/// The mini mark's slot at the header's left edge: the face is a fixed 24x6 cell mark.
pub const FACE_SLOT_WIDTH: u16 = 24;
pub const FACE_ROWS: u16 = 6;
/// The header shows the mark (6 rows) only in a terminal at least this big; smaller panes keep the 2-row header.
pub const FACE_MIN_AREA: (u16, u16) = (56, 22);
