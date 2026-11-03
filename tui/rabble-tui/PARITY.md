# Parity with the Python UI (S296, Lane S)

Checklist source: `tests/test_ui_app.py`, `test_ui_transcript_scroll.py`, `test_ui_permission.py`, `test_ui_prefs.py`, `test_ui_theme.py`.
Rust tests live beside the code (`cargo test --offline --release` from `tui/`). `app::` = `src/app.rs` tests.

## test_ui_app.py
| Python test | Rust |
|---|---|
| test_text_rendered_once_and_header | `app::text_is_rendered_once_and_the_header_shows_the_model`, `transcript::text_is_drawn_once_when_the_block_follows_its_deltas` |
| test_tool_card_with_result | `app::a_tool_card_gets_its_result`, `transcript::tool_card_expands_to_the_input` |
| test_organ_turn_notices | `app::organ_turns_become_notices_and_background_turns_are_named` |
| test_escape_model_mode | `app::model_and_mode_commands_reach_the_engine_and_stick`, `model_picker_opens_without_an_argument_and_enter_chooses`, `interrupt_clears_pending_asks_and_the_placeholder` (Esc) |
| test_unknown_slash_passes_through | `app::an_unknown_slash_command_goes_to_the_engine_as_typed` |
| test_ctrl_c_twice_exits | `app::ctrl_c_twice_quits_once_is_a_hint` (also Ctrl-D) |
| test_permission_fallback_denies | dropped: the Python modal was an optional import; the Rust modal is always built |
| test_at_completion | `composer::at_completion_finds_files` (Python also keeps the `@`; so do we) |
| test_engine_calls_that_raise_become_notices | `app::engine_trouble_becomes_notices_not_crashes` (no session, `Error`, malformed line, closed socket) |
| test_mode_auto | `app::model_and_mode_commands...` (Shift+Tab cycles to `auto`) |
| test_copy_last_reply | `app::copy_puts_the_last_reply_on_the_clipboard_effect` (OSC 52 + wl-copy/xclip in `run.rs`, `util::base64` tested against RFC 4648 vectors) |
| test_reload_keeps_session_and_transcript, test_reload_refuses_syntax_errors_and_busy | dropped, because: `/reload` re-imports Python modules; a compiled binary has nothing to reload (restart reattaches to the live engine session: `--attach`) |
| test_tool_card_title_survives_bracket_runs | `transcript::tool_card_title_survives_bracket_runs` (no markup layer, so nothing to escape; pinned anyway) |
| test_system_clipboard_ignores_non_text | implemented (`util::system_clipboard` returns "" on a failing tool or non-UTF-8), NOT unit-tested: it shells out to wl-paste/xclip |

## test_ui_transcript_scroll.py
| Python test | Rust |
|---|---|
| stray_jump_to_top_while_blurred_is_repinned | `transcript::stray_jump_to_top_while_blurred_is_repinned` |
| stray_jump_right_after_refocus_is_repinned | `transcript::stray_jump_right_after_refocus_is_repinned` (400 ms guard, then the reader may scroll) |
| reader_scrolled_up_is_not_dragged_down | `transcript::follows_while_pinned_and_stays_put_when_scrolled_up`, `a_reader_scrolled_up_stays_up_across_blur` |
| nudged_two_lines_up_still_follows | `transcript::nudged_two_lines_up_still_follows...` (slack: distance < 3 lines) |
| submit_jumps_to_bottom_after_scrolling_up | `transcript::submit_jumps_to_bottom_after_scrolling_up`, `app::own_messages_jump_to_the_bottom_others_do_not_yank_the_reader` |
| wheel_burst_accumulates_into_one_glide | dropped, because: Mark chose 1:1 instant scroll (Vim feel). Replaced by `transcript::wheel_is_one_to_one_three_lines_per_notch` and `app::mouse_wheel_scrolls_one_to_one...`. `glide.rs` stays only for the R0 spike and the bench crate. |

