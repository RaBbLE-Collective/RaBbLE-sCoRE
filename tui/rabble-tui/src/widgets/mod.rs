pub mod composer;
pub mod crew;
pub mod face;
pub mod header;
pub mod select;
pub mod modal;
pub mod transcript;

/// The mini mark's slot at the header's left edge: the face is a fixed 24x6 cell mark.
pub const FACE_SLOT_WIDTH: u16 = 24;
pub const FACE_ROWS: u16 = 6;
/// The header shows the mark (6 rows) only in a terminal at least this big; smaller panes keep a 3-row header.
pub const FACE_MIN_AREA: (u16, u16) = (56, 22);
/// Below `FACE_MIN_AREA` the mark shrinks into the 3-row header instead of vanishing: a dedicated micro mark
/// (`rabble_face::micro`: two eyes hanging from their portals, 7x3 cells), the three text lines reflowing to the right of it.
pub const FACE_COMPACT_SLOT_WIDTH: u16 = rabble_face::micro::MICRO_COLS as u16;
pub const FACE_COMPACT_ROWS: u16 = 3;
/// The compact mark needs at least this much pane (width, height); under it the header is text only.
pub const FACE_COMPACT_MIN_AREA: (u16, u16) = (30, 10);
