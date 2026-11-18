# nebula_glyph

NeBuLA glyph, the terminal backend of NeBuLA (formerly the `rabble-face` crate). It draws RaBbLE's face as
terminal cells: the full face, the mini mark and the micro mark. No terminal dependencies; `rabble-tui` owns the I/O.

- Vocabulary (`src/vocab.rs`): canonical ids only. States `idle listening thinking speaking`, moods
  `ponder process insight`, cues (`error` and `denied` have a glyph form), expressions `calm joy sad angry`.
  The TUI's old private names (`typing streaming working error denied`) resolve through `vocab::ALIASES`, copied
  from `RaBbLE-Grimoire/registry/entity/rabble.entity.json` (`aliases.score_tui`, version 0.1.0); a test fails if
  the registry drifts.
- Reduced moves (`src/moves.rs`): `portal_swap` and `turn` last 0.5 s and boot 1.0 s, all stepped at 8 Hz.
  `cargo run --offline -p nebula_glyph --example moves -- swap|turn|boot` prints the frames.
- Reduced infinity mark: `Face::infinity`, full face only, off by default.
- `ui/face.py` stays the frozen golden oracle (`tests/golden_face.py`, `tests/golden.rs`).
- Design notes live in the Grimoire: `log/plans/Unified-Entity/` and `registry/entity/`.