## test_ui_permission.py
| Python test | Rust |
|---|---|
| allow_keys, allow_session, deny_with_reason, deny_empty_reason, escape_denies | `modal::{allow_keys, allow_session, deny_with_reason, deny_empty_reason, escape_denies}` |
| bash_command_not_truncated, edit_diff, organ_label | `modal::{bash_command_is_not_truncated, edit_shows_a_diff, the_organ_is_named}` |
| picker_enter / down_enter / escape / empty | `modal::picker_enter_down_enter_escape_and_empty` |
| no_hex_literals | `theme::no_hex_or_rgb_literals_anywhere_in_src` (walks every file under `src/`, fails if the walk finds too few; also rejects `Color::Rgb(`/`Color::Indexed(` outside `theme.rs`). The R0 spike's RGB literals now come from `Theme`. |
| keys_inside_the_arming_window_are_ignored | `modal::keys_inside_the_arming_window_are_ignored_but_escape_is_not`, `modal::continuous_typing_never_answers_even_past_the_arming_window` (STRONGER than Python: every ignored key pushes the deadline out, so a typist never answers; Enter is not an allow key; key repeats are ignored: `app::held_keys_cannot_answer_a_prompt...`) |
| answer_controls_stay_on_screen_in_a_short_pane | `modal::answer_controls_and_reason_stay_on_screen_in_a_short_pane` (8, 10, 16, 24, 40 rows; the click half is dropped: the Rust prompt is keyboard only). Differs on purpose: a body taller than the pane scrolls (Up/Down/PageUp/PageDown) and the allow keys stay dead until the end has been shown (`a_clipped_body_cannot_be_allowed_until_it_has_been_read`). |
| new (multi-client) | `app::permission_ask_answer_and_dismissal_by_another_client` (PermissionResolved closes the prompt, a replayed ask is not reopened) |

## test_ui_prefs.py
| Python test | Rust |
|---|---|
| settings_roundtrip_and_bad_file | `prefs::settings_roundtrip_and_bad_file` (same `ui.json`, same fields, same fallbacks) |
| resolve_model | `prefs::resolve_model_maps_labels_and_passes_ids_through` |
| model_crew_trust_commands | `app::model_trust_crew_commands` (`/crew` restarts via `create_session{resume,crew}`) |
| slash_completion_and_help | `app::slash_completion_uses_capabilities_and_help_lists_commands`, `composer::slash_completion_*` |

## test_ui_theme.py
| Python test | Rust |
|---|---|
| palette_parses | `rabble-theme` tests (Lane-shared crate) |
| rabble_theme | `theme::colour_comes_from_the_palette` |
| no_hex_literals | `theme::no_hex_or_rgb_literals_anywhere_in_src` |
| organ_colors | CHANGED: Python reads each persona's `color_base` from the Crew frontmatter; Rust hashes the organ name onto palette accents (`Theme::organ`). Persona colours are unfinished (needs the roster parsed in Rust or shipped by the engine). |
| new | `theme::no_color_drops_every_colour_but_keeps_emphasis`, `vscode_gets_safe_glyphs`, `app::nothing_the_shell_draws_needs_a_hex...` (NO_COLOR leaves no colour cells) |

## Behaviours in `ui/` with no Python test
Ported: header (model, mode, trust, crew, cwd, entropy, cost, context %, limit), divider with status, thinking placeholder, Ctrl-O tool cards, Shift+Tab mode cycle, history, multiline (Shift+Enter, Ctrl-J, Alt-Enter), bracketed paste, Ctrl-V clipboard paste, `/park` settings, `/park now|continue`, auto-park at `RABBLE_PARK_AT` (default 80%), handoff check, `/resume` picker (live engine sessions + `~/.claude/projects` history), `/status`, `/help`.
Dropped, because: `/reload` (compiled); Ctrl-C copies a mouse selection (mouse capture is on, the terminal selects with Shift+drag, so bare Ctrl-C only counts toward quitting); park ETA from context growth (`Bloat`); the "inside RaBbLE-sCoRE" warning; the OS usage tracker (5h/week windows in the header and `/status`), which reads `orchestra/usage.py`'s cache and needs a port or an engine event.
Not here (other lanes): narration lines (Lane N events render as `Unknown` until `rabble-proto` adds them). The face is below (S297).

## Added by the S296 review fixes (no Python counterpart)
Prompt: ask arriving behind a picker (`an_ask_that_arrives_while_a_picker_is_open...`), late `/resume` list never replaces a prompt and switching sessions clears queued asks (`a_late_session_list...`), tiny-terminal modals (`modals_do_not_panic_in_tiny_areas`).
Terminal: half frame discarded on panic (`run::a_half_built_frame...`), ESU first in restore (`run::restore_ends_synchronized_output...`), SIGTERM and SIGHUP restore the terminal (checked by hand in a pty), input-thread failure quits with a message.
Engine I/O: bounded frame queue with the input channel drained first, capped line reader, invalid UTF-8 is malformed not closed (`client::` tests), reconnect with `attach{since_seq: max(seq)}` (`app::a_dropped_connection_reconnects...`), a replaced `create_session` is closed not adopted, another session's `Error` is not shown.
Workers off the render thread: `@` file scan (capped during the walk, no symlinked dirs, cache dropped on submit), history titles (first 200 lines of at most 4 MiB), Ctrl-V clipboard (2 s limit), session list.
Safety of what is drawn: control characters, C1, DEL and bidi overrides are stripped, tabs expanded (`wrap::clean`; `transcript::escape_sequences_from_the_engine_never_reach_a_cell`, `modal::escape_sequences_in_a_prompt...`, `composer::submit_drops_the_file_cache_and_paste_strips_escapes`).

## The face (S297, Lane F): `ui/face.py`, `ui/eyes.py`, `tests/test_ui_face.py`, the face parts of `ui/app.py`
The Rust face is a cell-for-cell port: `tests/golden_face.py` dumps 48 frames (+5 degrade runs, rigs, meshes, Life) from Python into `tests/fixtures/face/golden.json`; `rabble-face/tests/golden.rs` replays them. Result: every cell identical, glyph and both colours (0 cells off by even one channel unit). `ui/prng.py` + `rabble-face/src/prng.rs` (SplitMix64) replaced `random.Random` in both bodies, so the mesh and orb dust differ from before the change in the PYTHON face too (Mark confirms by eye).
| Python | Rust |
|---|---|
| test_mini_shape | `behaviour::mini_shape` |
| test_rig_baseline (mini, 100x30, 200x55) | `golden::rig_baseline` (+ `rig_matches_python`: 12 rigs, snap and unsnapped, to 1e-9) |
| test_mood_mapping | `golden::moods_curves_and_blend_match` |
| test_blend_pose | same test (3 blends against Python's numbers) |
| test_widget_renders, test_full_fills_and_denied_reverts | `golden::driver_eases_and_denied_reverts` (the mood/pose state machine is `rabble_face::Driver`, no clock inside), `widgets::face::{the_mark_ticks_at_its_own_cadence_and_only_when_visible, the_full_face_draws_into_the_buffer_and_survives_a_resize}` |
| test_no_color | `widgets::face::no_color_shows_the_word_and_nothing_else` |
| test_provider_of | `golden::provider_of_matches` (14 models incl. local, empty, case) |
| test_provider_aura_tints_outside_the_eyes_only | `behaviour::provider_aura_tints_outside_the_eyes_only` + golden frames for all three providers |
| test_safe_glyphs_stay_in_block_elements | `behaviour::safe_glyphs_stay_in_block_elements`, `widgets::face::safe_glyph_override...` (the `RABBLE_GLYPHS` / `TERM_PROGRAM=vscode` env read in `face::safe_glyphs()` is not unit-tested: it reads the process environment) |
| test_compose_matches_frame (100x30, 220x60) | `behaviour::compose_matches_frame` + golden `compose` frames (cached background + layer + assemble equals Python's) |
| test_canvas_cells_fast_path_matches_cell | dropped, because: Rust has one cell path (no half-block "used cells" shortcut); every golden frame covers it |
| test_divider_flows_cyan_to_magenta | not face code (the divider widget); the Rust divider is a plain rule, unchanged |
| `degrade`, `noise_level`, `break_level`, `eye_block` (no Python test) | `golden::degrade_matches_python` (5 settings x 6 consecutive frames through one rng + burst state), curves test |
| `Life` | `golden::life_matches_python` (600 steps incl. a speed change) |
| mesh layout | `golden::mesh_matches_python` (3 sizes, node positions to 1e-9) |
| octant table | `golden::octant_table_matches_unicode` (256 patterns against Python's `unicodedata` build) |
| frame rate | `behaviour::frame_rate_gates_at_260x70` (eyes >= 30 fps, background >= 15 fps, release builds), `examples/bench.rs` |
| wiring: mood from events | `app::mood_follows_the_events_the_python_widget_reacts_to` (TextDelta streaming, ToolCall/ToolResult/Organ working, composer text typing, Error flash, observer TurnDone clears), `denying_a_permission_flashes_the_denied_mood` |
| wiring: provider tint, signal | `app::the_providers_tint_follows_the_model_and_the_entropy_signal_reaches_the_face` (ModelUsed -> provider, `EntropyChanged.local` -> none, `base`/`stability` -> noise/breaking) |
| `/face` | `app::slash_face_mounts_a_full_face_until_the_next_message` (transient: the next submit drops it, as Python's `drop_transient`) |
| never delays keys | `app::keys_and_wheel_never_compute_a_face_frame` (only `tick_face` computes, and `run.rs` calls it only on an iteration with no input), `a_face_frame_is_cheap_enough_to_sit_between_keys` |
| mark layout | `app::the_mark_takes_six_header_rows_in_a_big_pane_and_none_in_a_small_one` |

Differences, on purpose:
- Header: the mark makes the header 6 rows (as Python) only when the pane is at least 56x22 (`FACE_MIN_AREA`); smaller panes keep the 2-row header and the mark costs nothing (not ticked while hidden). The two text lines are centred beside the mark; Python's header shows five more info lines (capability bars, usage windows, ...) that the Rust shell does not have yet.
- Frames are computed inline on the event loop, not on the two worker threads Python needs because of the GIL. A frame costs 0.2 ms (mark) to about 1.3 ms (full, 260x70); `tick_face` runs only when the loop has no keyboard or wheel input in hand, so under a wheel flood the face holds its last frame. A paint is requested only when a cell changed (replaces `_refresh_changed`).
- Palette-background cells draw with the terminal's default background (translucent terminals show through), where Python resolves to the header's background.
- `/face` without a palette: a notice (`RaBbLE`), nothing to draw.
- Dropped, because: the stepped drawing path (`fine_eyes=False`, `_eye_and_portal`, `_in_pill`: dead code, never enabled); constructor knobs only tests used (`eye_h`, `eye_w`, `aura`, `rim`, `rim_tips`, `spread`); `/reload` of `ui.prng` (compiled); the style cache and `Strip` merging (ratatui diffs cells).
- Not done: the native fixes the plan asked for where Python could not (dark stair-stepped halo, black portal fill, specks on the mini mark): parity first, the goldens pin Python's pixels, so any fix is a deliberate golden regeneration with Mark's eye.

## Gaze (S303): `ui/gaze.py`, the gaze parts of `ui/app.py`, `ui/face.py`, `tests/test_ui_gaze.py`
The Python twin looks where the Rust shell looks. Rust: `mod gaze` in `src/app.rs` (`steer_gaze`, `gaze_scene`), `widgets/face.rs` (`cell_aspect`).
| Behaviour | Python | Rust |
|---|---|---|
| constants (`EVERY`, `POINTER_ACTIVE`, `POINTER_TTL`, `ATTN_EVERY`, `ATTN_HOLD`, `ATTN_TTL`, `TYPING_HOLD`, `CARD_HOLD`, `RANGE_COLS/ROWS`) | `ui/gaze.py`, pinned to the Rust source text by `test_constants_match_rust` | `mod gaze` |
| `choose()` priority modal > typing > card > streaming text; `aim()`; hysteresis (`pick`) | `gaze.choose/aim/Steer.pick`; `test_choose_priority`, `test_aim_is_relative_and_clamped`, `test_hysteresis` | `gaze::choose/aim/Steer::pick` tests |
| pointer steers the header mark and every `/face`; no repaint; motion is not typing; lapses over `POINTER_TTL` | `RabbleApp.on_mouse_move/down/up`, `_gaze_tick`; `test_pointer_steers_both_faces_without_a_repaint_and_is_not_typing`, `test_step_pointer_then_lapse_then_saccades` | `the_pointer_steers_both_faces_without_a_repaint_and_lapses` |
| entity attention when the pointer idled 2 s; re-pick every 500 ms | `Steer.step`; `test_step_rechooses_only_every_attn_every` | `steer_gaze` |
| modal / composer cursor / newest line after a card / end of streaming text, else saccades; follows real loop order past `ATTN_HOLD`; a failed measure keeps the last good newest line | `_gaze_scene`, `_measure_newest` (returns `gaze.MISS`, so `Steer.newest` is kept); `test_entity_follows_streaming_text_in_real_loop_order_past_attn_hold`, `test_modal_wins_and_a_key_in_the_composer_counts_as_typing` | `the_entity_looks_at_streaming_text_and_the_composer_then_lets_go`, `gz.newest` cache (45315fa) |
| cell aspect cached until resize; `RABBLE_CELL_ASPECT` parsed like `f64::from_str` (rejects `" 2.3"`, `"2_3"`, fullwidth digits) | `face.cell_aspect/reset_cell_aspect`, `FaceWidget.on_resize`; `test_cell_aspect_env_is_strict`, `test_cell_aspect_is_cached_until_reset` | `widgets/face.rs` |

Differences, on purpose:
- The gaze tick is a Textual 10 Hz timer (`gaze.EVERY`); Rust rides its face tick. Same cadence, same "never repaints by itself" (`Life.set_gaze` only stores a target).
- Any key in the focused composer counts as typing (as in Rust); the permission modal is a separate Textual screen, so keys there never do.
- Face centres come from each widget's screen `region`; a `/face` scrolled out of view has no centre and falls back to saccades (Rust: `full_visible`). Several `/face` widgets each aim from their own centre.
- The newest line is measured from the last transcript leaf widget (up to 4 rows from its bottom, text end via `render_line`), only while streaming or a card is fresh, and cached; Rust reads `Transcript::newest_line` at layout.
- Other overlays (model/session pickers) are not looked at, as in Rust (only the permission prompt is).
- Python's `FaceWidget.-full` min-height is 2, as the Rust floor.
